from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.runtime.tenant_context import TenantContext, resolve_runtime_tenant_context
from deerflow.tools.builtins.tenant_datasource import TenantDataSource, TenantDataSourceError, build_database_code

TENANT_DB = "semantic_demo_public_demo"


def _runtime() -> SimpleNamespace:
    authorization = AuthorizationContext.from_mapping(
        {
            "principal_id": "public-user-001",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "system_code": "demo",
            "role_codes": ["site_admin"],
            "scope_mode": "resource_set",
            "allowed_site_ids": ["site-demo-001"],
            "allowed_project_ids": ["project-demo-001"],
            "permission_version": "1",
        }
    )
    return SimpleNamespace(
        context={
            "saas_user_id": "public-user-001",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "tenant_name": "Public Demo Tenant",
            "system_code": "demo",
            "authorization_context": authorization.to_runtime_dict(),
        }
    )


def _datasource() -> TenantDataSource:
    return TenantDataSource(
        code=TENANT_DB,
        host="127.0.0.1",
        port=3306,
        database=TENANT_DB,
        username="readonly",
        password="secret",
        driver_class="com.mysql.cj.jdbc.Driver",
        allowed_databases=(TENANT_DB,),
    )


def test_runtime_tenant_context_resolves_from_tool_context():
    ctx = resolve_runtime_tenant_context(_runtime())

    assert ctx.user_id == "public-user-001"
    assert ctx.tenant_id == "public-tenant-001"
    assert ctx.tenant_code == "public_demo"
    assert ctx.tenant_name == "Public Demo Tenant"
    assert ctx.system_code == "demo"


def test_build_database_code_uses_system_and_tenant_code():
    ctx = TenantContext(
        user_id="public-user-001",
        tenant_id="public-tenant-001",
        tenant_code="public_demo",
        tenant_name="Public Demo Tenant",
        system_code="demo",
    )

    assert build_database_code(ctx) == TENANT_DB


def test_build_database_code_rejects_unsafe_identifiers():
    ctx = TenantContext(
        user_id="public-user-001",
        tenant_id="public-tenant-001",
        tenant_code="public_demo;DROP",
        tenant_name=None,
        system_code="demo",
    )

    with pytest.raises(TenantDataSourceError):
        build_database_code(ctx)


def test_resolve_tenant_datasource_queries_configurable_public_registry_columns(monkeypatch):
    from deerflow.tools.builtins import tenant_datasource

    captured = {}
    db = MagicMock()
    db.run.return_value = "[('semantic_demo_public_demo', 'jdbc:mysql://127.0.0.1:3306/semantic_demo_public_demo', 'readonly', 'secret', 'com.mysql.cj.jdbc.Driver')]"

    def fake_from_uri(uri: str, sample_rows_in_table_info: int = 3):
        captured["uri"] = uri
        captured["sample_rows_in_table_info"] = sample_rows_in_table_info
        return db

    monkeypatch.setenv("SAAS_CONFIG_DB_HOST", "config-db")
    monkeypatch.setenv("SAAS_CONFIG_DB_PORT", "3306")
    monkeypatch.setenv("SAAS_CONFIG_DB_USER", "config_user")
    monkeypatch.setenv("SAAS_CONFIG_DB_PASSWORD", "config_password")
    monkeypatch.setenv("SAAS_CONFIG_DB_DATABASE", "config")
    monkeypatch.setattr(tenant_datasource.SQLDatabase, "from_uri", fake_from_uri)
    tenant_datasource._resolve_by_code.cache_clear()

    ds = tenant_datasource._resolve_by_code(TENANT_DB)

    assert "username" in db.run.call_args.args[0]
    assert "driver_class" in db.run.call_args.args[0]
    assert "user_name" not in db.run.call_args.args[0]
    assert "driverClass" not in db.run.call_args.args[0]
    assert db.run.call_args.kwargs["parameters"] == {"database_code": TENANT_DB}
    assert db.run.call_args.kwargs["execution_options"] == {"timeout": 10}
    assert captured["sample_rows_in_table_info"] == 0
    assert ds.username == "readonly"
    assert ds.driver_class == "com.mysql.cj.jdbc.Driver"


def test_get_db_without_tenant_context_uses_local_mysql(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    captured = {}

    def fake_from_uri(uri: str, sample_rows_in_table_info: int = 3):
        captured["uri"] = uri
        captured["sample_rows_in_table_info"] = sample_rows_in_table_info
        return MagicMock()

    monkeypatch.setenv("MYSQL_HOST", "localhost")
    monkeypatch.setenv("MYSQL_PORT", "3306")
    monkeypatch.setenv("MYSQL_USER", "local_user")
    monkeypatch.setenv("MYSQL_PASSWORD", "local_password")
    monkeypatch.setenv("MYSQL_DATABASE", "local_db")
    monkeypatch.setattr(sql_tools.SQLDatabase, "from_uri", fake_from_uri)

    sql_tools._get_db(runtime=SimpleNamespace(context={}))

    assert captured["uri"] == "mysql+mysqlconnector://local_user:local_password@localhost:3306/local_db"


def test_sql_show_databases_saas_mode_only_returns_allowed_database(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    monkeypatch.setattr(sql_tools, "resolve_tenant_datasource", lambda ctx: _datasource())

    result = sql_tools.sql_show_databases.func(runtime=_runtime())

    assert TENANT_DB in result
    assert "semantic_demo_other" not in result


def test_saas_discovery_tools_emit_scope_audit(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    records = []
    runtime = _runtime()
    runtime.context.update(
        {
            "run_id": "run-discovery",
            "thread_id": "thread-discovery",
            "__run_journal": SimpleNamespace(record_middleware=lambda **kwargs: records.append(kwargs)),
        }
    )
    runtime.tool_call_id = "tool-discovery"
    db = MagicMock()
    db.get_usable_table_names.return_value = ["demo_sites", "demo_private_credentials"]
    monkeypatch.setattr(sql_tools, "resolve_tenant_datasource", lambda ctx: _datasource())
    monkeypatch.setattr(sql_tools, "_get_db", lambda database=None, runtime=None: db)
    monkeypatch.setattr(
        sql_tools,
        "_safe_table_info",
        lambda _db, table, _policy: f"CREATE TABLE `{table}` (`id` varchar(64))",
    )

    sql_tools.sql_show_databases.func(runtime=runtime)
    sql_tools.sql_list_tables.func(runtime=runtime)
    sql_tools.sql_schema.func("demo_sites", runtime=runtime)

    assert [record["changes"]["operation"] for record in records] == [
        "sql_show_databases",
        "sql_list_tables",
        "sql_schema",
    ]
    assert all(record["changes"]["decision"] == "allow" for record in records)
    assert all(record["changes"]["scope_hash"] for record in records)
    assert records[1]["changes"]["referenced_tables"] == ["demo_sites"]
    assert records[2]["changes"]["referenced_tables"] == ["demo_sites"]


def test_saas_query_and_checker_emit_distinct_scope_audit_operations(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    records = []
    runtime = _runtime()
    runtime.context["__run_journal"] = SimpleNamespace(record_middleware=lambda **kwargs: records.append(kwargs))
    db = MagicMock()
    db.run.return_value = "[(1,)]"
    monkeypatch.setattr(sql_tools, "resolve_tenant_datasource", lambda _ctx: _datasource())
    monkeypatch.setattr(sql_tools.SQLDatabase, "from_uri", lambda *_args, **_kwargs: db)

    sql_tools.sql_query.func("SELECT id FROM demo_sites", runtime=runtime)
    sql_tools.sql_query_checker.func("SELECT id FROM demo_sites", runtime=runtime)

    assert [record["changes"]["operation"] for record in records] == [
        "sql_query",
        "sql_query_checker",
    ]
    assert all(record["changes"]["decision"] == "allow" for record in records)


def test_saas_list_tables_audits_disallowed_database_as_deny(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    records = []
    runtime = _runtime()
    runtime.context["__run_journal"] = SimpleNamespace(record_middleware=lambda **kwargs: records.append(kwargs))
    monkeypatch.setattr(sql_tools, "resolve_tenant_datasource", lambda _ctx: _datasource())

    result = sql_tools.sql_list_tables.func(
        database_name="semantic_demo_other",
        runtime=runtime,
    )

    assert result == "Error listing tables: authorization denied."
    assert len(records) == 1
    audit = records[0]["changes"]
    assert audit["operation"] == "sql_list_tables"
    assert audit["decision"] == "deny"
    assert audit["error_category"] == "AUTHORIZATION_DENIED"


def test_validate_query_databases_allows_current_tenant_database():
    from deerflow.tools.builtins.sql_tools import _validate_query_databases

    ok, error = _validate_query_databases(f"SELECT * FROM {TENANT_DB}.demo_projects", _datasource())

    assert ok is True
    assert error == ""


def test_validate_query_databases_blocks_other_tenant_database():
    from deerflow.tools.builtins.sql_tools import _validate_query_databases

    ok, error = _validate_query_databases("SELECT * FROM semantic_demo_other.demo_projects", _datasource())

    assert ok is False
    assert "not allowed" in error


def test_validate_table_databases_blocks_other_tenant_schema():
    from deerflow.tools.builtins.sql_tools import _validate_table_databases

    ok, error = _validate_table_databases(["semantic_demo_other.demo_projects"], _datasource())

    assert ok is False
    assert "not allowed" in error


def test_saas_schema_blocks_other_tenant_database(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    monkeypatch.setattr(sql_tools, "resolve_tenant_datasource", lambda ctx: _datasource())

    result = sql_tools.sql_schema.func("semantic_demo_other.demo_projects", runtime=_runtime())

    assert result.startswith("Error getting schema:")
    assert "not allowed" in result


def test_saas_query_blocks_write_operations():
    from deerflow.tools.builtins import sql_tools

    result = sql_tools.sql_query.func("DELETE FROM demo_projects", runtime=_runtime())

    assert result.startswith("Query blocked:")
    assert "DELETE" in result


def test_saas_query_ignores_mysql_allow_write(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    monkeypatch.setenv("MYSQL_ALLOW_WRITE", "true")

    result = sql_tools.sql_query.func("UPDATE demo_projects SET name = 'x'", runtime=_runtime())

    assert result.startswith("Query blocked:")


def test_saas_query_blocks_outfile():
    from deerflow.tools.builtins import sql_tools

    result = sql_tools.sql_query.func("SELECT * FROM demo_projects INTO OUTFILE '/tmp/x.csv'", runtime=_runtime())

    assert result.startswith("Query blocked:")
    assert "OUTFILE" in result


def test_saas_query_adds_default_limit_and_uses_tenant_database(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    db = MagicMock()
    db.run.return_value = "[(1,)]"
    captured = {}

    def fake_from_uri(uri: str, sample_rows_in_table_info: int = 3):
        captured["uri"] = uri
        return db

    monkeypatch.setattr(sql_tools, "resolve_tenant_datasource", lambda ctx: _datasource())
    monkeypatch.setattr(sql_tools.SQLDatabase, "from_uri", fake_from_uri)

    result = sql_tools.sql_query.func("SELECT id FROM demo_projects", runtime=_runtime())

    assert result == "[(1,)]"
    assert captured["uri"] == f"mysql+mysqlconnector://readonly:secret@127.0.0.1:3306/{TENANT_DB}"
    executed = db.run.call_args
    assert "scope_0_0" in executed.args[0]
    assert executed.kwargs["parameters"] == {"scope_0_0": "site-demo-001"}


def test_saas_query_fails_closed_without_authorization(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    runtime = _runtime()
    runtime.context.pop("authorization_context")
    monkeypatch.setattr(sql_tools, "resolve_tenant_datasource", lambda ctx: _datasource())

    result = sql_tools.sql_query.func("SELECT id FROM demo_projects", runtime=runtime)

    assert result.startswith("Query blocked:")
    assert "authorization context" in result


def test_saas_authorization_without_tenant_context_never_falls_back_to_local_mysql(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    runtime = _runtime()
    for key in ("tenant_id", "tenant_code", "system_code"):
        runtime.context.pop(key)
    from_uri = MagicMock(side_effect=AssertionError("local MySQL must not be opened"))
    monkeypatch.setattr(sql_tools.SQLDatabase, "from_uri", from_uri)

    result = sql_tools.sql_query.func("SELECT id FROM demo_projects", runtime=runtime)

    assert result.startswith("Query blocked:")
    assert "local MySQL fallback is disabled" in result
    from_uri.assert_not_called()


def test_saas_sql_audit_contains_scope_lineage_and_result_counts(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    records = []
    journal = SimpleNamespace(record_middleware=lambda **kwargs: records.append(kwargs))
    runtime = _runtime()
    runtime.tool_call_id = "tool-1"
    runtime.context.update(
        {
            "run_id": "run-1",
            "thread_id": "thread-1",
            "__run_journal": journal,
        }
    )
    db = MagicMock()
    db.run.return_value = "[{'id': 'project-demo-001'}]"
    monkeypatch.setattr(sql_tools, "resolve_tenant_datasource", lambda _ctx: _datasource())
    monkeypatch.setattr(sql_tools.SQLDatabase, "from_uri", lambda *_args, **_kwargs: db)

    result = sql_tools.sql_query.func("SELECT id FROM demo_projects", runtime=runtime)

    assert result == "[{'id': 'project-demo-001'}]"
    audit = records[-1]["changes"]
    assert audit["run_id"] == "run-1"
    assert audit["thread_id"] == "thread-1"
    assert audit["tool_call_id"] == "tool-1"
    assert audit["principal_id"] == "public-user-001"
    assert audit["referenced_tables"] == ["demo_projects"]
    assert "id" in audit["referenced_fields"]
    assert audit["scope_predicates_applied"] == 1
    assert audit["returned_rows"] == 1
    assert audit["truncated"] is False
    assert audit["error_category"] is None


def test_saas_sql_precheck_denial_is_audited_without_opening_database(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    records = []
    runtime = _runtime()
    runtime.context["__run_journal"] = SimpleNamespace(record_middleware=lambda **kwargs: records.append(kwargs))
    from_uri = MagicMock(side_effect=AssertionError("blocked SQL must not open a database"))
    monkeypatch.setattr(sql_tools.SQLDatabase, "from_uri", from_uri)

    result = sql_tools.sql_query.func("DELETE FROM demo_projects", runtime=runtime)

    assert result.startswith("Query blocked:")
    from_uri.assert_not_called()
    assert len(records) == 1
    audit = records[0]["changes"]
    assert audit["operation"] == "sql_query"
    assert audit["decision"] == "deny"
    assert audit["deny_reason"] == audit["reason"]
    assert audit["error_category"] == "QUERY_REJECTED"


def test_saas_schema_disables_sample_rows(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    captured = {}
    db = MagicMock()
    db.get_usable_table_names.return_value = ["demo_projects"]
    db.get_table_info.return_value = "CREATE TABLE demo_projects (...)"

    def fake_from_uri(uri: str, sample_rows_in_table_info: int = 3):
        captured["sample_rows"] = sample_rows_in_table_info
        return db

    monkeypatch.setattr(sql_tools, "resolve_tenant_datasource", lambda ctx: _datasource())
    monkeypatch.setattr(sql_tools.SQLDatabase, "from_uri", fake_from_uri)
    monkeypatch.setattr(sql_tools, "_safe_table_info", lambda _db, table, _policy: f"CREATE TABLE {table} (...)")

    result = sql_tools.sql_schema.func("demo_projects", runtime=_runtime())

    assert "CREATE TABLE" in result
    assert captured["sample_rows"] == 0


def test_saas_schema_missing_table_does_not_reveal_forbidden_physical_tables(monkeypatch):
    from deerflow.tools.builtins import sql_tools

    db = MagicMock()
    db.get_usable_table_names.return_value = ["demo_private_credentials"]
    monkeypatch.setattr(sql_tools, "resolve_tenant_datasource", lambda _ctx: _datasource())
    monkeypatch.setattr(sql_tools.SQLDatabase, "from_uri", lambda *_args, **_kwargs: db)

    result = sql_tools.sql_schema.func("demo_sites", runtime=_runtime())

    assert result == "Error: Tables not found: ['demo_sites']."
    assert "demo_private_credentials" not in result


def test_sql_tools_hide_runtime_from_model_schema():
    from deerflow.tools.builtins.sql_tools import sql_query, sql_show_databases

    assert "runtime" not in sql_query.args
    assert "runtime" not in sql_show_databases.args
