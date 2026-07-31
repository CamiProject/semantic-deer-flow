import time

import jwt
import pytest

from app.auth.saas_authorization import SaasAuthorizationError, decode_authorization_token
from app.semantic.approval import verify_action_approval
from app.semantic.config import get_semantic_settings
from deerflow.config.app_config import AppConfig


def _authorization_claims() -> dict[str, object]:
    now = int(time.time())
    return {
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
        "jti": "eval-auth-1",
    }


def test_evals_config_is_disabled_and_fail_closed_by_default():
    config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}})

    assert config.evals.enabled is False
    assert config.evals.max_concurrency == 3
    assert config.evals.evidence.require_persistent_run_events is True
    assert config.evals.gate.fail_on_any_p0 is True


def test_evals_config_accepts_bounded_runner_settings():
    config = AppConfig.model_validate(
        {
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "evals": {
                "enabled": True,
                "output_dir": "eval-results",
                "max_concurrency": 2,
                "default_timeout_seconds": 90,
            },
        }
    )

    assert config.evals.enabled is True
    assert config.evals.output_dir == "eval-results"
    assert config.evals.max_concurrency == 2


@pytest.mark.parametrize(
    ("environment", "enabled"),
    [("production", False), ("eval", True)],
)
def test_semantic_evidence_requires_eval_environment(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    enabled: bool,
):
    monkeypatch.setenv("DEER_FLOW_ENV", environment)
    monkeypatch.setenv("DEER_FLOW_SEMANTIC_DATABASE_URL", "sqlite+aiosqlite:///eval.db")
    monkeypatch.setenv("DEER_FLOW_SEMANTIC_SERVICE_TOKEN", "semantic-token")
    monkeypatch.setenv("DEER_FLOW_EVALS_EVIDENCE_ENABLED", "true")

    assert get_semantic_settings().evals_evidence_enabled is enabled


def test_eval_environment_verifies_with_dedicated_eval_key(monkeypatch: pytest.MonkeyPatch):
    eval_key = "eval-authorization-key-at-least-32-bytes"
    monkeypatch.setenv("DEER_FLOW_ENV", "eval")
    monkeypatch.setenv("EVALS_AUTHORIZATION_JWT_KEY", eval_key)
    monkeypatch.setenv("EVALS_AUTHORIZATION_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", "production-public-key")
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "RS256")
    token = jwt.encode(_authorization_claims(), eval_key, algorithm="HS256")

    authorization, _tenant_name = decode_authorization_token(token)

    assert authorization.principal_id == "public-user-001"


def test_non_eval_environment_never_trusts_dedicated_eval_key(monkeypatch: pytest.MonkeyPatch):
    production_key = "production-authorization-key-at-least-32-bytes"
    eval_key = "eval-authorization-key-at-least-32-bytes"
    monkeypatch.setenv("DEER_FLOW_ENV", "production")
    monkeypatch.setenv("EVALS_AUTHORIZATION_JWT_KEY", eval_key)
    monkeypatch.setenv("EVALS_AUTHORIZATION_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", production_key)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    token = jwt.encode(_authorization_claims(), eval_key, algorithm="HS256")

    with pytest.raises(SaasAuthorizationError, match="Invalid SaaS authorization context"):
        decode_authorization_token(token)


def test_eval_action_approval_uses_dedicated_eval_key(monkeypatch: pytest.MonkeyPatch):
    eval_key = "eval-authorization-key-at-least-32-bytes"
    monkeypatch.setenv("DEER_FLOW_ENV", "eval")
    monkeypatch.setenv("EVALS_AUTHORIZATION_JWT_KEY", eval_key)
    monkeypatch.setenv("EVALS_AUTHORIZATION_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", "production-public-key")
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "RS256")
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "saas-gateway",
            "aud": "semantic-action-approval",
            "sub": "public-user-001",
            "proposal_id": "proposal-1",
            "scope_hash": "scope-1",
            "approved_by": "eval-runner",
            "iat": now,
            "exp": now + 300,
            "jti": "eval-approval-1",
        },
        eval_key,
        algorithm="HS256",
    )

    approved_by = verify_action_approval(
        token,
        proposal_id="proposal-1",
        principal_id="public-user-001",
        scope_hash="scope-1",
    )

    assert approved_by == "eval-runner"
