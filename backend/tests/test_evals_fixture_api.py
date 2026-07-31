from __future__ import annotations

import jwt
from fastapi.testclient import TestClient

from app.evals.fixture_api import EvalFixtureSettings, create_fixture_app

JWT_KEY = "eval-jwt-secret-at-least-thirty-two-bytes"


def _settings() -> EvalFixtureSettings:
    return EvalFixtureSettings(
        environment="eval",
        control_token="fixture-control-secret",
        saas_internal_token="fixture-worker-secret",
        authorization_jwt_key=JWT_KEY,
        authorization_algorithm="HS256",
        authorization_issuer="saas-gateway",
        action_worker_audience="action-worker",
    )


def _reset_payload(
    trial_id: str,
    thread_id: str,
    *,
    scenario: str = "site_display_name_old",
    expected_after: dict | None = None,
) -> dict:
    return {
        "eval_run_id": "eval-run-1",
        "trial_id": trial_id,
        "thread_id": thread_id,
        "case_id": "action-site-rename",
        "scenario": scenario,
        "tenant_id": "public-tenant-001",
        "tenant_code": "public_demo",
        "tenant_name": "Eval Tenant",
        "principal_id": "public-user-001",
        "system_code": "demo",
        "permission_version": "1",
        "role_codes": ["site_admin"],
        "scope": {"mode": "resource_set", "site_ids": ["site-demo-001"], "project_ids": []},
        "scope_hash": "scope-hash-1",
        "expected_after": expected_after or {},
    }


def _control_headers() -> dict[str, str]:
    return {"X-Evals-Token": "fixture-control-secret"}


def _worker_headers(**extra: str) -> dict[str, str]:
    return {"X-SaaS-Internal-Token": "fixture-worker-secret", **extra}


def test_fixture_trials_are_isolated_and_domain_writes_are_idempotent():
    app = create_fixture_app(settings=_settings())
    with TestClient(app) as client:
        for trial_id, thread_id in (("trial-1", "thread-1"), ("trial-2", "thread-2")):
            response = client.post(
                f"/v1/evals/trials/{trial_id}/reset",
                headers=_control_headers(),
                json=_reset_payload(
                    trial_id,
                    thread_id,
                    expected_after={
                        "sites": {
                            "site-demo-001": {
                                "display_name": "New Display Name",
                                "version": "2",
                            }
                        }
                    },
                ),
            )
            assert response.status_code == 200

        domain_headers = _worker_headers(
            **{
                "Idempotency-Key": "idem-1",
                "X-DeerFlow-Thread-Id": "thread-1",
                "X-DeerFlow-Run-Id": "run-1",
            }
        )
        body = {
            "target_type": "Site",
            "target_id": "site-demo-001",
            "parameters": {"name": "New Display Name"},
            "actor": "public-user-001",
            "scope_hash": "scope-hash-1",
            "action_id": "site.update_display_name",
            "action_version": "1",
        }
        first = client.patch(
            "/api/internal/sites/site-demo-001/display-name",
            headers=domain_headers,
            json=body,
        )
        repeated = client.patch(
            "/api/internal/sites/site-demo-001/display-name",
            headers=domain_headers,
            json=body,
        )
        changed = client.get(
            "/v1/evals/trials/trial-1/state",
            headers=_control_headers(),
        )
        untouched = client.get(
            "/v1/evals/trials/trial-2/state",
            headers=_control_headers(),
        )

    assert first.status_code == 200
    assert repeated.json() == first.json()
    assert changed.json()["state"]["sites"]["site-demo-001"]["display_name"] == "New Display Name"
    assert changed.json()["state"]["sites"]["site-demo-001"]["version"] == "2"
    assert changed.json()["unexpected_changes"] == []
    assert untouched.json()["state"]["sites"]["site-demo-001"]["display_name"] == "Old Display Name"


def test_fixture_reports_state_changes_not_declared_by_case_expectation():
    app = create_fixture_app(settings=_settings())
    with TestClient(app) as client:
        client.post(
            "/v1/evals/trials/trial-1/reset",
            headers=_control_headers(),
            json=_reset_payload("trial-1", "thread-1", expected_after={}),
        )
        client.patch(
            "/api/internal/sites/site-demo-001/display-name",
            headers=_worker_headers(
                **{
                    "Idempotency-Key": "idem-unexpected",
                    "X-DeerFlow-Thread-Id": "thread-1",
                }
            ),
            json={
                "target_type": "Site",
                "target_id": "site-demo-001",
                "parameters": {"name": "Unexpected Name"},
                "actor": "public-user-001",
                "scope_hash": "scope-hash-1",
                "action_id": "site.update_display_name",
                "action_version": "1",
            },
        )
        state = client.get(
            "/v1/evals/trials/trial-1/state",
            headers=_control_headers(),
        )

    assert state.json()["unexpected_changes"] == [
        "sites.site-demo-001.display_name",
        "sites.site-demo-001.version",
    ]


def test_fixture_iam_revalidation_issues_fresh_action_worker_token_and_can_deny():
    app = create_fixture_app(settings=_settings())
    with TestClient(app) as client:
        reset = client.post(
            "/v1/evals/trials/trial-1/reset",
            headers=_control_headers(),
            json=_reset_payload("trial-1", "thread-1"),
        )
        assert reset.status_code == 200
        body = {
            "principal_id": "public-user-001",
            "tenant_id": "public-tenant-001",
            "system_code": "demo",
            "permission_version": "1",
            "scope_hash": "scope-hash-1",
            "action_id": "site.update_display_name",
            "action_version": "1",
            "target_type": "Site",
            "target_id": "site-demo-001",
        }
        allowed = client.post(
            "/api/authorization/actions/revalidate",
            headers=_worker_headers(**{"X-DeerFlow-Thread-Id": "thread-1"}),
            json=body,
        )
        client.post(
            "/v1/evals/trials/trial-denied/reset",
            headers=_control_headers(),
            json=_reset_payload("trial-denied", "thread-denied", scenario="action_iam_denied"),
        )
        denied = client.post(
            "/api/authorization/actions/revalidate",
            headers=_worker_headers(**{"X-DeerFlow-Thread-Id": "thread-denied"}),
            json=body,
        )

    assert allowed.status_code == 200
    claims = jwt.decode(
        allowed.json()["authorization_token"],
        JWT_KEY,
        algorithms=["HS256"],
        audience="action-worker",
        issuer="saas-gateway",
    )
    assert claims["sub"] == "public-user-001"
    assert claims["scope"]["site_ids"] == ["site-demo-001"]
    assert denied.status_code == 403


def test_fixture_rejects_non_eval_environment():
    try:
        EvalFixtureSettings(
            environment="production",
            control_token="control",
            saas_internal_token="worker",
            authorization_jwt_key=JWT_KEY,
        )
    except ValueError as exc:
        assert "DEER_FLOW_ENV=eval" in str(exc)
    else:
        raise AssertionError("production fixture configuration must fail closed")


def test_fixture_from_env_prefers_dedicated_eval_signing_key(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_ENV", "eval")
    monkeypatch.setenv("EVALS_FIXTURE_TOKEN", "fixture-control")
    monkeypatch.setenv("EVALS_SAAS_INTERNAL_TOKEN", "fixture-worker")
    monkeypatch.setenv("EVALS_AUTHORIZATION_JWT_KEY", "eval-signing-secret-at-least-32-bytes")
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", "production-verification-key-must-not-be-used")

    settings = EvalFixtureSettings.from_env()

    assert settings.authorization_jwt_key == "eval-signing-secret-at-least-32-bytes"
