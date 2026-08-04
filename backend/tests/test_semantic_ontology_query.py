from __future__ import annotations

import pytest

from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.semantic.ontology import OntologyError, OntologyRegistry
from deerflow.semantic.query import (
    SemanticFilter,
    SemanticOrder,
    compile_metric_query,
    compile_metrics_query,
    compile_object_query,
)
from deerflow.semantic.sql_scope import SqlScopePolicyRegistry


@pytest.fixture()
def registry():
    return OntologyRegistry.from_mapping(
        {
            "version": "7",
            "objects": {
                "Site": {
                    "table": "demo_sites",
                    "id_field": "id",
                    "label": "场地",
                    "keywords": ["site"],
                    "properties": {
                        "id": {"column": "id", "type": "string"},
                        "name": {
                            "column": "name",
                            "type": "string",
                            "label": "场地名称",
                            "sensitivity": "internal",
                            "filterable": True,
                        },
                    },
                },
                "Project": {
                    "table": "demo_projects",
                    "id_field": "id",
                    "label": "项目",
                    "properties": {
                        "id": {"column": "id", "type": "string"},
                        "site_id": {"column": "site_id", "type": "string", "filterable": True},
                        "private_code": {
                            "column": "private_code",
                            "type": "string",
                            "allowed_roles": ["tenant_admin"],
                        },
                    },
                },
            },
            "links": {},
            "metrics": {
                "project.count": {
                    "object_type": "Project",
                    "aggregation": "count",
                    "dimensions": ["site_id"],
                    "filters": ["site_id"],
                    "keywords": ["项目数"],
                    "grain": "project",
                    "time_semantics": "snapshot",
                    "allowed_roles": ["site_admin"],
                }
            },
            "actions": {
                "site.rename": {
                    "version": "2",
                    "keywords": ["rename site", "change site"],
                    "target_type": "Site",
                    "scope_dimension": "site",
                    "parameters": {"name": {"type": "string", "required": True, "min_length": 1}},
                    "approval": {"required": True},
                    "authorization": {"allowed_roles": ["site_admin"]},
                    "executor": {"type": "domain_api", "method": "PATCH", "path": "/sites/{target_id}"},
                }
            },
        }
    )


@pytest.fixture()
def policy():
    return SqlScopePolicyRegistry.from_mapping(
        {
            "version": "3",
            "tables": {
                "demo_sites": {"access": "scoped", "scope_dimension": "site", "scope_column": "id"},
                "demo_projects": {"access": "scoped", "scope_dimension": "site", "scope_column": "site_id"},
            },
        }
    )


@pytest.fixture()
def authorization():
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


def test_registry_resolves_business_context_and_validates_actions(registry):
    context = registry.resolve("查询场地和项目数")

    assert context["ontology_version"] == "7"
    assert [item["id"] for item in context["objects"]] == ["Site", "Project"]
    assert [item["id"] for item in context["metrics"]] == ["project.count"]
    assert registry.action("site.rename").validate_parameters({"name": "New"}) == {"name": "New"}
    assert registry.object("Site").property("name").sensitivity == "internal"
    assert registry.metric("project.count").grain == "project"
    with pytest.raises(OntologyError):
        registry.action("site.rename").validate_parameters({"name": ""})


@pytest.mark.parametrize(
    "question",
    [
        "Change site-demo-001 display name to New Display Name.",
        "Rename site-demo-001 to New Display Name.",
    ],
)
def test_registry_resolves_action_keywords(registry, authorization, question):
    context = registry.resolve(question, authorization=authorization)

    assert [item["id"] for item in context["actions"]] == ["site.rename"]
    assert registry.action("site.rename").keywords == ("rename site", "change site")


def test_registry_merges_structured_semantic_candidates_before_authorization(registry, authorization):
    question = "update the facility title"

    exact = registry.resolve(question, authorization=authorization)
    recalled = registry.resolve(
        question,
        authorization=authorization,
        candidate_ids={"objects": ["Site"], "actions": ["site.rename"]},
    )

    assert exact["actions"] == []
    assert [item["id"] for item in recalled["objects"]] == ["Site"]
    assert [item["id"] for item in recalled["actions"]] == ["site.rename"]


def test_registry_filters_role_restricted_metrics_and_actions(registry):
    viewer = AuthorizationContext.from_mapping(
        {
            "principal_id": "public-user-001",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "system_code": "demo",
            "role_codes": ["viewer"],
            "scope_mode": "resource_set",
            "allowed_site_ids": ["site-demo-001"],
            "allowed_project_ids": [],
            "permission_version": "1",
        }
    )

    context = registry.resolve("查询项目数并修改场地", authorization=viewer)

    assert context["metrics"] == []
    assert context["actions"] == []
    project = next(item for item in context["objects"] if item["id"] == "Project")
    assert "private_code" not in project["properties"]
    assert "private_code" not in {item["id"] for item in project["property_definitions"]}
    with pytest.raises(OntologyError, match="not authorized"):
        registry.authorize_metric("project.count", viewer)
    with pytest.raises(OntologyError, match="not authorized"):
        registry.authorize_action("site.rename", viewer)


def test_object_query_compiles_through_scope_guard(registry, policy, authorization):
    compiled = compile_object_query(
        registry=registry,
        object_type="Site",
        authorization=authorization,
        allowed_databases=("semantic_demo",),
        filters=[SemanticFilter(field="name", operator="eq", value="A")],
        policy_registry=policy,
    )

    assert compiled.semantic_kind == "object"
    assert compiled.columns == ("id", "name")
    assert compiled.scoped_query.parameters == {"filter_0": "A", "scope_0_0": "site-demo-001"}
    assert "scope_0_0" in compiled.scoped_query.sql


def test_object_query_caps_in_filter_cardinality(registry, policy, authorization):
    with pytest.raises(OntologyError, match="filter budget"):
        compile_object_query(
            registry=registry,
            object_type="Site",
            authorization=authorization,
            allowed_databases=("semantic_demo",),
            filters=[
                SemanticFilter(
                    field="id",
                    operator="in",
                    value=[f"site-{index}" for index in range(101)],
                )
            ],
            policy_registry=policy,
        )


def test_metric_query_allows_only_declared_dimensions_and_filters(registry, policy, authorization):
    compiled = compile_metric_query(
        registry=registry,
        metric_id="project.count",
        authorization=authorization,
        allowed_databases=("semantic_demo",),
        dimensions=["site_id"],
        filters=[SemanticFilter(field="site_id", operator="eq", value="site-demo-001")],
        policy_registry=policy,
    )

    assert compiled.columns == ("site_id", "project.count")
    assert "COUNT(*)" in compiled.scoped_query.sql
    assert "GROUP BY" in compiled.scoped_query.sql
    assert compiled.scoped_query.parameters["scope_0_0"] == "site-demo-001"

    with pytest.raises(OntologyError, match="dimensions"):
        compile_metric_query(
            registry=registry,
            metric_id="project.count",
            authorization=authorization,
            allowed_databases=("semantic_demo",),
            dimensions=["id"],
            policy_registry=policy,
        )


def test_multi_metric_query_compiles_ordering_without_accepting_sql(registry, policy, authorization):
    registry = OntologyRegistry.from_mapping(
        {
            "version": "8",
            "objects": {
                "Project": {
                    "table": "demo_projects",
                    "id_field": "id",
                    "properties": {
                        "id": {"column": "id", "type": "string"},
                        "site_id": {"column": "site_id", "type": "string", "filterable": True},
                        "budget": {"column": "budget", "type": "number", "aggregatable": True},
                    },
                }
            },
            "links": {},
            "metrics": {
                "project.count": {
                    "object_type": "Project",
                    "aggregation": "count",
                    "dimensions": ["site_id"],
                    "filters": ["site_id"],
                },
                "project.budget_total": {
                    "object_type": "Project",
                    "aggregation": "sum",
                    "field": "budget",
                    "dimensions": ["site_id"],
                    "filters": ["site_id"],
                    "unit": "CNY",
                },
            },
            "actions": {},
        }
    )

    compiled = compile_metrics_query(
        registry=registry,
        metric_ids=["project.count", "project.budget_total"],
        authorization=authorization,
        allowed_databases=("semantic_demo",),
        dimensions=["site_id"],
        order_by=[SemanticOrder(field="project.budget_total", direction="desc")],
        policy_registry=policy,
    )

    assert compiled.columns == ("site_id", "project.count", "project.budget_total")
    assert compiled.semantic_ids == ("project.count", "project.budget_total")
    assert "ORDER BY `project.budget_total` DESC" in compiled.scoped_query.sql

    with pytest.raises(OntologyError, match="order"):
        compile_metrics_query(
            registry=registry,
            metric_ids=["project.count"],
            authorization=authorization,
            allowed_databases=("semantic_demo",),
            order_by=[SemanticOrder(field="DROP_TABLE", direction="desc")],
            policy_registry=policy,
        )
