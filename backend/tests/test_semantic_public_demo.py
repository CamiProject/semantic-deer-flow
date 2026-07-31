from __future__ import annotations

import pytest

from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.runtime.tenant_context import TenantContext
from deerflow.semantic.demo import (
    create_public_demo_runtime,
    resolve_public_demo_datasource,
)
from deerflow.semantic.ontology import get_ontology_registry
from deerflow.semantic.sql_scope import get_sql_scope_policy_registry
from deerflow.tools.builtins.tenant_datasource import TenantDataSourceError


def _authorization() -> AuthorizationContext:
    return AuthorizationContext.from_mapping(
        {
            "principal_id": "public-user-001",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "system_code": "demo",
            "role_codes": ["viewer"],
            "scope_mode": "resource_set",
            "allowed_site_ids": ["site-demo-001"],
            "allowed_project_ids": ["project-demo-001"],
            "permission_version": "1",
        }
    )


def test_public_demo_runtime_reads_only_scoped_rows() -> None:
    runtime = create_public_demo_runtime(
        ontology=get_ontology_registry(),
        sql_policy=get_sql_scope_policy_registry(),
    )

    visible = runtime.get_object(
        authorization=_authorization(),
        object_type="Site",
        object_id="site-demo-001",
    )
    hidden = runtime.get_object(
        authorization=_authorization(),
        object_type="Site",
        object_id="site-demo-002",
    )

    assert visible.rows == [{"id": "site-demo-001", "name": "Public Demo Site A"}]
    assert hidden.rows == []
    assert visible.source_refs == ("demo_sites",)


def test_public_demo_datasource_rejects_non_public_tenant() -> None:
    context = TenantContext(
        user_id="private-user",
        tenant_id="private-tenant",
        tenant_code="private",
        tenant_name=None,
        system_code="demo",
    )

    with pytest.raises(TenantDataSourceError, match="synthetic tenant"):
        resolve_public_demo_datasource(context)
