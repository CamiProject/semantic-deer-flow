"""HTTP client for executing and reading real Gateway runs."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.evals.contracts import EvalCase


class EvalGatewayError(RuntimeError):
    """Raised when the Gateway execution contract cannot be completed."""


@dataclass(frozen=True)
class GatewayTrialResult:
    thread_id: str
    run_id: str
    wait_response: dict[str, Any]
    run: dict[str, Any]
    events: list[dict[str, Any]]
    latency_ms: int


class GatewayClient:
    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = settings.gateway_url.rstrip("/")
        self._internal_token = settings.gateway_internal_token
        self._transport = transport

    def _headers(self, *, authorization_token: str, principal_id: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-DeerFlow-Internal-Token": self._internal_token,
            "X-SaaS-Authorization-Context": authorization_token,
            "X-DeerFlow-Owner-User-Id": principal_id,
        }

    async def execute_trial(
        self,
        *,
        case: EvalCase,
        eval_run_id: str,
        trial_index: int,
        dataset_hash: str,
        thread_id: str,
        authorization_token: str,
        timeout_seconds: int,
    ) -> GatewayTrialResult:
        headers = self._headers(
            authorization_token=authorization_token,
            principal_id=case.fixture.principal_id,
        )
        metadata = {
            "eval_run_id": eval_run_id,
            "eval_case_id": case.case_id,
            "eval_trial_index": trial_index,
            "eval_dataset_hash": dataset_hash,
        }
        start = time.monotonic()
        wait_response: dict[str, Any] = {}
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout_seconds,
            transport=self._transport,
            trust_env=False,
        ) as client:
            for turn in case.turns:
                response = await client.post(
                    "/api/runs/saas-query/wait",
                    json={
                        "input": {"messages": [{"role": turn.role, "content": turn.content}]},
                        "metadata": metadata,
                        "config": {"configurable": {"thread_id": thread_id}},
                        "on_disconnect": "continue",
                        "on_completion": "keep",
                    },
                )
                self._raise_for_status(response, "Gateway Run")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise EvalGatewayError("Gateway wait response must be a JSON object")
                wait_response = payload

            run = await self._find_trial_run(client, thread_id=thread_id, metadata=metadata)
            run_id = str(run.get("run_id") or "")
            if not run_id:
                raise EvalGatewayError("Gateway Run record has no run_id")
            events = await self._read_all_events(client, thread_id=thread_id, run_id=run_id)
        return GatewayTrialResult(
            thread_id=thread_id,
            run_id=run_id,
            wait_response=wait_response,
            run=run,
            events=events,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response, operation: str) -> None:
        if response.is_error:
            detail = response.text[:1000]
            raise EvalGatewayError(f"{operation} failed with HTTP {response.status_code}: {detail}")

    async def _find_trial_run(
        self,
        client: httpx.AsyncClient,
        *,
        thread_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        for attempt in range(5):
            response = await client.get(f"/api/threads/{thread_id}/runs")
            self._raise_for_status(response, "Gateway Run lookup")
            payload = response.json()
            if not isinstance(payload, list):
                raise EvalGatewayError("Gateway Run lookup response must be a list")
            matches = [item for item in payload if isinstance(item, dict) and all((item.get("metadata") or {}).get(key) == value for key, value in metadata.items())]
            if matches:
                return sorted(matches, key=lambda item: str(item.get("created_at") or ""))[-1]
            if attempt < 4:
                await asyncio.sleep(0.1 * (attempt + 1))
        raise EvalGatewayError("Gateway Run record was not found for the Eval Trial")

    async def _read_all_events(
        self,
        client: httpx.AsyncClient,
        *,
        thread_id: str,
        run_id: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        after_seq: int | None = None
        while True:
            params: dict[str, Any] = {"limit": 2000}
            if after_seq is not None:
                params["after_seq"] = after_seq
            response = await client.get(
                f"/api/threads/{thread_id}/runs/{run_id}/events",
                params=params,
            )
            self._raise_for_status(response, "Gateway Run events lookup")
            page = response.json()
            if not isinstance(page, list):
                raise EvalGatewayError("Gateway Run events response must be a list")
            valid = [item for item in page if isinstance(item, dict)]
            events.extend(valid)
            if len(page) < 2000:
                break
            next_seq = max((int(item.get("seq", 0)) for item in valid), default=after_seq or 0)
            if after_seq is not None and next_seq <= after_seq:
                raise EvalGatewayError("Gateway Run event pagination did not advance")
            after_seq = next_seq
        return events
