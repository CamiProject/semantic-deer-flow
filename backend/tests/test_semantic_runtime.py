from __future__ import annotations

from unittest.mock import MagicMock

from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.semantic.ontology import OntologyRegistry
from deerflow.semantic.query import SemanticFilter
from deerflow.semantic.runtime import SemanticQueryRuntime
from deerflow.semantic.sql_scope import SqlScopePolicyRegistry
from deerflow.tools.builtins.tenant_datasource import TenantDataSource


def _authorization():
    return AuthorizationContext.from_mapping(
        {
            "principal_id": "public-user-001",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "system_code": "demo",
            "role_codes": ["site_admin"],
            "scope_mode": "resource_set",
            "allowed_site_ids": ["site-demo-001"],
            "allowed_project_ids": [],
            "permission_version": "1",
        }
    )


def _runtime():
    ontology = OntologyRegistry.from_mapping(
        {
            "version": "1",
            "objects": {
                "Site": {
                    "table": "demo_sites",
                    "id_field": "id",
                    "label": "场地",
                    "keywords": ["site"],
                    "properties": {
                        "id": {"column": "id", "type": "string"},
                        "name": {"column": "name", "type": "string"},
                    },
                }
            },
            "links": {},
            "metrics": {
                "site.count": {
                    "object_type": "Site",
                    "aggregation": "count",
                    "dimensions": [],
                    "filters": ["name"],
                    "keywords": ["场地数"],
                }
            },
            "actions": {
                "site.rename": {
                    "target_type": "Site",
                    "scope_dimension": "site",
                    "parameters": {"name": {"type": "string", "required": True}},
                    "approval": {"required": True},
                    "executor": {"type": "domain_api", "path": "/sites/{target_id}"},
                }
            },
        }
    )
    policy = SqlScopePolicyRegistry.from_mapping(
        {
            "version": "1",
            "tables": {"demo_sites": {"access": "scoped", "scope_dimension": "site", "scope_column": "id"}},
        }
    )
    datasource = TenantDataSource(
        code="semantic_demo",
        host="db",
        port=3306,
        database="semantic_demo",
        username="readonly",
        password="secret",
        driver_class="mysql",
        allowed_databases=("semantic_demo",),
    )
    database = MagicMock()
    database.run.return_value = [{"id": "site-demo-001", "name": "A"}]
    runtime = SemanticQueryRuntime(
        ontology=ontology,
        sql_policy=policy,
        datasource_resolver=lambda _ctx: datasource,
        database_factory=lambda _uri: database,
    )
    return runtime, database


def test_runtime_executes_compiled_scoped_query_and_returns_lineage():
    runtime, database = _runtime()

    result = runtime.search_objects(
        authorization=_authorization(),
        object_type="Site",
        filters=[SemanticFilter(field="name", operator="eq", value="A")],
    )

    assert result.rows == [{"id": "site-demo-001", "name": "A"}]
    assert result.source_refs == ("demo_sites",)
    assert result.scope_hash == _authorization().scope_hash
    assert result.authorization_scope_hash == _authorization().scope_hash
    assert result.as_of
    assert len(result.normalized_query_hash) == 64
    assert result.scope_predicates_applied == 1
    call = database.run.call_args
    assert call.kwargs["parameters"] == {"filter_0": "A", "scope_0_0": "site-demo-001"}


def test_runtime_oag_facts_use_scoped_object_query():
    runtime, database = _runtime()

    context = runtime.resolve_business_context(
        authorization=_authorization(),
        question="查看场地",
    )

    assert context["objects"][0]["id"] == "Site"
    assert context["facts"][0]["rows"] == [{"id": "site-demo-001", "name": "A"}]
    assert context["authorization_scope_hash"] == _authorization().scope_hash
    assert context["as_of"]
    assert context["source_refs"] == ["demo_sites"]
    assert "scope_0_0" in database.run.call_args.args[0]
