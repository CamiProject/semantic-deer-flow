"""Shared verification for short-lived SaaS authorization contexts."""

from __future__ import annotations

import os

import jwt

from deerflow.runtime.authorization_context import AuthorizationContext, AuthorizationContextError

SAAS_AUTHORIZATION_JWT_KEY_ENV_VAR = "SAAS_AUTHORIZATION_JWT_KEY"
SAAS_AUTHORIZATION_JWT_ALGORITHMS_ENV_VAR = "SAAS_AUTHORIZATION_JWT_ALGORITHMS"
SAAS_AUTHORIZATION_JWT_ISSUER_ENV_VAR = "SAAS_AUTHORIZATION_JWT_ISSUER"
SAAS_AUTHORIZATION_JWT_AUDIENCE_ENV_VAR = "SAAS_AUTHORIZATION_JWT_AUDIENCE"
SAAS_AUTHORIZATION_JWT_MAX_TTL_ENV_VAR = "SAAS_AUTHORIZATION_JWT_MAX_TTL_SECONDS"
EVALS_AUTHORIZATION_JWT_KEY_ENV_VAR = "EVALS_AUTHORIZATION_JWT_KEY"
EVALS_AUTHORIZATION_JWT_ALGORITHM_ENV_VAR = "EVALS_AUTHORIZATION_JWT_ALGORITHM"


class SaasAuthorizationError(ValueError):
    """Raised when a SaaS authorization token cannot be trusted."""


def jwt_settings(*, audience: str | None = None) -> tuple[str, list[str], str, str]:
    eval_key = os.environ.get(EVALS_AUTHORIZATION_JWT_KEY_ENV_VAR, "") if os.environ.get("DEER_FLOW_ENV", "").strip() == "eval" else ""
    if eval_key.strip():
        key = eval_key.replace("\\n", "\n").strip()
        algorithm_setting = os.environ.get(EVALS_AUTHORIZATION_JWT_ALGORITHM_ENV_VAR, "HS256")
    else:
        key = os.environ.get(SAAS_AUTHORIZATION_JWT_KEY_ENV_VAR, "").replace("\\n", "\n").strip()
        algorithm_setting = os.environ.get(SAAS_AUTHORIZATION_JWT_ALGORITHMS_ENV_VAR, "RS256")
    algorithms = [value.strip() for value in algorithm_setting.split(",") if value.strip()]
    issuer = os.environ.get(SAAS_AUTHORIZATION_JWT_ISSUER_ENV_VAR, "saas-gateway").strip()
    resolved_audience = audience or os.environ.get(SAAS_AUTHORIZATION_JWT_AUDIENCE_ENV_VAR, "deerflow").strip()
    if not key:
        raise SaasAuthorizationError(f"{SAAS_AUTHORIZATION_JWT_KEY_ENV_VAR} is not configured")
    if not algorithms or not issuer or not resolved_audience:
        raise SaasAuthorizationError("SaaS authorization JWT verification is not configured")
    return key, algorithms, issuer, resolved_audience


def decode_authorization_token(
    token: str,
    *,
    audience: str | None = None,
) -> tuple[AuthorizationContext, str | None]:
    """Verify a SaaS authorization JWT for the given service audience."""
    key, algorithms, issuer, resolved_audience = jwt_settings(audience=audience)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            issuer=issuer,
            audience=resolved_audience,
            options={
                "require": [
                    "exp",
                    "iat",
                    "iss",
                    "aud",
                    "sub",
                    "jti",
                    "tenant_id",
                    "tenant_code",
                    "system_code",
                    "permission_version",
                    "scope",
                ]
            },
        )
    except jwt.PyJWTError as exc:
        raise SaasAuthorizationError("Invalid SaaS authorization context") from exc

    try:
        issued_at = int(claims["iat"])
        expires_at = int(claims["exp"])
        max_ttl = int(os.environ.get(SAAS_AUTHORIZATION_JWT_MAX_TTL_ENV_VAR, "300"))
    except (KeyError, TypeError, ValueError) as exc:
        raise SaasAuthorizationError("Invalid SaaS authorization token lifetime") from exc
    if max_ttl <= 0 or expires_at <= issued_at or expires_at - issued_at > max_ttl:
        raise SaasAuthorizationError("SaaS authorization token lifetime exceeds policy")

    scope = claims.get("scope")
    if not isinstance(scope, dict):
        raise SaasAuthorizationError("Invalid SaaS authorization scope")
    try:
        context = AuthorizationContext.from_mapping(
            {
                "principal_id": claims.get("sub"),
                "tenant_id": claims.get("tenant_id"),
                "tenant_code": claims.get("tenant_code"),
                "system_code": claims.get("system_code"),
                "role_codes": claims.get("role_codes", []),
                "scope_mode": scope.get("mode"),
                "allowed_site_ids": scope.get("site_ids", []),
                "allowed_project_ids": scope.get("project_ids", []),
                "scope_ref": scope.get("scope_ref"),
                "permission_version": claims.get("permission_version"),
            }
        )
    except AuthorizationContextError as exc:
        raise SaasAuthorizationError("Invalid SaaS authorization context") from exc
    tenant_name = claims.get("tenant_name")
    return context, str(tenant_name).strip() if tenant_name else None
