from __future__ import annotations

import pytest

from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.semantic.sql_scope import SqlScopeError, SqlScopePolicyRegistry, guard_sql_query


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
            "allowed_site_ids": ["site-demo-001", "site-demo-002"],
            "allowed_project_ids": ["project-demo-001"],
            "permission_version": "1",
        }
    )


@pytest.fixture()
def registry():
    return SqlScopePolicyRegistry.from_mapping(
        {
            "version": "test-1",
            "tables": {
                "demo_sites": {
                    "access": "scoped",
                    "scope_dimension": "site",
                    "scope_column": "id",
                    "allowed_fields": ["id", "name"],
                },
                "demo_projects": {
                    "access": "scoped",
                    "scope_dimension": "site",
                    "scope_column": "site_id",
                    "allowed_fields": ["id", "name", "site_id"],
                },
                "demo_devices": {
                    "access": "scoped",
                    "scope_dimension": "project",
                    "scope_column": "project_id",
                    "hidden_fields": ["secret"],
                    "masked_fields": ["serial_no"],
                    "aggregate_only_fields": ["reading"],
                },
                "demo_reference_values": {"access": "reference"},
                "demo_private_credentials": {"access": "forbidden"},
            },
        }
    )


def test_guard_scopes_joined_tables_and_binds_resources(authorization, registry):
    guarded = guard_sql_query(
        "SELECT p.id, s.name FROM demo_projects p JOIN demo_sites s ON s.id = p.site_id",
        authorization=authorization,
        allowed_databases=("semantic_demo",),
        registry=registry,
    )

    assert "scope_0_0" in guarded.sql
    assert "scope_1_0" in guarded.sql
    assert set(guarded.parameters.values()) == {"site-demo-001", "site-demo-002"}
    assert guarded.scope_predicates_applied == 2
    assert {"id", "name", "site_id"}.issubset(set(guarded.referenced_fields))
    assert guarded.referenced_tables == ("demo_projects", "demo_sites")
    assert guarded.sql.endswith("LIMIT 100")


def test_guard_scopes_cte_and_union_branches(authorization, registry):
    guarded = guard_sql_query(
        "WITH p AS (SELECT id FROM demo_projects) SELECT id FROM p UNION ALL SELECT id FROM demo_sites",
        authorization=authorization,
        allowed_databases=("semantic_demo",),
        registry=registry,
    )

    assert guarded.referenced_tables == ("demo_projects", "demo_sites")
    assert len(guarded.parameters) == 4


def test_guard_rejects_cte_names_that_shadow_policy_tables(authorization, registry):
    with pytest.raises(SqlScopeError, match="shadows"):
        guard_sql_query(
            "WITH demo_sites AS (SELECT id FROM semantic_demo.demo_sites) SELECT p.id FROM demo_projects p JOIN demo_sites s ON s.id = p.site_id",
            authorization=authorization,
            allowed_databases=("semantic_demo",),
            registry=registry,
        )


def test_guard_allows_count_star_for_field_restricted_table(authorization, registry):
    guarded = guard_sql_query(
        "SELECT COUNT(*) AS total FROM demo_sites",
        authorization=authorization,
        allowed_databases=("semantic_demo",),
        registry=registry,
    )

    assert "COUNT(*)" in guarded.sql
    assert guarded.scope_predicates_applied == 1


def test_guard_rejects_unqualified_field_not_allowed_by_every_candidate(authorization):
    registry = SqlScopePolicyRegistry.from_mapping(
        {
            "version": "ambiguous-1",
            "tables": {
                "table_a": {"access": "reference", "allowed_fields": ["id"]},
                "table_b": {"access": "reference", "allowed_fields": ["id", "secret"]},
            },
        }
    )

    with pytest.raises(SqlScopeError, match="not allowed"):
        guard_sql_query(
            "SELECT secret FROM table_a a JOIN table_b b ON a.id = b.id",
            authorization=authorization,
            allowed_databases=("semantic_demo",),
            registry=registry,
        )


def test_guard_rejects_unknown_sensitive_and_cross_database_tables(authorization, registry):
    with pytest.raises(SqlScopeError, match="not allowed"):
        guard_sql_query("SELECT * FROM unknown", authorization=authorization, allowed_databases=("semantic_demo",), registry=registry)
    with pytest.raises(SqlScopeError, match="not allowed"):
        guard_sql_query("SELECT id FROM demo_private_credentials", authorization=authorization, allowed_databases=("semantic_demo",), registry=registry)
    with pytest.raises(SqlScopeError, match="Database"):
        guard_sql_query("SELECT id FROM other.demo_sites", authorization=authorization, allowed_databases=("semantic_demo",), registry=registry)


def test_guard_rejects_multiple_statements_and_writes(authorization, registry):
    with pytest.raises(SqlScopeError, match="Exactly one"):
        guard_sql_query("SELECT id FROM demo_sites; SELECT id FROM demo_sites", authorization=authorization, allowed_databases=("semantic_demo",), registry=registry)
    with pytest.raises(SqlScopeError, match="SELECT"):
        guard_sql_query("DELETE FROM demo_sites", authorization=authorization, allowed_databases=("semantic_demo",), registry=registry)


def test_guard_rejects_hidden_field_and_star(authorization, registry):
    with pytest.raises(SqlScopeError, match="hidden"):
        guard_sql_query("SELECT secret FROM demo_devices", authorization=authorization, allowed_databases=("semantic_demo",), registry=registry)
    with pytest.raises(SqlScopeError, match=r"SELECT \*"):
        guard_sql_query("SELECT * FROM demo_devices", authorization=authorization, allowed_databases=("semantic_demo",), registry=registry)
    with pytest.raises(SqlScopeError, match="masked"):
        guard_sql_query("SELECT serial_no FROM demo_devices", authorization=authorization, allowed_databases=("semantic_demo",), registry=registry)
    with pytest.raises(SqlScopeError, match="aggregate-only"):
        guard_sql_query("SELECT reading FROM demo_devices", authorization=authorization, allowed_databases=("semantic_demo",), registry=registry)

    guarded = guard_sql_query(
        "SELECT SUM(reading) AS total FROM demo_devices",
        authorization=authorization,
        allowed_databases=("semantic_demo",),
        registry=registry,
    )
    assert "SUM(reading)" in guarded.sql


def test_guard_clamps_limits_and_rejects_unbounded_offsets(authorization, registry):
    guarded = guard_sql_query(
        "SELECT id FROM demo_sites LIMIT 999999",
        authorization=authorization,
        allowed_databases=("semantic_demo",),
        registry=registry,
        limit=100,
    )

    assert guarded.sql.endswith("LIMIT 100")

    with pytest.raises(SqlScopeError, match="OFFSET"):
        guard_sql_query(
            "SELECT id FROM demo_sites LIMIT 100 OFFSET 999999",
            authorization=authorization,
            allowed_databases=("semantic_demo",),
            registry=registry,
        )


@pytest.mark.parametrize(
    "query",
    [
        "SELECT SLEEP(10) FROM demo_sites",
        "SELECT LOAD_FILE('/etc/passwd') FROM demo_sites",
        "SELECT id FROM demo_sites FOR UPDATE",
        "SELECT id FROM demo_sites INTO OUTFILE '/tmp/export.csv'",
        "SELECT @@version FROM demo_sites",
        "SELECT CURRENT_USER() FROM demo_sites",
        "SELECT DATABASE() FROM demo_sites",
        "SELECT VERSION() FROM demo_sites",
        "SELECT CONNECTION_ID() FROM demo_sites",
        "SELECT @previous_value FROM demo_sites",
        "SELECT @captured := name FROM demo_sites",
        "SELECT LAST_INSERT_ID() FROM demo_sites",
        "SELECT FOUND_ROWS() FROM demo_sites",
        "SELECT ROW_COUNT() FROM demo_sites",
        "SELECT /*+ SET_VAR(max_execution_time=0) */ id FROM demo_sites",
    ],
)
def test_guard_rejects_dangerous_select_constructs(authorization, registry, query):
    with pytest.raises(SqlScopeError):
        guard_sql_query(
            query,
            authorization=authorization,
            allowed_databases=("semantic_demo",),
            registry=registry,
        )


def test_tenant_all_keeps_policy_but_does_not_add_resource_predicates(registry):
    authorization = AuthorizationContext.from_mapping(
        {
            "principal_id": "admin",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "system_code": "demo",
            "role_codes": ["tenant_admin"],
            "scope_mode": "tenant_all",
            "allowed_site_ids": [],
            "allowed_project_ids": [],
            "permission_version": "1",
        }
    )

    guarded = guard_sql_query("SELECT id FROM demo_sites", authorization=authorization, allowed_databases=("semantic_demo",), registry=registry)

    assert guarded.parameters == {}
    assert "scope_" not in guarded.sql
