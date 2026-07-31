from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import jwt
from fastapi.testclient import TestClient

from app.semantic.api import create_app
from app.semantic.config import SemanticSettings
from deerflow.semantic.ontology import OntologyRegistry
from deerflow.semantic.sql_scope import SqlScopePolicyRegistry

SECRET = "test-secret-at-least-thirty-two-bytes"


def _token(**overrides):
    now = int(time.time())
    claims = {
        "iss": "saas-gateway",
        "aud": ["deerflow", "semantic-platform"],
        "sub": "public-user-001",
        "tenant_id": "public-tenant-001",
        "tenant_code": "public_demo",
        "system_code": "demo",
        "role_codes": ["site_admin"],
        "scope": {"mode": "resource_set", "site_ids": ["site-demo-001"], "project_ids": []},
        "permission_version": "1",
        "iat": now,
        "exp": now + 300,
        "jti": "authz-1",
    }
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _ontology():
    return OntologyRegistry.from_mapping(
        {
            "version": "1",
            "objects": {
                "Site": {
                    "table": "demo_sites",
                    "id_field": "id",
                    "label": "场地",
                    "properties": {
                        "id": {"column": "id", "type": "string"},
                        "name": {"column": "name", "type": "string"},
                    },
                }
            },
            "links": {},
            "metrics": {
                "site.count": {
                    "object_type": "Site",
                    "aggregation": "count",
                    "dimensions": [],
                    "filters": [],
                }
            },
            "actions": {
                "site.rename": {
                    "target_type": "Site",
                    "scope_dimension": "site",
                    "parameters": {"name": {"type": "string", "required": True}},
                    "approval": {"required": True},
                    "executor": {"type": "domain_api", "path": "/sites/{target_id}"},
                }
            },
        }
    )


def _policy():
    return SqlScopePolicyRegistry.from_mapping({"version": "1", "tables": {"demo_sites": {"access": "scoped", "scope_dimension": "site", "scope_column": "id"}}})


def _settings(tmp_path):
    return SemanticSettings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'semantic.db'}",
        service_token="service-secret",
        authorization_audience="semantic-platform",
        action_worker_poll_seconds=0.1,
        action_lease_seconds=30,
        evals_evidence_enabled=True,
    )


def _settings_with_scope_resolver(tmp_path):
    return SemanticSettings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'semantic.db'}",
        service_token="service-secret",
        authorization_audience="semantic-platform",
        action_worker_poll_seconds=0.1,
        action_lease_seconds=30,
        evals_evidence_enabled=True,
        scope_resolver_url="http://iam.internal/v1/scopes/resolve",
        scope_resolver_token="scope-service-secret",
    )


def _headers(token=None):
    return {
        "X-DeerFlow-Semantic-Token": "service-secret",
        "X-SaaS-Authorization-Context": token or _token(),
        "X-DeerFlow-Run-Id": "run-1",
        "X-DeerFlow-Thread-Id": "thread-1",
        "X-DeerFlow-Tool-Call-Id": "tool-1",
        "X-DeerFlow-Semantic-Trace-Id": "semantic-trace-1",
    }


def _approval_token(*, proposal_id, scope_hash, issuer="saas-gateway"):
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": "semantic-action-approval",
            "sub": "public-user-001",
            "proposal_id": proposal_id,
            "scope_hash": scope_hash,
            "approved_by": "approver-1",
            "iat": now,
            "exp": now + 300,
            "jti": f"approval-{proposal_id}",
        },
        SECRET,
        algorithm="HS256",
    )


def test_action_approval_rejects_wrong_issuer(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", SECRET)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    app = create_app(settings=_settings(tmp_path), ontology=_ontology(), sql_policy=_policy())
    runtime = MagicMock()
    runtime.get_object.return_value = SimpleNamespace(rows=[{"id": "site-demo-001", "name": "Old"}])
    with TestClient(app) as client:
        app.state.semantic_runtime = runtime
        proposal = client.post(
            "/v1/actions/proposals",
            headers={**_headers(), "Idempotency-Key": "approval-issuer"},
            json={
                "action_id": "site.rename",
                "target_id": "site-demo-001",
                "parameters": {"name": "New"},
            },
        )
        assert proposal.status_code == 200
        proposal_id = proposal.json()["proposal_id"]
        scope_hash = proposal.json()["scope_hash"]
        preview = client.post(
            f"/v1/actions/proposals/{proposal_id}/preview",
            headers=_headers(),
        )
        assert preview.status_code == 200

        approved = client.post(
            f"/v1/actions/proposals/{proposal_id}/approve",
            headers=_headers(),
            json={
                "approval_token": _approval_token(
                    proposal_id=proposal_id,
                    scope_hash=scope_hash,
                    issuer="untrusted-issuer",
                )
            },
        )

    assert approved.status_code == 403
    assert approved.json()["detail"]["code"] == "AUTHORIZATION_DENIED"


def test_semantic_api_requires_both_service_and_user_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", SECRET)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    app = create_app(settings=_settings(tmp_path), ontology=_ontology(), sql_policy=_policy())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/v1/metrics/site.count")
        assert response.status_code == 401
        response = client.get(
            "/v1/metrics/site.count",
            headers={"X-DeerFlow-Semantic-Token": "service-secret", "X-SaaS-Authorization-Context": _token(aud="deerflow")},
        )
        assert response.status_code == 401

        response = client.get(
            "/v1/metrics/site.count",
            headers={
                "X-DeerFlow-Semantic-Token": "service-secret",
                "X-SaaS-Authorization-Context": _token(),
            },
        )
        assert response.status_code == 400

        response = client.post(
            "/v1/queries",
            headers=_headers(),
            json={"metrics": ["site.count", "site.count"]},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "INVALID_SEMANTIC_QUERY"

        unknown = client.get("/v1/metrics/unknown.metric", headers=_headers())
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "ONTOLOGY_NOT_FOUND"


def test_semantic_api_query_and_action_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", SECRET)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    monkeypatch.setenv("SAAS_ACTION_APPROVAL_JWT_KEY", SECRET)
    app = create_app(settings=_settings(tmp_path), ontology=_ontology(), sql_policy=_policy())
    runtime = MagicMock()
    runtime.query_metrics.return_value.to_dict.return_value = {
        "rows": [{"site.count": 1}],
        "columns": ["site.count"],
        "scope_hash": "hash",
    }
    runtime.get_object.return_value = SimpleNamespace(rows=[{"id": "site-demo-001", "name": "Old"}])
    with TestClient(app) as client:
        app.state.semantic_runtime = runtime
        query = client.post("/v1/queries", headers=_headers(), json={"metrics": ["site.count"]})
        assert query.status_code == 200
        assert query.json()["rows"] == [{"site.count": 1}]
        assert query.json()["semantic_trace_id"] == "semantic-trace-1"

        actions = client.get("/v1/actions", headers=_headers())
        assert actions.status_code == 200
        assert [item["id"] for item in actions.json()["actions"]] == ["site.rename"]

        proposal = client.post(
            "/v1/actions/proposals",
            headers={**_headers(), "Idempotency-Key": "key-1"},
            json={"action_id": "site.rename", "target_id": "site-demo-001", "parameters": {"name": "New"}},
        )
        assert proposal.status_code == 200
        proposal_id = proposal.json()["proposal_id"]
        scope_hash = proposal.json()["scope_hash"]
        preview = client.post(f"/v1/actions/proposals/{proposal_id}/preview", headers=_headers())
        assert preview.status_code == 200
        blocked = client.post(f"/v1/actions/proposals/{proposal_id}/execute", headers=_headers())
        assert blocked.status_code == 409
        approved = client.post(
            f"/v1/actions/proposals/{proposal_id}/approve",
            headers=_headers(),
            json={"approval_token": _approval_token(proposal_id=proposal_id, scope_hash=scope_hash)},
        )
        assert approved.status_code == 200
        assert approved.json()["approved_by"] == "approver-1"
        execution = client.post(f"/v1/actions/proposals/{proposal_id}/execute", headers=_headers())
        assert execution.status_code == 200
        status = client.get(
            f"/v1/actions/executions/{execution.json()['execution_id']}",
            headers=_headers(),
        )
        assert status.json()["status"] == "READY"


def test_semantic_api_resolves_scope_ref_with_fresh_signed_token(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", SECRET)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    now = int(time.time())
    resolved_token = _token(
        scope={"mode": "resource_set", "site_ids": ["site-demo-001"], "project_ids": []},
        jti="resolved-1",
        iat=now,
        exp=now + 300,
    )
    captured = {}

    async def resolve_scope(request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"authorization_token": resolved_token})

    app = create_app(
        settings=_settings_with_scope_resolver(tmp_path),
        ontology=_ontology(),
        sql_policy=_policy(),
    )
    runtime = MagicMock()
    runtime.query_metrics.return_value.to_dict.return_value = {"rows": [{"site.count": 1}]}
    scope_ref_token = _token(
        scope={"mode": "scope_ref", "scope_ref": "scope-large-1"},
        jti="scope-ref-1",
    )
    with TestClient(app) as client:
        app.state.semantic_runtime = runtime
        app.state.scope_resolver_transport = httpx.MockTransport(resolve_scope)
        response = client.post(
            "/v1/queries",
            headers=_headers(scope_ref_token),
            json={"metrics": ["site.count"]},
        )

    assert response.status_code == 200
    authorization = runtime.query_metrics.call_args.kwargs["authorization"]
    assert authorization.scope_mode == "resource_set"
    assert authorization.allowed_site_ids == ("site-demo-001",)
    assert captured["headers"]["x-saas-internal-token"] == "scope-service-secret"


def test_ontology_resolve_reports_denied_action_without_disclosing_definition(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", SECRET)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    ontology = OntologyRegistry.from_mapping(
        {
            "version": "1",
            "objects": {
                "Site": {
                    "table": "demo_sites",
                    "id_field": "id",
                    "properties": {"id": {"column": "id", "type": "string"}},
                }
            },
            "links": {},
            "metrics": {},
            "actions": {
                "site.secret_rename": {
                    "label": "rename site display name",
                    "keywords": ["rename", "display name"],
                    "target_type": "Site",
                    "scope_dimension": "site",
                    "authorization": {"allowed_roles": ["site_admin"]},
                    "parameters": {"name": {"type": "string", "required": True}},
                    "approval": {"required": True},
                    "executor": {"type": "domain_api", "path": "/sites/{target_id}"},
                }
            },
        }
    )
    app = create_app(settings=_settings(tmp_path), ontology=ontology, sql_policy=_policy())
    viewer_token = _token(role_codes=["viewer"])
    with TestClient(app) as client:
        response = client.post(
            "/v1/ontology/resolve",
            headers=_headers(viewer_token),
            json={"question": "rename site display name", "include_facts": False},
        )
        trace = client.get("/v1/audit/traces/semantic-trace-1", headers=_headers(viewer_token))

    assert response.status_code == 200
    payload = response.json()
    assert payload["actions"] == []
    assert payload["action_authorization"] == {
        "status": "denied",
        "code": "AUTHORIZATION_DENIED",
    }
    assert "site.secret_rename" not in str(payload)
    assert trace.status_code == 200
    assert trace.json()["events"][0]["decision"] == "deny"
    assert trace.json()["events"][0]["details"]["action_decision"]["code"] == "AUTHORIZATION_DENIED"


def test_semantic_api_rejects_scope_ref_resolution_to_tenant_all(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", SECRET)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    resolved_token = _token(
        scope={"mode": "tenant_all"},
        jti="resolved-tenant-all",
    )

    async def resolve_scope(_request):
        return httpx.Response(200, json={"authorization_token": resolved_token})

    app = create_app(
        settings=_settings_with_scope_resolver(tmp_path),
        ontology=_ontology(),
        sql_policy=_policy(),
    )
    runtime = MagicMock()
    scope_ref_token = _token(
        scope={"mode": "scope_ref", "scope_ref": "scope-large-1"},
        jti="scope-ref-no-escalation",
    )
    with TestClient(app) as client:
        app.state.semantic_runtime = runtime
        app.state.scope_resolver_transport = httpx.MockTransport(resolve_scope)
        response = client.post(
            "/v1/queries",
            headers=_headers(scope_ref_token),
            json={"metrics": ["site.count"]},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "SCOPE_CHANGED"
    runtime.query_metrics.assert_not_called()


def test_semantic_audit_evidence_endpoint_returns_only_matching_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", SECRET)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    app = create_app(settings=_settings(tmp_path), ontology=_ontology(), sql_policy=_policy())
    runtime = MagicMock()
    runtime.query_metrics.return_value.to_dict.return_value = {
        "rows": [{"site.count": 1}],
        "ontology_version": "1",
        "policy_version": "1",
        "scope_hash": "ignored",
    }
    with TestClient(app) as client:
        app.state.semantic_runtime = runtime
        query = client.post(
            "/v1/queries",
            headers=_headers(),
            json={"metrics": ["site.count"]},
        )
        assert query.status_code == 200

        own = client.get("/v1/audit/traces/semantic-trace-1", headers=_headers())
        changed_scope_token = _token(
            scope={"mode": "resource_set", "site_ids": ["site-demo-002"], "project_ids": []},
            jti="changed-scope",
        )
        other_scope = client.get(
            "/v1/audit/traces/semantic-trace-1",
            headers=_headers(changed_scope_token),
        )

    assert own.status_code == 200
    assert own.json()["semantic_trace_id"] == "semantic-trace-1"
    assert own.json()["events"][0]["event_type"] == "metric.query"
    assert own.json()["events"][0]["run_id"] == "run-1"
    assert other_scope.status_code == 404


def test_action_evidence_endpoint_includes_owned_transitions(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", SECRET)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    app = create_app(settings=_settings(tmp_path), ontology=_ontology(), sql_policy=_policy())
    runtime = MagicMock()
    runtime.get_object.return_value = SimpleNamespace(rows=[{"id": "site-demo-001", "name": "Old"}])
    with TestClient(app) as client:
        app.state.semantic_runtime = runtime
        proposal = client.post(
            "/v1/actions/proposals",
            headers={**_headers(), "Idempotency-Key": "evidence-proposal"},
            json={"action_id": "site.rename", "target_id": "site-demo-001", "parameters": {"name": "New"}},
        )
        assert proposal.status_code == 200
        proposal_id = proposal.json()["proposal_id"]

        evidence = client.get(
            f"/v1/actions/proposals/{proposal_id}/evidence",
            headers=_headers(),
        )
        changed_scope_token = _token(
            scope={"mode": "resource_set", "site_ids": ["site-demo-002"], "project_ids": []},
            jti="changed-action-scope",
        )
        hidden = client.get(
            f"/v1/actions/proposals/{proposal_id}/evidence",
            headers=_headers(changed_scope_token),
        )

    assert evidence.status_code == 200
    assert evidence.json()["proposal"]["proposal_id"] == proposal_id
    assert evidence.json()["proposal_transitions"] == ["PROPOSED"]
    assert evidence.json()["execution"] is None
    assert hidden.status_code == 404


def test_evals_evidence_endpoints_are_hidden_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", SECRET)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    app = create_app(
        settings=replace(_settings(tmp_path), evals_evidence_enabled=False),
        ontology=_ontology(),
        sql_policy=_policy(),
    )

    with TestClient(app) as client:
        response = client.get("/v1/audit/traces/trace-1", headers=_headers())

    assert response.status_code == 404
