"""HTTP client for the independently authenticated Semantic Platform."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from deerflow.runtime.secret_context import SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY

SEMANTIC_API_URL_ENV_VAR = "DEER_FLOW_SEMANTIC_API_URL"
SEMANTIC_SERVICE_TOKEN_ENV_VAR = "DEER_FLOW_SEMANTIC_SERVICE_TOKEN"
SEMANTIC_TIMEOUT_ENV_VAR = "DEER_FLOW_SEMANTIC_TIMEOUT_SECONDS"


class SemanticClientError(RuntimeError):
    """A sanitized Semantic Platform failure safe to return to an Agent."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        safe_code = str(code or "EXECUTION_FAILED")[:64]
        safe_message = str(message or "Semantic Platform request failed")[:500]
        super().__init__(f"{safe_code}: {safe_message}")
        self.code = safe_code
        self.message = safe_message
        self.status_code = status_code


@dataclass(frozen=True)
class SemanticCallContext:
    authorization_token: str
    run_id: str
    thread_id: str
    tool_call_id: str
    semantic_trace_id: str


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SemanticClientError("AUTHENTICATION_FAILED", f"Missing trusted {field}")
    return text


def semantic_call_context(
    runtime: Any,
    *,
    tool_call_id: str | None = None,
) -> SemanticCallContext:
    context = getattr(runtime, "context", None)
    if not isinstance(context, Mapping):
        raise SemanticClientError("AUTHENTICATION_FAILED", "Missing trusted runtime context")
    runtime_tool_call_id = getattr(runtime, "tool_call_id", None)
    return SemanticCallContext(
        authorization_token=_required_text(
            context.get(SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY),
            "SaaS authorization token",
        ),
        run_id=_required_text(context.get("run_id"), "run_id"),
        thread_id=_required_text(context.get("thread_id"), "thread_id"),
        tool_call_id=_required_text(
            tool_call_id or runtime_tool_call_id,
            "tool_call_id",
        ),
        semantic_trace_id=_required_text(
            context.get("semantic_trace_id") or str(uuid.uuid4()),
            "semantic_trace_id",
        ),
    )


class SemanticPlatformClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        call_context: SemanticCallContext,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise SemanticClientError("POLICY_UNAVAILABLE", "Invalid Semantic Platform URL")
        if not service_token.strip():
            raise SemanticClientError("POLICY_UNAVAILABLE", "Semantic Platform service authentication is not configured")
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self.call_context = call_context
        self._timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))
        self._transport = transport

    @classmethod
    def from_runtime(
        cls,
        runtime: Any,
        *,
        tool_call_id: str | None = None,
    ) -> SemanticPlatformClient:
        try:
            timeout = float(os.environ.get(SEMANTIC_TIMEOUT_ENV_VAR, "30"))
        except ValueError as exc:
            raise SemanticClientError("POLICY_UNAVAILABLE", "Invalid Semantic Platform timeout") from exc
        return cls(
            base_url=os.environ.get(SEMANTIC_API_URL_ENV_VAR, "http://127.0.0.1:8003"),
            service_token=os.environ.get(SEMANTIC_SERVICE_TOKEN_ENV_VAR, ""),
            call_context=semantic_call_context(runtime, tool_call_id=tool_call_id),
            timeout_seconds=timeout,
        )

    @property
    def idempotency_key(self) -> str:
        material = f"{self.call_context.run_id}:{self.call_context.tool_call_id}"
        return "df-" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "X-DeerFlow-Semantic-Token": self._service_token,
            "X-SaaS-Authorization-Context": self.call_context.authorization_token,
            "X-DeerFlow-Run-Id": self.call_context.run_id,
            "X-DeerFlow-Thread-Id": self.call_context.thread_id,
            "X-DeerFlow-Tool-Call-Id": self.call_context.tool_call_id,
            "X-DeerFlow-Semantic-Trace-Id": self.call_context.semantic_trace_id,
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _error_payload(response: httpx.Response) -> tuple[str, str]:
        try:
            payload = response.json()
        except ValueError:
            return "EXECUTION_FAILED", "Semantic Platform request failed"
        detail = payload.get("detail") if isinstance(payload, Mapping) else None
        if isinstance(detail, Mapping):
            return (
                str(detail.get("code") or "EXECUTION_FAILED"),
                str(detail.get("message") or "Semantic Platform request failed"),
            )
        return "EXECUTION_FAILED", "Semantic Platform request failed"

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    headers=self._headers(headers),
                    json=dict(json_body) if json_body is not None else None,
                )
        except httpx.HTTPError as exc:
            raise SemanticClientError(
                "POLICY_UNAVAILABLE",
                "Semantic Platform is unavailable",
            ) from exc
        if response.is_error:
            code, message = self._error_payload(response)
            raise SemanticClientError(code, message, status_code=response.status_code)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise SemanticClientError(
                "EXECUTION_FAILED",
                "Semantic Platform returned an invalid response",
            ) from exc
        if not isinstance(payload, dict):
            raise SemanticClientError(
                "EXECUTION_FAILED",
                "Semantic Platform returned an invalid response",
            )
        return payload

    async def resolve_business_context(
        self,
        *,
        question: str,
        include_facts: bool,
        fact_limit: int,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/v1/ontology/resolve",
            json_body={
                "question": question,
                "include_facts": include_facts,
                "fact_limit": fact_limit,
            },
        )

    async def search_objects(
        self,
        *,
        object_type: str,
        filters: list[dict[str, Any]],
        properties: list[str] | None,
        limit: int,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/v1/objects/search",
            json_body={
                "object_type": object_type,
                "filters": filters,
                "properties": properties,
                "limit": limit,
            },
        )

    async def get_object(self, *, object_type: str, object_id: str) -> dict[str, Any]:
        return await self.request(
            "GET",
            f"/v1/objects/{quote(object_type, safe='')}/{quote(object_id, safe='')}",
        )

    async def query_metrics(
        self,
        *,
        metrics: list[str],
        dimensions: list[str],
        filters: list[dict[str, Any]],
        order_by: list[dict[str, Any]],
        limit: int,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/v1/queries",
            json_body={
                "metrics": metrics,
                "dimensions": dimensions,
                "filters": filters,
                "order_by": order_by,
                "limit": limit,
            },
        )

    async def explain_metric(self, *, metric_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/metrics/{quote(metric_id, safe='')}")

    async def list_available_actions(self) -> dict[str, Any]:
        return await self.request("GET", "/v1/actions")

    async def propose_action(
        self,
        *,
        action_id: str,
        target_id: str,
        parameters: Mapping[str, Any],
        reason: str | None,
        expected_object_version: str | None,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/v1/actions/proposals",
            headers={"Idempotency-Key": self.idempotency_key},
            json_body={
                "action_id": action_id,
                "target_id": target_id,
                "parameters": dict(parameters),
                "reason": reason,
                "expected_object_version": expected_object_version,
            },
        )

    async def preview_action(self, *, proposal_id: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/v1/actions/proposals/{quote(proposal_id, safe='')}/preview",
        )

    async def execute_action(self, *, proposal_id: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/v1/actions/proposals/{quote(proposal_id, safe='')}/execute",
        )

    async def get_action_status(self, *, execution_id: str) -> dict[str, Any]:
        return await self.request(
            "GET",
            f"/v1/actions/executions/{quote(execution_id, safe='')}",
        )
