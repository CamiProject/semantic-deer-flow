from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from deerflow.runtime.secret_context import (
    SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY,
    redact_secret_context_keys,
)
from deerflow.semantic.client import (
    SemanticCallContext,
    SemanticClientError,
    SemanticPlatformClient,
)
from deerflow.tools.builtins.semantic_tools import SEMANTIC_TOOLS, propose_action, query_metrics


@pytest.mark.asyncio
async def test_semantic_client_forwards_private_auth_and_runtime_correlation_headers():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["json"] = request.content.decode()
        return httpx.Response(200, json={"rows": [], "semantic_trace_id": "trace-1"})

    client = SemanticPlatformClient(
        base_url="http://semantic.internal:8003",
        service_token="service-secret",
        call_context=SemanticCallContext(
            authorization_token="user-jwt",
            run_id="run-1",
            thread_id="thread-1",
            tool_call_id="tool-1",
            semantic_trace_id="trace-1",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = await client.query_metrics(
        metrics=["site.count"],
        dimensions=[],
        filters=[],
        order_by=[],
        limit=10,
    )

    assert result["rows"] == []
    assert captured["headers"]["x-deerflow-semantic-token"] == "service-secret"
    assert captured["headers"]["x-saas-authorization-context"] == "user-jwt"
    assert captured["headers"]["x-deerflow-run-id"] == "run-1"
    assert captured["headers"]["x-deerflow-thread-id"] == "thread-1"
    assert captured["headers"]["x-deerflow-tool-call-id"] == "tool-1"
    assert captured["headers"]["x-deerflow-semantic-trace-id"] == "trace-1"


def test_semantic_tools_do_not_expose_runtime_or_security_boundaries_in_schemas():
    expected = {
        "resolve_business_context",
        "search_objects",
        "get_object",
        "query_metrics",
        "explain_metric",
        "list_available_actions",
        "propose_action",
        "preview_action",
        "execute_action",
        "get_action_status",
    }

    assert {tool.name for tool in SEMANTIC_TOOLS} == expected
    for semantic_tool in SEMANTIC_TOOLS:
        assert "runtime" not in semantic_tool.args
        serialized_schema = str(semantic_tool.args).lower()
        for forbidden in ("tenant", "database", "jdbc", "password", "service_token", "scope_hash"):
            assert forbidden not in serialized_schema


def test_semantic_private_token_is_removed_by_runtime_secret_redaction():
    context = {
        "thread_id": "thread-1",
        SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY: "raw-user-jwt",
    }

    assert redact_secret_context_keys(context) == {"thread_id": "thread-1"}


def test_semantic_client_missing_private_auth_fails_without_exposing_service_token(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_SEMANTIC_SERVICE_TOKEN", "service-secret")
    runtime = SimpleNamespace(
        context={"run_id": "run-1", "thread_id": "thread-1"},
        tool_call_id="tool-1",
    )

    with pytest.raises(SemanticClientError) as exc_info:
        SemanticPlatformClient.from_runtime(runtime)

    assert exc_info.value.code == "AUTHENTICATION_FAILED"
    assert "service-secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_query_metrics_tool_uses_only_structured_semantic_request(monkeypatch):
    captured = {}

    class FakeClient:
        async def query_metrics(self, **kwargs):
            captured.update(kwargs)
            return {"rows": [{"site.count": 1}]}

    monkeypatch.setattr(
        "deerflow.tools.builtins.semantic_tools._client",
        lambda _runtime: FakeClient(),
    )

    result = await query_metrics.coroutine(
        metrics=["site.count"],
        dimensions=["site_id"],
        filters=[{"field": "name", "op": "eq", "value": "A"}],
        order_by=[{"field": "site.count", "direction": "desc"}],
        limit=20,
        runtime=SimpleNamespace(),
    )

    assert result == {"rows": [{"site.count": 1}]}
    assert captured == {
        "metrics": ["site.count"],
        "dimensions": ["site_id"],
        "filters": [{"field": "name", "op": "eq", "value": "A"}],
        "order_by": [{"field": "site.count", "direction": "desc"}],
        "limit": 20,
    }


@pytest.mark.asyncio
async def test_propose_action_persists_and_previews_without_executing(monkeypatch):
    calls = []

    class FakeClient:
        async def propose_action(self, **kwargs):
            calls.append(("propose", kwargs))
            return {"proposal_id": "proposal-1", "status": "PROPOSED"}

        async def preview_action(self, **kwargs):
            calls.append(("preview", kwargs))
            return {"proposal_id": "proposal-1", "status": "PENDING_APPROVAL"}

    monkeypatch.setattr(
        "deerflow.tools.builtins.semantic_tools._client",
        lambda _runtime: FakeClient(),
    )

    result = await propose_action.coroutine(
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "New"},
        runtime=SimpleNamespace(),
    )

    assert result == {"proposal_id": "proposal-1", "status": "PENDING_APPROVAL"}
    assert calls == [
        (
            "propose",
            {
                "action_id": "site.rename",
                "target_id": "site-demo-001",
                "parameters": {"name": "New"},
                "reason": None,
                "expected_object_version": None,
            },
        ),
        ("preview", {"proposal_id": "proposal-1"}),
    ]
