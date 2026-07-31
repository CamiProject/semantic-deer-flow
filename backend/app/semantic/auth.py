"""Service-to-service and end-user authorization for semantic APIs."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx
from fastapi import Header, HTTPException, Request

from app.auth.saas_authorization import SaasAuthorizationError, decode_authorization_token
from app.semantic.config import SemanticSettings
from deerflow.runtime.authorization_context import AuthorizationContext

SERVICE_TOKEN_HEADER = "X-DeerFlow-Semantic-Token"
AUTHORIZATION_CONTEXT_HEADER = "X-SaaS-Authorization-Context"


def _settings(request: Request) -> SemanticSettings:
    return request.app.state.settings


async def _resolve_scope_ref(
    request: Request,
    *,
    original_token: str,
    authorization: AuthorizationContext,
) -> AuthorizationContext:
    settings = _settings(request)
    parsed = urlsplit(settings.scope_resolver_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or not settings.scope_resolver_token:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "POLICY_UNAVAILABLE",
                "message": "SaaS scope resolver is not configured",
            },
        )
    transport = getattr(request.app.state, "scope_resolver_transport", None)
    try:
        async with httpx.AsyncClient(
            timeout=15,
            transport=transport,
            trust_env=False,
        ) as client:
            response = await client.post(
                settings.scope_resolver_url,
                headers={
                    "X-SaaS-Internal-Token": settings.scope_resolver_token,
                    AUTHORIZATION_CONTEXT_HEADER: original_token,
                },
                json={
                    "scope_ref": authorization.scope_ref,
                    "principal_id": authorization.principal_id,
                    "tenant_id": authorization.tenant_id,
                    "system_code": authorization.system_code,
                    "permission_version": authorization.permission_version,
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "POLICY_UNAVAILABLE",
                "message": "SaaS scope resolver is unavailable",
            },
        ) from exc
    if response.status_code >= 500:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "POLICY_UNAVAILABLE",
                "message": "SaaS scope resolver is unavailable",
            },
        )
    if response.is_error:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AUTHORIZATION_DENIED",
                "message": "SaaS scope resolution was denied",
            },
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "POLICY_UNAVAILABLE",
                "message": "SaaS scope resolver returned an invalid response",
            },
        ) from exc
    resolved_token = payload.get("authorization_token") if isinstance(payload, Mapping) else None
    if not isinstance(resolved_token, str) or not resolved_token:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "POLICY_UNAVAILABLE",
                "message": "SaaS scope resolver returned no authorization token",
            },
        )
    try:
        resolved, _tenant_name = decode_authorization_token(
            resolved_token,
            audience=settings.authorization_audience,
        )
    except SaasAuthorizationError as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTHENTICATION_FAILED",
                "message": "Resolved SaaS authorization context is invalid",
            },
        ) from exc
    if (
        resolved.principal_id != authorization.principal_id
        or resolved.tenant_id != authorization.tenant_id
        or resolved.tenant_code != authorization.tenant_code
        or resolved.system_code != authorization.system_code
        or resolved.permission_version != authorization.permission_version
        or resolved.scope_mode != "resource_set"
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SCOPE_CHANGED",
                "message": "Resolved SaaS scope does not match the signed reference",
            },
        )
    return resolved


async def require_semantic_authorization(
    request: Request,
    service_token: str | None = Header(default=None, alias=SERVICE_TOKEN_HEADER),
    authorization_token: str | None = Header(default=None, alias=AUTHORIZATION_CONTEXT_HEADER),
) -> AuthorizationContext:
    settings = _settings(request)
    if not service_token or not secrets.compare_digest(service_token, settings.service_token):
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTHENTICATION_FAILED", "message": "Invalid semantic service token"},
        )
    if not authorization_token:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTHENTICATION_FAILED", "message": "Missing SaaS authorization context"},
        )
    try:
        authorization, _tenant_name = decode_authorization_token(
            authorization_token,
            audience=settings.authorization_audience,
        )
    except SaasAuthorizationError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTHENTICATION_FAILED", "message": "Invalid SaaS authorization context"},
        ) from exc
    if authorization.scope_mode == "scope_ref":
        authorization = await _resolve_scope_ref(
            request,
            original_token=authorization_token,
            authorization=authorization,
        )
    if authorization.scope_mode == "none":
        raise HTTPException(
            status_code=403,
            detail={"code": "AUTHORIZATION_DENIED", "message": "Authorization scope is not executable"},
        )
    return authorization
