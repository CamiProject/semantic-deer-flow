"""Authentication for trusted Gateway internal callers."""

from __future__ import annotations

import os
import re
import secrets
from types import SimpleNamespace
from typing import Any

from app.auth import saas_authorization as _saas_authorization
from deerflow.config.paths import make_safe_user_id
from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.runtime.user_context import DEFAULT_USER_ID

SAAS_AUTHORIZATION_JWT_ALGORITHMS_ENV_VAR = _saas_authorization.SAAS_AUTHORIZATION_JWT_ALGORITHMS_ENV_VAR
SAAS_AUTHORIZATION_JWT_AUDIENCE_ENV_VAR = _saas_authorization.SAAS_AUTHORIZATION_JWT_AUDIENCE_ENV_VAR
SAAS_AUTHORIZATION_JWT_ISSUER_ENV_VAR = _saas_authorization.SAAS_AUTHORIZATION_JWT_ISSUER_ENV_VAR
SAAS_AUTHORIZATION_JWT_KEY_ENV_VAR = _saas_authorization.SAAS_AUTHORIZATION_JWT_KEY_ENV_VAR
SaasAuthorizationError = _saas_authorization.SaasAuthorizationError
decode_authorization_token = _saas_authorization.decode_authorization_token

INTERNAL_AUTH_HEADER_NAME = "X-DeerFlow-Internal-Token"
INTERNAL_OWNER_USER_ID_HEADER_NAME = "X-DeerFlow-Owner-User-Id"
SAAS_TENANT_ID_HEADER_NAME = "X-SaaS-Tenant-Id"
SAAS_TENANT_CODE_HEADER_NAME = "X-SaaS-Tenant-Code"
SAAS_TENANT_NAME_HEADER_NAME = "X-SaaS-Tenant-Name"
SAAS_SYSTEM_CODE_HEADER_NAME = "X-SaaS-System-Code"
SAAS_AUTHORIZATION_CONTEXT_HEADER_NAME = "X-SaaS-Authorization-Context"
INTERNAL_AUTH_ENV_VAR = "DEER_FLOW_INTERNAL_AUTH_TOKEN"
INTERNAL_SYSTEM_ROLE = "internal"

_SAFE_HEADER_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _load_internal_auth_token() -> str:
    token = os.environ.get(INTERNAL_AUTH_ENV_VAR)
    if token:
        return token
    return secrets.token_urlsafe(32)


_INTERNAL_AUTH_TOKEN = _load_internal_auth_token()


def create_internal_auth_headers(*, owner_user_id: str | None = None) -> dict[str, str]:
    """Return headers that authenticate trusted Gateway internal calls."""
    headers = {INTERNAL_AUTH_HEADER_NAME: _INTERNAL_AUTH_TOKEN}
    if owner_user_id:
        headers[INTERNAL_OWNER_USER_ID_HEADER_NAME] = owner_user_id
    return headers


def is_valid_internal_auth_token(token: str | None) -> bool:
    """Return True when *token* matches this Gateway worker's internal token."""
    return bool(token) and secrets.compare_digest(token, _INTERNAL_AUTH_TOKEN)


def get_internal_user(owner_user_id: str | None = None):
    """Return the synthetic user used for trusted internal channel calls.

    When *owner_user_id* is provided (extracted from the
    ``X-DeerFlow-Owner-User-Id`` header), the synthetic user's ``.id``
    carries the actual channel owner instead of ``DEFAULT_USER_ID``.
    This ensures that ``get_effective_user_id()`` and downstream
    filesystem-path resolution (per-user custom skills, memory, thread
    data) use the correct identity for IM channel messages instead of
    falling back to ``"default"``.

    The owner id is normalized through :func:`make_safe_user_id` so that
    IM channel ids containing characters outside ``[A-Za-z0-9_-]`` (e.g.
    Feishu ``open_id`` prefixed with ``ou_`` and containing underscores
    that the rest of the system may treat as path separators, or
    Telegram chat ids like ``-1001234567890``) cannot be used to escape
    the per-user storage bucket or impersonate a different user via
    header value tricks (e.g. trailing slashes, ``..`` segments). The
    normalization is lossy but deterministic: two distinct raw inputs
    never share a safe id, so cross-user bleed is impossible.
    """
    if owner_user_id:
        effective_id = make_safe_user_id(owner_user_id)
    else:
        effective_id = DEFAULT_USER_ID
    return SimpleNamespace(id=effective_id, system_role=INTERNAL_SYSTEM_ROLE)


def get_trusted_internal_owner_user_id(request: Any) -> str | None:
    """Return the owner override for a trusted internal request, if present.

    The header is ignored for normal browser/API callers. It is only honored
    after ``AuthMiddleware`` has validated the internal auth token and stamped
    the synthetic internal user onto ``request.state.user``.
    """
    user = getattr(getattr(request, "state", None), "user", None)
    if getattr(user, "system_role", None) != INTERNAL_SYSTEM_ROLE:
        return None

    owner_user_id = request.headers.get(INTERNAL_OWNER_USER_ID_HEADER_NAME)
    if not owner_user_id:
        return None
    owner_user_id = owner_user_id.strip()
    return owner_user_id or None


def _clean_required_header(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value or not _SAFE_HEADER_VALUE_RE.fullmatch(value):
        return None
    return value


def _clean_optional_header(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value or None


def decode_saas_authorization_context(token: str) -> tuple[AuthorizationContext, str | None]:
    """Verify a short-lived SaaS authorization JWT and normalize its scope."""
    return decode_authorization_token(token)


def get_trusted_saas_authorization_context(request: Any) -> AuthorizationContext | None:
    """Return a verified authorization context for an internal SaaS request."""
    user = getattr(getattr(request, "state", None), "user", None)
    if getattr(user, "system_role", None) != INTERNAL_SYSTEM_ROLE:
        return None

    state = getattr(request, "state", None)
    cached = getattr(state, "saas_authorization_context", None)
    if isinstance(cached, AuthorizationContext):
        return cached

    token = request.headers.get(SAAS_AUTHORIZATION_CONTEXT_HEADER_NAME)
    if not token:
        return None
    context, tenant_name = decode_saas_authorization_context(token.strip())
    if state is not None:
        state.saas_authorization_context = context
        state.saas_tenant_name = tenant_name
    return context


def get_trusted_saas_authorization_token(request: Any) -> str | None:
    """Return the verified raw SaaS JWT for runtime-only semantic forwarding."""
    context = get_trusted_saas_authorization_context(request)
    if context is None:
        return None
    token = request.headers.get(SAAS_AUTHORIZATION_CONTEXT_HEADER_NAME)
    if not token:
        return None
    return token.strip() or None


def get_trusted_saas_context(request: Any) -> dict[str, str]:
    """Return trusted SaaS tenant context from internal Gateway headers.

    These headers are ignored for browser/API callers. They are honored only
    after ``AuthMiddleware`` validates ``X-DeerFlow-Internal-Token`` and stamps
    the synthetic internal user onto ``request.state.user``.
    """
    user = getattr(getattr(request, "state", None), "user", None)
    if getattr(user, "system_role", None) != INTERNAL_SYSTEM_ROLE:
        return {}

    authorization = get_trusted_saas_authorization_context(request)
    if authorization is not None:
        context = {
            "tenant_id": authorization.tenant_id,
            "tenant_code": authorization.tenant_code,
            "system_code": authorization.system_code,
        }
        tenant_name = getattr(getattr(request, "state", None), "saas_tenant_name", None)
        if tenant_name:
            context["tenant_name"] = tenant_name
        return context

    tenant_id = _clean_required_header(request.headers.get(SAAS_TENANT_ID_HEADER_NAME))
    tenant_code = _clean_required_header(request.headers.get(SAAS_TENANT_CODE_HEADER_NAME))
    system_code = _clean_required_header(request.headers.get(SAAS_SYSTEM_CODE_HEADER_NAME))
    if not tenant_id or not tenant_code or not system_code:
        return {}

    context = {
        "tenant_id": tenant_id,
        "tenant_code": tenant_code,
        "system_code": system_code,
    }
    tenant_name = _clean_optional_header(request.headers.get(SAAS_TENANT_NAME_HEADER_NAME))
    if tenant_name:
        context["tenant_name"] = tenant_name
    return context
