"""Tests for Gateway internal auth token handling."""

from __future__ import annotations

import importlib
import time
from types import SimpleNamespace

import jwt
import pytest


def test_internal_auth_uses_shared_env_token(monkeypatch):
    import app.gateway.internal_auth as internal_auth

    monkeypatch.setenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", "shared-token")
    reloaded = importlib.reload(internal_auth)
    try:
        headers = reloaded.create_internal_auth_headers()

        assert headers[reloaded.INTERNAL_AUTH_HEADER_NAME] == "shared-token"
        assert reloaded.is_valid_internal_auth_token("shared-token") is True
        assert reloaded.is_valid_internal_auth_token("other-token") is False
    finally:
        monkeypatch.delenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", raising=False)
        importlib.reload(reloaded)


def test_internal_auth_generates_process_local_fallback(monkeypatch):
    import app.gateway.internal_auth as internal_auth

    monkeypatch.delenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", raising=False)
    reloaded = importlib.reload(internal_auth)
    try:
        token = reloaded.create_internal_auth_headers()[reloaded.INTERNAL_AUTH_HEADER_NAME]

        assert token
        assert reloaded.is_valid_internal_auth_token(token) is True
    finally:
        importlib.reload(reloaded)


def test_internal_auth_headers_can_carry_owner_user_id(monkeypatch):
    import app.gateway.internal_auth as internal_auth

    monkeypatch.setenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", "shared-token")
    reloaded = importlib.reload(internal_auth)
    try:
        headers = reloaded.create_internal_auth_headers(owner_user_id="owner-1")

        assert headers[reloaded.INTERNAL_AUTH_HEADER_NAME] == "shared-token"
        assert headers[reloaded.INTERNAL_OWNER_USER_ID_HEADER_NAME] == "owner-1"
    finally:
        monkeypatch.delenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", raising=False)
        importlib.reload(reloaded)


def test_get_internal_user_normalises_unsafe_owner_user_id():
    """P2-3: X-DeerFlow-Owner-User-Id is at the trust boundary, so the
    synthetic internal user must use a path-safe id. ``make_safe_user_id``
    is lossy but deterministic; two distinct raw inputs never collide.
    """
    import app.gateway.internal_auth as internal_auth
    from deerflow.config.paths import make_safe_user_id

    # Path-traversal-style payloads must be normalised away.
    user_a = internal_auth.get_internal_user(owner_user_id="ou_abc/../../etc/passwd")
    user_b = internal_auth.get_internal_user(owner_user_id="ou_abc/../../etc/passwd")
    assert user_a.id == user_b.id
    assert "/" not in user_a.id
    assert ".." not in user_a.id

    # Negative chat ids and unsafe punctuation must be normalised.
    user_neg = internal_auth.get_internal_user(owner_user_id="-1001234567890:alice")
    assert user_neg.id == make_safe_user_id("-1001234567890:alice")
    assert ":" not in user_neg.id
    assert user_neg.system_role == "internal"

    # Already-safe ids pass through unchanged.
    user_safe = internal_auth.get_internal_user(owner_user_id="alice_42")
    assert user_safe.id == "alice_42"

    # Empty / None falls back to default.
    assert internal_auth.get_internal_user().id == "default"
    assert internal_auth.get_internal_user(owner_user_id="").id == "default"


def test_get_trusted_saas_context_accepts_internal_headers():
    from app.gateway import internal_auth

    request = SimpleNamespace(
        headers={
            internal_auth.SAAS_TENANT_ID_HEADER_NAME: "public-tenant-001",
            internal_auth.SAAS_TENANT_CODE_HEADER_NAME: "public_demo",
            internal_auth.SAAS_TENANT_NAME_HEADER_NAME: "Public Demo Tenant",
            internal_auth.SAAS_SYSTEM_CODE_HEADER_NAME: "demo",
        },
        state=SimpleNamespace(user=SimpleNamespace(system_role=internal_auth.INTERNAL_SYSTEM_ROLE)),
    )

    assert internal_auth.get_trusted_saas_context(request) == {
        "tenant_id": "public-tenant-001",
        "tenant_code": "public_demo",
        "tenant_name": "Public Demo Tenant",
        "system_code": "demo",
    }


def test_get_trusted_saas_context_ignores_non_internal_user():
    from app.gateway import internal_auth

    request = SimpleNamespace(
        headers={
            internal_auth.SAAS_TENANT_ID_HEADER_NAME: "public-tenant-001",
            internal_auth.SAAS_TENANT_CODE_HEADER_NAME: "public_demo",
            internal_auth.SAAS_SYSTEM_CODE_HEADER_NAME: "demo",
        },
        state=SimpleNamespace(user=SimpleNamespace(system_role="user")),
    )

    assert internal_auth.get_trusted_saas_context(request) == {}


def test_get_trusted_saas_context_rejects_unsafe_required_headers():
    from app.gateway import internal_auth

    request = SimpleNamespace(
        headers={
            internal_auth.SAAS_TENANT_ID_HEADER_NAME: "public-tenant-001",
            internal_auth.SAAS_TENANT_CODE_HEADER_NAME: "public_demo;DROP",
            internal_auth.SAAS_SYSTEM_CODE_HEADER_NAME: "demo",
        },
        state=SimpleNamespace(user=SimpleNamespace(system_role=internal_auth.INTERNAL_SYSTEM_ROLE)),
    )

    assert internal_auth.get_trusted_saas_context(request) == {}


def _authorization_token(monkeypatch, **overrides):
    from app.gateway import internal_auth

    test_secret = "test-secret-at-least-thirty-two-bytes"
    monkeypatch.setenv(internal_auth.SAAS_AUTHORIZATION_JWT_KEY_ENV_VAR, test_secret)
    monkeypatch.setenv(internal_auth.SAAS_AUTHORIZATION_JWT_ALGORITHMS_ENV_VAR, "HS256")
    now = int(time.time())
    claims = {
        "iss": "saas-gateway",
        "aud": ["deerflow", "semantic-platform"],
        "sub": "public-user-001",
        "tenant_id": "public-tenant-001",
        "tenant_code": "public_demo",
        "tenant_name": "Public Demo Tenant",
        "system_code": "demo",
        "role_codes": ["site_admin"],
        "scope": {"mode": "resource_set", "site_ids": ["site-demo-001"], "project_ids": []},
        "permission_version": "42",
        "iat": now,
        "exp": now + 300,
        "jti": "authz-1",
    }
    claims.update(overrides)
    return jwt.encode(claims, test_secret, algorithm="HS256")


def test_get_trusted_saas_authorization_context_verifies_and_normalizes(monkeypatch):
    from app.gateway import internal_auth

    token = _authorization_token(monkeypatch)
    request = SimpleNamespace(
        headers={internal_auth.SAAS_AUTHORIZATION_CONTEXT_HEADER_NAME: token},
        state=SimpleNamespace(user=SimpleNamespace(system_role=internal_auth.INTERNAL_SYSTEM_ROLE)),
    )

    context = internal_auth.get_trusted_saas_authorization_context(request)

    assert context is not None
    assert context.principal_id == "public-user-001"
    assert context.allowed_site_ids == ("site-demo-001",)
    assert internal_auth.get_trusted_saas_authorization_token(request) == token
    assert internal_auth.get_trusted_saas_context(request) == {
        "tenant_id": "public-tenant-001",
        "tenant_code": "public_demo",
        "tenant_name": "Public Demo Tenant",
        "system_code": "demo",
    }


def test_get_trusted_saas_authorization_context_rejects_expired_token(monkeypatch):
    from app.gateway import internal_auth

    token = _authorization_token(monkeypatch, exp=int(time.time()) - 1)
    request = SimpleNamespace(
        headers={internal_auth.SAAS_AUTHORIZATION_CONTEXT_HEADER_NAME: token},
        state=SimpleNamespace(user=SimpleNamespace(system_role=internal_auth.INTERNAL_SYSTEM_ROLE)),
    )

    with pytest.raises(internal_auth.SaasAuthorizationError):
        internal_auth.get_trusted_saas_authorization_context(request)


def test_get_trusted_saas_authorization_context_rejects_excessive_ttl(monkeypatch):
    from app.gateway import internal_auth

    now = int(time.time())
    token = _authorization_token(monkeypatch, iat=now, exp=now + 3600)
    request = SimpleNamespace(
        headers={internal_auth.SAAS_AUTHORIZATION_CONTEXT_HEADER_NAME: token},
        state=SimpleNamespace(user=SimpleNamespace(system_role=internal_auth.INTERNAL_SYSTEM_ROLE)),
    )

    with pytest.raises(internal_auth.SaasAuthorizationError, match="lifetime"):
        internal_auth.get_trusted_saas_authorization_context(request)


def test_get_trusted_saas_authorization_token_ignores_non_internal_user(monkeypatch):
    from app.gateway import internal_auth

    token = _authorization_token(monkeypatch)
    request = SimpleNamespace(
        headers={internal_auth.SAAS_AUTHORIZATION_CONTEXT_HEADER_NAME: token},
        state=SimpleNamespace(user=SimpleNamespace(system_role="user")),
    )

    assert internal_auth.get_trusted_saas_authorization_token(request) is None
