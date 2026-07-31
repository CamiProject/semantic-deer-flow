"""Trusted SaaS tenant context helpers."""

from __future__ import annotations

from dataclasses import dataclass

from deerflow.runtime.user_context import resolve_runtime_user_id


class MissingTenantContextError(RuntimeError):
    """Raised when a run has no trusted SaaS tenant context."""


@dataclass(frozen=True)
class TenantContext:
    user_id: str
    tenant_id: str
    tenant_code: str
    tenant_name: str | None
    system_code: str


def _string_value(context: dict, key: str) -> str | None:
    value = context.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def resolve_runtime_tenant_context(runtime: object | None) -> TenantContext:
    """Resolve trusted SaaS tenant context from ``ToolRuntime.context``.

    Missing context is intentionally distinct from a permission failure so SQL
    tools can preserve local ``MYSQL_*`` fallback behaviour for development.
    """
    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        raise MissingTenantContextError("No SaaS tenant context is available.")

    tenant_id = _string_value(context, "tenant_id")
    tenant_code = _string_value(context, "tenant_code")
    system_code = _string_value(context, "system_code")
    if not tenant_id or not tenant_code or not system_code:
        raise MissingTenantContextError("No SaaS tenant context is available.")

    return TenantContext(
        user_id=_string_value(context, "saas_user_id") or resolve_runtime_user_id(runtime),
        tenant_id=tenant_id,
        tenant_code=tenant_code,
        tenant_name=_string_value(context, "tenant_name"),
        system_code=system_code,
    )
