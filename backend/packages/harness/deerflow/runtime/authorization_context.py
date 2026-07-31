"""Trusted SaaS authorization context used by data and action tools."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SCOPE_MODES = frozenset({"tenant_all", "resource_set", "scope_ref", "none"})
_MAX_RESOURCE_IDS = 256


class AuthorizationContextError(ValueError):
    """Raised when a trusted authorization context is malformed."""


class MissingAuthorizationContextError(RuntimeError):
    """Raised when a runtime has no trusted SaaS authorization context."""


def _required_id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(text):
        raise AuthorizationContextError(f"Invalid {field}")
    return text


def _optional_id(value: Any, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _required_id(text, field)


def _id_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise AuthorizationContextError(f"{field} must be a list")
    if len(value) > _MAX_RESOURCE_IDS:
        raise AuthorizationContextError(f"{field} exceeds {_MAX_RESOURCE_IDS} entries")
    return tuple(sorted({_required_id(item, field) for item in value}))


def compute_scope_hash(
    *,
    principal_id: str,
    tenant_id: str,
    tenant_code: str,
    system_code: str,
    role_codes: tuple[str, ...],
    scope_mode: str,
    allowed_site_ids: tuple[str, ...],
    allowed_project_ids: tuple[str, ...],
    scope_ref: str | None,
    permission_version: str,
) -> str:
    """Return a stable digest for the effective authorization boundary."""
    payload = {
        "allowed_project_ids": sorted(allowed_project_ids),
        "allowed_site_ids": sorted(allowed_site_ids),
        "permission_version": permission_version,
        "principal_id": principal_id,
        "role_codes": sorted(role_codes),
        "scope_mode": scope_mode,
        "scope_ref": scope_ref,
        "system_code": system_code,
        "tenant_code": tenant_code,
        "tenant_id": tenant_id,
    }
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthorizationContext:
    principal_id: str
    tenant_id: str
    tenant_code: str
    system_code: str
    role_codes: tuple[str, ...]
    scope_mode: str
    allowed_site_ids: tuple[str, ...]
    allowed_project_ids: tuple[str, ...]
    scope_ref: str | None
    permission_version: str
    scope_hash: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AuthorizationContext:
        principal_id = _required_id(value.get("principal_id"), "principal_id")
        tenant_id = _required_id(value.get("tenant_id"), "tenant_id")
        tenant_code = _required_id(value.get("tenant_code"), "tenant_code")
        system_code = _required_id(value.get("system_code"), "system_code")
        role_codes = _id_tuple(value.get("role_codes"), "role_codes")
        scope_mode = str(value.get("scope_mode") or "").strip()
        if scope_mode not in _SCOPE_MODES:
            raise AuthorizationContextError("Invalid scope_mode")
        allowed_site_ids = _id_tuple(value.get("allowed_site_ids"), "allowed_site_ids")
        allowed_project_ids = _id_tuple(value.get("allowed_project_ids"), "allowed_project_ids")
        scope_ref = _optional_id(value.get("scope_ref"), "scope_ref")
        permission_version = _required_id(value.get("permission_version"), "permission_version")

        if scope_mode == "resource_set" and not (allowed_site_ids or allowed_project_ids):
            raise AuthorizationContextError("resource_set scope must contain at least one resource")
        if scope_mode == "scope_ref" and scope_ref is None:
            raise AuthorizationContextError("scope_ref mode requires scope_ref")
        if scope_mode in {"tenant_all", "none"} and (allowed_site_ids or allowed_project_ids or scope_ref):
            raise AuthorizationContextError(f"{scope_mode} scope cannot carry resources")

        scope_hash = compute_scope_hash(
            principal_id=principal_id,
            tenant_id=tenant_id,
            tenant_code=tenant_code,
            system_code=system_code,
            role_codes=role_codes,
            scope_mode=scope_mode,
            allowed_site_ids=allowed_site_ids,
            allowed_project_ids=allowed_project_ids,
            scope_ref=scope_ref,
            permission_version=permission_version,
        )
        supplied_hash = str(value.get("scope_hash") or "").strip()
        if supplied_hash and supplied_hash != scope_hash:
            raise AuthorizationContextError("scope_hash does not match authorization context")

        return cls(
            principal_id=principal_id,
            tenant_id=tenant_id,
            tenant_code=tenant_code,
            system_code=system_code,
            role_codes=role_codes,
            scope_mode=scope_mode,
            allowed_site_ids=allowed_site_ids,
            allowed_project_ids=allowed_project_ids,
            scope_ref=scope_ref,
            permission_version=permission_version,
            scope_hash=scope_hash,
        )

    def to_runtime_dict(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "tenant_code": self.tenant_code,
            "system_code": self.system_code,
            "role_codes": list(self.role_codes),
            "scope_mode": self.scope_mode,
            "allowed_site_ids": list(self.allowed_site_ids),
            "allowed_project_ids": list(self.allowed_project_ids),
            "scope_ref": self.scope_ref,
            "permission_version": self.permission_version,
            "scope_hash": self.scope_hash,
        }


def resolve_runtime_authorization_context(runtime: object | None) -> AuthorizationContext:
    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        raise MissingAuthorizationContextError("No SaaS authorization context is available.")
    value = context.get("authorization_context")
    if not isinstance(value, Mapping):
        raise MissingAuthorizationContextError("No SaaS authorization context is available.")
    try:
        return AuthorizationContext.from_mapping(value)
    except AuthorizationContextError as exc:
        raise MissingAuthorizationContextError("The SaaS authorization context is invalid.") from exc
