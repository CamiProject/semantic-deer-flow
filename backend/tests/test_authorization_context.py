from __future__ import annotations

from types import SimpleNamespace

import pytest

from deerflow.runtime.authorization_context import (
    AuthorizationContext,
    AuthorizationContextError,
    MissingAuthorizationContextError,
    resolve_runtime_authorization_context,
)


def _mapping(**overrides):
    value = {
        "principal_id": "public-user-001",
        "tenant_id": "public-tenant-001",
        "tenant_code": "public_demo",
        "system_code": "demo",
        "role_codes": ["site_admin", "viewer"],
        "scope_mode": "resource_set",
        "allowed_site_ids": ["site-demo-002", "site-demo-001"],
        "allowed_project_ids": ["project-demo-001"],
        "scope_ref": None,
        "permission_version": "42",
    }
    value.update(overrides)
    return value


def test_authorization_context_normalizes_resources_and_hashes_stably():
    first = AuthorizationContext.from_mapping(_mapping())
    second = AuthorizationContext.from_mapping(
        _mapping(
            role_codes=["viewer", "site_admin", "viewer"],
            allowed_site_ids=["site-demo-001", "site-demo-002", "site-demo-001"],
        )
    )

    assert first.role_codes == ("site_admin", "viewer")
    assert first.allowed_site_ids == ("site-demo-001", "site-demo-002")
    assert first.scope_hash == second.scope_hash


def test_authorization_context_rejects_empty_resource_set():
    with pytest.raises(AuthorizationContextError, match="at least one resource"):
        AuthorizationContext.from_mapping(_mapping(allowed_site_ids=[], allowed_project_ids=[]))


def test_authorization_context_rejects_caller_supplied_wrong_hash():
    with pytest.raises(AuthorizationContextError, match="scope_hash"):
        AuthorizationContext.from_mapping(_mapping(scope_hash="0" * 64))


def test_resolve_runtime_authorization_context_reads_nested_trusted_value():
    expected = AuthorizationContext.from_mapping(_mapping())
    runtime = SimpleNamespace(context={"authorization_context": expected.to_runtime_dict()})

    actual = resolve_runtime_authorization_context(runtime)

    assert actual == expected


def test_resolve_runtime_authorization_context_fails_closed_when_missing():
    with pytest.raises(MissingAuthorizationContextError):
        resolve_runtime_authorization_context(SimpleNamespace(context={}))
