"""Read-only Semantic Platform evidence client."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx
import jwt


class SemanticEvidenceError(RuntimeError):
    pass


class SemanticEvidenceClient:
    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = settings.semantic_url.rstrip("/")
        self._service_token = settings.semantic_service_token
        self._transport = transport
        self._approval_key = settings.approval_jwt_key or settings.authorization_jwt_key
        self._approval_algorithm = settings.authorization_algorithm
        self._approval_issuer = settings.approval_jwt_issuer or settings.authorization_issuer
        self._approval_ttl_seconds = settings.authorization_ttl_seconds

    def _headers(
        self,
        *,
        authorization_token: str,
        run_id: str,
        thread_id: str,
    ) -> dict[str, str]:
        correlation = uuid.uuid4().hex
        return {
            "X-DeerFlow-Semantic-Token": self._service_token,
            "X-SaaS-Authorization-Context": authorization_token,
            "X-DeerFlow-Run-Id": run_id,
            "X-DeerFlow-Thread-Id": thread_id,
            "X-DeerFlow-Tool-Call-Id": f"eval-collector-{correlation}",
            "X-DeerFlow-Semantic-Trace-Id": f"eval-collector-{correlation}",
        }

    async def get_trace(
        self,
        semantic_trace_id: str,
        *,
        authorization_token: str,
        run_id: str,
        thread_id: str,
    ) -> dict[str, Any] | None:
        return await self._get(
            f"/v1/audit/traces/{semantic_trace_id}",
            authorization_token=authorization_token,
            run_id=run_id,
            thread_id=thread_id,
        )

    async def get_action(
        self,
        proposal_id: str,
        *,
        authorization_token: str,
        run_id: str,
        thread_id: str,
    ) -> dict[str, Any] | None:
        return await self._get(
            f"/v1/actions/proposals/{proposal_id}/evidence",
            authorization_token=authorization_token,
            run_id=run_id,
            thread_id=thread_id,
        )

    async def approve_and_execute(
        self,
        proposal_id: str,
        *,
        principal_id: str,
        expected_scope_hash: str,
        authorization_token: str,
        run_id: str,
        thread_id: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        evidence = await self.get_action(
            proposal_id,
            authorization_token=authorization_token,
            run_id=run_id,
            thread_id=thread_id,
        )
        if evidence is None or not isinstance(evidence.get("proposal"), dict):
            raise SemanticEvidenceError("Action proposal evidence is unavailable")
        proposal = evidence["proposal"]
        status = proposal.get("status")
        if status == "PROPOSED":
            proposal = await self._post(
                f"/v1/actions/proposals/{proposal_id}/preview",
                {},
                authorization_token=authorization_token,
                run_id=run_id,
                thread_id=thread_id,
            )
            status = proposal.get("status")
        if status == "PENDING_APPROVAL":
            now = int(time.time())
            approval_token = jwt.encode(
                {
                    "iss": self._approval_issuer,
                    "aud": "semantic-action-approval",
                    "sub": principal_id,
                    "proposal_id": proposal_id,
                    "scope_hash": expected_scope_hash,
                    "approved_by": "eval-runner",
                    "iat": now,
                    "exp": now + self._approval_ttl_seconds,
                    "jti": str(uuid.uuid4()),
                },
                self._approval_key,
                algorithm=self._approval_algorithm,
            )
            proposal = await self._post(
                f"/v1/actions/proposals/{proposal_id}/approve",
                {"approval_token": approval_token},
                authorization_token=authorization_token,
                run_id=run_id,
                thread_id=thread_id,
            )
            status = proposal.get("status")
        if status not in {"READY", "EXECUTING", "SUCCEEDED", "FAILED", "COMPENSATED", "COMPENSATION_FAILED"}:
            raise SemanticEvidenceError(f"Action proposal cannot be executed from {status}")
        if status == "READY":
            await self._post(
                f"/v1/actions/proposals/{proposal_id}/execute",
                None,
                authorization_token=authorization_token,
                run_id=run_id,
                thread_id=thread_id,
            )

        deadline = time.monotonic() + timeout_seconds
        terminal = {"SUCCEEDED", "FAILED", "COMPENSATED", "COMPENSATION_FAILED"}
        while True:
            evidence = await self.get_action(
                proposal_id,
                authorization_token=authorization_token,
                run_id=run_id,
                thread_id=thread_id,
            )
            if evidence and isinstance(evidence.get("execution"), dict):
                execution_status = evidence["execution"].get("status")
                if execution_status in terminal:
                    return evidence
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Action {proposal_id} did not reach a terminal state")
            await asyncio.sleep(0.2)

    async def _get(
        self,
        path: str,
        *,
        authorization_token: str,
        run_id: str,
        thread_id: str,
    ) -> dict[str, Any] | None:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(
                authorization_token=authorization_token,
                run_id=run_id,
                thread_id=thread_id,
            ),
            timeout=30,
            transport=self._transport,
            trust_env=False,
        ) as client:
            response = await client.get(path)
        if response.status_code == 404:
            return None
        if response.is_error:
            raise SemanticEvidenceError(f"Semantic evidence lookup failed with HTTP {response.status_code}: {response.text[:1000]}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise SemanticEvidenceError("Semantic evidence response must be a JSON object")
        return payload

    async def _post(
        self,
        path: str,
        payload: dict[str, Any] | None,
        *,
        authorization_token: str,
        run_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(
                authorization_token=authorization_token,
                run_id=run_id,
                thread_id=thread_id,
            ),
            timeout=30,
            transport=self._transport,
            trust_env=False,
        ) as client:
            response = await client.post(path, json=payload) if payload is not None else await client.post(path)
        if response.is_error:
            raise SemanticEvidenceError(f"Semantic Action request failed with HTTP {response.status_code}: {response.text[:1000]}")
        value = response.json()
        if not isinstance(value, dict):
            raise SemanticEvidenceError("Semantic Action response must be a JSON object")
        return value
