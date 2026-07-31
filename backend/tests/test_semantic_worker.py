from __future__ import annotations

import time

import httpx
import jwt
import pytest

from app.semantic.actions import ActionError
from app.semantic.worker import (
    DomainApiActionExecutor,
    SaasAuthorizationRevalidator,
)

SECRET = "test-secret-at-least-thirty-two-bytes"


def _authorization_token():
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "saas-gateway",
            "aud": "action-worker",
            "sub": "public-user-001",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "system_code": "demo",
            "role_codes": ["site_admin"],
            "scope": {
                "mode": "resource_set",
                "site_ids": ["site-demo-001"],
                "project_ids": [],
            },
            "permission_version": "1",
            "iat": now,
            "exp": now + 300,
            "jti": "worker-auth-1",
        },
        SECRET,
        algorithm="HS256",
    )


def _proposal():
    return {
        "proposal_id": "proposal-1",
        "action_id": "site.rename",
        "action_version": "1",
        "target_type": "Site",
        "target_id": "site-demo-001",
        "parameters": {"name": "New"},
        "reason": "rename",
        "expected_object_version": "3",
        "scope_hash": "scope-1",
        "permission_version": "1",
        "idempotency_key": "idem-1",
        "run_id": "run-1",
        "thread_id": "thread-1",
        "tool_call_id": "tool-1",
        "semantic_trace_id": "trace-1",
        "authorization_snapshot": {
            "principal_id": "public-user-001",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "system_code": "demo",
        },
        "executor": {
            "type": "domain_api",
            "method": "PATCH",
            "path": "/sites/{target_id}",
            "result_fields": ["updated"],
        },
    }


@pytest.mark.asyncio
async def test_action_revalidator_requires_fresh_signed_worker_token(monkeypatch):
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", SECRET)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={"authorization_token": _authorization_token()},
        )

    revalidator = SaasAuthorizationRevalidator(
        url="http://iam.internal/v1/actions/revalidate",
        service_token="iam-service-token",
        transport=httpx.MockTransport(handler),
    )

    authorization = await revalidator.revalidate(_proposal())

    assert authorization.principal_id == "public-user-001"
    assert authorization.allowed_site_ids == ("site-demo-001",)
    assert captured["headers"]["x-saas-internal-token"] == "iam-service-token"


@pytest.mark.asyncio
async def test_action_revalidator_fails_closed_when_not_configured():
    revalidator = SaasAuthorizationRevalidator(url="", service_token="")

    with pytest.raises(ActionError) as exc_info:
        await revalidator.revalidate(_proposal())

    assert exc_info.value.code == "POLICY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_domain_executor_forwards_idempotency_version_and_trace_headers():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "updated": True,
                "password": "must-not-reach-agent",
                "debug": "internal diagnostic",
            },
        )

    executor = DomainApiActionExecutor(
        base_url="http://domain.internal",
        service_token="domain-service-token",
        transport=httpx.MockTransport(handler),
    )

    result = await executor.execute(_proposal())

    assert result == {"updated": True}
    assert captured["path"] == "/sites/site-demo-001"
    assert captured["headers"]["idempotency-key"] == "idem-1"
    assert captured["headers"]["if-match"] == "3"
    assert captured["headers"]["x-deerflow-run-id"] == "run-1"
    assert captured["headers"]["x-deerflow-semantic-trace-id"] == "trace-1"
    assert captured["headers"]["x-saas-internal-token"] == "domain-service-token"


@pytest.mark.asyncio
async def test_domain_executor_rejects_network_path_reference_without_sending_token():
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"updated": True})

    proposal = _proposal()
    proposal["executor"] = {
        "type": "domain_api",
        "method": "PATCH",
        "path": "//attacker.example/sites/{target_id}",
        "result_fields": ["updated"],
    }
    executor = DomainApiActionExecutor(
        base_url="http://domain.internal",
        service_token="domain-service-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ActionError, match="Unsafe domain API path"):
        await executor.execute(proposal)

    assert called is False


@pytest.mark.asyncio
async def test_domain_executor_compensation_uses_separate_idempotency_key():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"compensated": True})

    executor = DomainApiActionExecutor(
        base_url="http://domain.internal",
        service_token="domain-service-token",
        transport=httpx.MockTransport(handler),
    )

    result = await executor.compensate(
        _proposal(),
        {
            "type": "domain_api",
            "method": "POST",
            "path": "/sites/{target_id}/compensate-rename",
            "result_fields": ["compensated"],
        },
        error_code="EXECUTION_FAILED",
    )

    assert result == {"compensated": True}
    assert captured["path"] == "/sites/site-demo-001/compensate-rename"
    assert captured["headers"]["idempotency-key"] != "idem-1"
    assert len(captured["headers"]["idempotency-key"]) == 64
    assert '"compensation":true' in captured["body"]
    assert '"compensation_for_error":"EXECUTION_FAILED"' in captured["body"]
