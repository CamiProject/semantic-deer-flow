"""SQL tools for MySQL database querying."""

import ast
import hashlib
import os
import re
import time
from urllib.parse import quote_plus

from langchain.tools import tool
from langchain_community.utilities import SQLDatabase

from deerflow.runtime.authorization_context import (
    AuthorizationContext,
    MissingAuthorizationContextError,
    resolve_runtime_authorization_context,
)
from deerflow.runtime.tenant_context import MissingTenantContextError, TenantContext, resolve_runtime_tenant_context
from deerflow.semantic.sql_scope import (
    ScopedQuery,
    SqlScopeError,
    TableScopePolicy,
    get_sql_scope_policy_registry,
    guard_sql_query,
)
from deerflow.tools.builtins.tenant_datasource import TenantDataSource, TenantDataSourceError, build_tenant_mysql_uri, resolve_tenant_datasource
from deerflow.tools.types import Runtime

FORBIDDEN_KEYWORDS = [
    "DROP",
    "DELETE",
    "TRUNCATE",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "EXECUTE",
    "CALL",
    "USE",
    "OUTFILE",
    "DUMPFILE",
    "LOAD",
    "SET",
    "LOCK",
    "UNLOCK",
]

UPDATE_INSERT_KEYWORDS = ["UPDATE", "INSERT", "REPLACE"]

DEFAULT_LIMIT = 100
SYSTEM_DATABASES = {"mysql", "information_schema", "performance_schema", "sys"}


class _LocalSQLMode:
    pass


LOCAL_SQL_MODE = _LocalSQLMode()


def _get_mysql_connection_string(database: str | None = None) -> str:
    host = os.getenv("MYSQL_HOST", "localhost")
    port = os.getenv("MYSQL_PORT", "3306")
    user = os.getenv("MYSQL_USER", "root")
    password = os.getenv("MYSQL_PASSWORD", "")
    db = database or os.getenv("MYSQL_DATABASE", "")

    if db:
        return f"mysql+mysqlconnector://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    else:
        return f"mysql+mysqlconnector://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/"


def _get_default_database() -> str:
    return os.getenv("MYSQL_DATABASE", "")


def _get_db(database: str | None = None, runtime: Runtime | None = None) -> SQLDatabase:
    source = _resolve_sql_source(runtime)
    if source is LOCAL_SQL_MODE:
        conn_str = _get_mysql_connection_string(database)
    else:
        assert isinstance(source, TenantDataSource)
        target_db = database or source.database
        _ensure_database_allowed(source, target_db)
        conn_str = build_tenant_mysql_uri(source, target_db)
    return SQLDatabase.from_uri(
        conn_str,
        sample_rows_in_table_info=0 if isinstance(source, TenantDataSource) else 3,
    )


def _resolve_sql_source(runtime: Runtime | None = None) -> TenantDataSource | _LocalSQLMode:
    try:
        ctx = resolve_runtime_tenant_context(runtime)
    except MissingTenantContextError as exc:
        context = getattr(runtime, "context", None)
        if isinstance(context, dict) and isinstance(context.get("authorization_context"), dict):
            raise MissingTenantContextError("SaaS tenant context is missing; local MySQL fallback is disabled.") from exc
        return LOCAL_SQL_MODE
    return resolve_tenant_datasource(ctx)


def _resolve_tenant_context_or_none(runtime: Runtime | None = None) -> TenantContext | None:
    try:
        return resolve_runtime_tenant_context(runtime)
    except MissingTenantContextError:
        return None


def _resolve_saas_authorization(runtime: Runtime | None) -> AuthorizationContext:
    authorization = resolve_runtime_authorization_context(runtime)
    tenant = resolve_runtime_tenant_context(runtime)
    if authorization.tenant_id != tenant.tenant_id or authorization.tenant_code != tenant.tenant_code or authorization.system_code != tenant.system_code:
        raise MissingAuthorizationContextError("SaaS tenant and authorization context do not match.")
    if authorization.scope_mode == "scope_ref":
        raise MissingAuthorizationContextError("SaaS scope_ref must be resolved before SQL execution.")
    if authorization.scope_mode == "none":
        raise MissingAuthorizationContextError("SaaS authorization context denies data access.")
    return authorization


def _prepare_scoped_query(query: str, runtime: Runtime | None, source: TenantDataSource) -> ScopedQuery:
    return guard_sql_query(
        query,
        authorization=_resolve_saas_authorization(runtime),
        allowed_databases=source.allowed_databases,
        limit=DEFAULT_LIMIT,
    )


def _record_sql_scope_audit(
    runtime: Runtime | None,
    *,
    decision: str,
    query: str,
    operation: str = "sql_query",
    scoped: ScopedQuery | None = None,
    referenced_tables: list[str] | tuple[str, ...] = (),
    referenced_fields: list[str] | tuple[str, ...] = (),
    policy_version: str | None = None,
    scope_hash: str | None = None,
    reason: str | None = None,
    duration_ms: int | None = None,
    returned_rows: int | None = None,
    truncated: bool | None = None,
    error_category: str | None = None,
) -> None:
    context = getattr(runtime, "context", None)
    journal = context.get("__run_journal") if isinstance(context, dict) else None
    if journal is None or not hasattr(journal, "record_middleware"):
        return
    normalized = scoped.sql if scoped is not None else query
    try:
        authorization = resolve_runtime_authorization_context(runtime)
    except (MissingAuthorizationContextError, ValueError):
        authorization = None
    changes = {
        "run_id": context.get("run_id") if isinstance(context, dict) else None,
        "thread_id": context.get("thread_id") if isinstance(context, dict) else None,
        "tool_call_id": getattr(runtime, "tool_call_id", None),
        "principal_id": authorization.principal_id if authorization is not None else None,
        "tenant_id": authorization.tenant_id if authorization is not None else None,
        "system_code": authorization.system_code if authorization is not None else None,
        "permission_version": authorization.permission_version if authorization is not None else None,
        "operation": operation,
        "decision": decision,
        "normalized_sql_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "referenced_tables": list(scoped.referenced_tables) if scoped is not None else list(referenced_tables),
        "referenced_fields": list(scoped.referenced_fields) if scoped is not None else list(referenced_fields),
        "policy_version": scoped.policy_version if scoped is not None else policy_version,
        "scope_hash": scoped.scope_hash if scoped is not None else scope_hash or (authorization.scope_hash if authorization is not None else None),
        "scope_parameter_count": len(scoped.parameters) if scoped is not None else 0,
        "scope_predicates_applied": scoped.scope_predicates_applied if scoped is not None else 0,
        "reason": reason,
        "deny_reason": reason if decision == "deny" else None,
        "duration_ms": duration_ms,
        "returned_rows": returned_rows,
        "truncated": truncated,
        "error_category": error_category,
    }
    try:
        journal.record_middleware(
            tag="sql_scope",
            name="ScopedSqlExecutor",
            hook="tool",
            action=decision,
            changes=changes,
        )
    except Exception:
        pass


def _result_stats(result: object, *, limit: int = DEFAULT_LIMIT) -> tuple[int | None, bool | None]:
    parsed = result
    if isinstance(result, str):
        try:
            parsed = ast.literal_eval(result)
        except (SyntaxError, ValueError):
            return None, None
    if isinstance(parsed, (list, tuple)):
        count = len(parsed)
        return count, count >= limit
    return None, None


def _safe_table_info(db: SQLDatabase, table_name: str, policy: TableScopePolicy) -> str:
    from sqlalchemy import inspect

    engine = getattr(db, "_engine", None)
    if engine is None:
        raise TenantDataSourceError("Cannot inspect field-restricted table safely")
    columns = inspect(engine).get_columns(table_name)
    visible = []
    for column in columns:
        name = str(column["name"])
        if name in policy.hidden_fields:
            continue
        if policy.allowed_fields is not None and name not in policy.allowed_fields:
            continue
        nullable = "" if column.get("nullable", True) else " NOT NULL"
        classification = ""
        if name in policy.masked_fields:
            classification = " /* MASKED */"
        elif name in policy.aggregate_only_fields:
            classification = " /* AGGREGATE_ONLY */"
        visible.append(f"  `{name}` {column['type']}{nullable}{classification}")
    if not visible:
        raise TenantDataSourceError(f"Table {table_name!r} has no visible fields")
    return f"CREATE TABLE `{table_name}` (\n" + ",\n".join(visible) + "\n)"


def _default_database_for_runtime(runtime: Runtime | None = None) -> str:
    source = _resolve_sql_source(runtime)
    if source is LOCAL_SQL_MODE:
        return _get_default_database()
    assert isinstance(source, TenantDataSource)
    return source.database


def _ensure_database_allowed(source: TenantDataSource, database: str) -> None:
    if database in SYSTEM_DATABASES:
        raise TenantDataSourceError(f"Database {database!r} is not allowed")
    if database not in source.allowed_databases:
        raise TenantDataSourceError(f"Database {database!r} is not allowed for datasource {source.code!r}")


def _extract_explicit_databases(sql: str) -> set[str]:
    qualified_table_pattern = re.compile(
        r"\b(?:FROM|JOIN|UPDATE|INTO|TABLE)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\.",
        re.IGNORECASE,
    )
    show_tables_pattern = re.compile(
        r"\bSHOW\s+(?:FULL\s+)?TABLES\s+(?:FROM|IN)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
        re.IGNORECASE,
    )
    show_columns_pattern = re.compile(
        r"\bSHOW\s+(?:FULL\s+)?COLUMNS\s+FROM\s+`?[A-Za-z_][A-Za-z0-9_]*`?\s+(?:FROM|IN)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
        re.IGNORECASE,
    )
    return set(qualified_table_pattern.findall(sql)) | set(show_tables_pattern.findall(sql)) | set(show_columns_pattern.findall(sql))


def _validate_query_databases(query: str, source: TenantDataSource | _LocalSQLMode) -> tuple[bool, str]:
    if source is LOCAL_SQL_MODE:
        return True, ""
    assert isinstance(source, TenantDataSource)
    for database in _extract_explicit_databases(query):
        if database not in source.allowed_databases:
            return False, f"Database {database!r} is not allowed for datasource {source.code!r}"
    return True, ""


def _validate_table_databases(table_names: list[str], source: TenantDataSource | _LocalSQLMode) -> tuple[bool, str]:
    if source is LOCAL_SQL_MODE:
        return True, ""
    assert isinstance(source, TenantDataSource)
    for table_name in table_names:
        if "." not in table_name:
            continue
        database = table_name.split(".", 1)[0].strip("`")
        if database not in source.allowed_databases:
            return False, f"Database {database!r} is not allowed for datasource {source.code!r}"
    return True, ""


def _validate_sql(sql: str, allow_write: bool = False) -> tuple[bool, str]:
    sql_upper = sql.upper().strip()

    if not allow_write:
        for keyword in UPDATE_INSERT_KEYWORDS:
            pattern = r"(^|\s|;)\s*" + keyword + r"(\s|$|;)"
            if re.search(pattern, sql_upper):
                return False, f"Write operation blocked: {keyword}. Set MYSQL_ALLOW_WRITE=true to enable."

    for keyword in FORBIDDEN_KEYWORDS:
        if keyword == "SET" and allow_write and sql_upper.startswith(("UPDATE ", "INSERT ", "REPLACE ")):
            continue
        pattern = r"(^|\s|;)\s*" + keyword + r"(\s|$|;)"
        if re.search(pattern, sql_upper):
            return False, f"Forbidden operation detected: {keyword}"

    return True, ""


def _add_limit_if_needed(sql: str, limit: int = DEFAULT_LIMIT) -> str:
    sql_upper = sql.upper().strip()

    if sql_upper.startswith("SELECT") and "LIMIT" not in sql_upper:
        sql = sql.rstrip(";")
        sql = f"{sql} LIMIT {limit}"

    return sql


@tool("sql_show_databases", parse_docstring=True)
def sql_show_databases(pattern: str = "", runtime: Runtime = None) -> str:
    """Show databases matching a name pattern.

    Use this tool to find specific databases when you don't know the exact name.
    This is a lightweight discovery tool that only queries database names,
    not their tables or schemas.

    Args:
        pattern: Optional database name pattern. In SaaS mode this filters only the current tenant databases.

    Returns:
        List of matching database names.
    """
    started = time.monotonic()
    try:
        source = _resolve_sql_source(runtime)
        if source is not LOCAL_SQL_MODE:
            assert isinstance(source, TenantDataSource)
            authorization = _resolve_saas_authorization(runtime)
            registry = get_sql_scope_policy_registry()
            visible = [db for db in source.allowed_databases if not pattern or pattern in db]
            _record_sql_scope_audit(
                runtime,
                decision="allow",
                query="sql_show_databases",
                operation="sql_show_databases",
                policy_version=registry.version,
                scope_hash=authorization.scope_hash,
                duration_ms=int((time.monotonic() - started) * 1000),
                returned_rows=len(visible),
                truncated=False,
            )
            if not visible:
                return f"No tenant databases found matching pattern '{pattern}'."
            output = f"Tenant databases for datasource '{source.code}' ({len(visible)}):\n"
            for i, name in enumerate(visible, 1):
                output += f"  {i}. {name}\n"
            return output

        db = _get_db(database=None, runtime=runtime)

        if pattern:
            escaped_pattern = pattern.replace("\\", "\\\\").replace("'", "\\'")
            query = f"SHOW DATABASES LIKE '%{escaped_pattern}%'"
        else:
            query = "SHOW DATABASES"

        result = db.run(query)

        if not result:
            return f"No databases found matching pattern '{pattern}'."

        db_names = []
        if isinstance(result, str):
            import ast

            try:
                parsed = ast.literal_eval(result)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, (list, tuple)):
                            db_names.append(item[0] if item[0] else "")
                        elif isinstance(item, str):
                            db_names.append(item)
            except (ValueError, SyntaxError):
                for line in result.strip().split("\n"):
                    line = line.strip()
                    if line and not line.startswith("[") and not line.startswith("("):
                        db_names.append(line)
        elif isinstance(result, (list, tuple)):
            for item in result:
                if isinstance(item, (list, tuple)):
                    db_names.append(item[0] if item[0] else "")
                else:
                    db_names.append(str(item) if item else "")

        db_names = [name for name in db_names if name and name not in SYSTEM_DATABASES]

        if not db_names:
            return f"No databases found matching pattern '{pattern}'."

        if pattern:
            output = f"Found {len(db_names)} databases matching '{pattern}':\n"
        else:
            output = f"Found {len(db_names)} databases:\n"

        for i, name in enumerate(db_names, 1):
            output += f"  {i}. {name}\n"

        return output

    except (MissingAuthorizationContextError, MissingTenantContextError) as exc:
        _record_sql_scope_audit(
            runtime,
            decision="deny",
            query="sql_show_databases",
            operation="sql_show_databases",
            reason=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="AUTHORIZATION_DENIED",
        )
        return "Error showing databases: authorization denied."
    except Exception:
        _record_sql_scope_audit(
            runtime,
            decision="error",
            query="sql_show_databases",
            operation="sql_show_databases",
            reason="Database discovery failed",
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="DISCOVERY_FAILED",
        )
        return "Error showing databases: database discovery failed."


@tool("sql_list_tables", parse_docstring=True)
def sql_list_tables(database_name: str = "", runtime: Runtime = None) -> str:
    """List all available tables in the MySQL database.

    Use this tool first to discover what tables exist before querying.

    Args:
        database_name: Optional database name to list tables from. In SaaS mode, empty means current tenant database.

    Returns:
        A formatted list of available table names.
    """
    started = time.monotonic()
    try:
        source = _resolve_sql_source(runtime)
        target_db = database_name or (source.database if isinstance(source, TenantDataSource) else _get_default_database())

        if not target_db:
            return "Error: No database specified. Please provide database_name parameter or configure MYSQL_DATABASE in .env"

        authorization = None
        registry = None
        if isinstance(source, TenantDataSource):
            authorization = _resolve_saas_authorization(runtime)
            _ensure_database_allowed(source, target_db)
            registry = get_sql_scope_policy_registry()
        db = _get_db(database=target_db, runtime=runtime)
        tables = list(db.get_usable_table_names())
        if registry is not None:
            visible = set(registry.visible_tables())
            tables = [table for table in tables if table.lower() in visible]
            _record_sql_scope_audit(
                runtime,
                decision="allow",
                query="sql_list_tables",
                operation="sql_list_tables",
                referenced_tables=tuple(sorted(table.lower() for table in tables)),
                policy_version=registry.version,
                scope_hash=authorization.scope_hash if authorization is not None else None,
                duration_ms=int((time.monotonic() - started) * 1000),
                returned_rows=len(tables),
                truncated=False,
            )

        if not tables:
            return f"No tables found in database '{target_db}'."

        result = f"Available tables in '{target_db}' ({len(tables)}):\n"
        for i, table in enumerate(tables, 1):
            result += f"  {i}. {table}\n"

        return result

    except (MissingAuthorizationContextError, MissingTenantContextError, TenantDataSourceError) as exc:
        _record_sql_scope_audit(
            runtime,
            decision="deny",
            query="sql_list_tables",
            operation="sql_list_tables",
            reason=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="AUTHORIZATION_DENIED",
        )
        return "Error listing tables: authorization denied."
    except ValueError as e:
        return f"Configuration error: {e}"
    except Exception:
        _record_sql_scope_audit(
            runtime,
            decision="error",
            query="sql_list_tables",
            operation="sql_list_tables",
            reason="Table discovery failed",
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="DISCOVERY_FAILED",
        )
        return "Error listing tables: database discovery failed."


@tool("sql_schema", parse_docstring=True)
def sql_schema(table_names: str, runtime: Runtime = None) -> str:
    """Get schema information and sample rows for specified tables.

    Use this tool to understand table structure before writing queries.

    Args:
        table_names: Comma-separated list of table names to inspect.

    Returns:
        Schema information including columns, types, and sample data.
    """
    started = time.monotonic()
    try:
        tables = [t.strip() for t in table_names.split(",") if t.strip()]

        if not tables:
            return "Error: No table names provided. Example: 'users,orders' or 'db1.users,db2.orders'"

        source = _resolve_sql_source(runtime)
        default_db = source.database if isinstance(source, TenantDataSource) else _get_default_database()

        db_to_use = default_db
        for table in tables:
            if "." in table:
                db_to_use = table.split(".")[0].strip("`")
                break

        if not db_to_use:
            return "Error: No database specified. Please provide table names with database prefix (e.g., 'db1.users') or configure MYSQL_DATABASE in .env"

        is_valid, error_msg = _validate_table_databases(tables, source)
        if not is_valid:
            _record_sql_scope_audit(
                runtime,
                decision="deny",
                query="sql_schema",
                operation="sql_schema",
                referenced_tables=tuple(sorted(tables)),
                reason=error_msg,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_category="AUTHORIZATION_DENIED",
            )
            return f"Error getting schema: {error_msg}"

        registry = None
        authorization = None
        policies: dict[str, TableScopePolicy] = {}
        schema_tables = [table.split(".", 1)[1].strip("`") if "." in table else table for table in tables]
        if isinstance(source, TenantDataSource):
            authorization = _resolve_saas_authorization(runtime)
            registry = get_sql_scope_policy_registry()
            policies = {table: registry.policy_for(table) for table in schema_tables}

        db = _get_db(database=db_to_use, runtime=runtime)

        if default_db and "." not in tables[0]:
            available_tables = db.get_usable_table_names()
            invalid_tables = [t for t in tables if t not in available_tables and "." not in t]
            if invalid_tables:
                if registry is not None:
                    _record_sql_scope_audit(
                        runtime,
                        decision="deny",
                        query="sql_schema",
                        operation="sql_schema",
                        referenced_tables=tuple(sorted(table.lower() for table in schema_tables)),
                        policy_version=registry.version,
                        scope_hash=authorization.scope_hash if authorization is not None else None,
                        reason="Requested schema table was not found",
                        duration_ms=int((time.monotonic() - started) * 1000),
                        error_category="SCHEMA_NOT_FOUND",
                    )
                    return f"Error: Tables not found: {invalid_tables}."
                return f"Error: Tables not found: {invalid_tables}. Available: {available_tables}"

        if registry is None:
            schema_info = db.get_table_info(schema_tables)
        else:
            schema_info = "\n\n".join(_safe_table_info(db, table, policies[table]) for table in schema_tables)
            visible_fields = sorted({field for policy in policies.values() for field in (policy.allowed_fields or ()) if field not in policy.hidden_fields})
            _record_sql_scope_audit(
                runtime,
                decision="allow",
                query="sql_schema",
                operation="sql_schema",
                referenced_tables=tuple(sorted(table.lower() for table in schema_tables)),
                referenced_fields=tuple(visible_fields),
                policy_version=registry.version,
                scope_hash=authorization.scope_hash if authorization is not None else None,
                duration_ms=int((time.monotonic() - started) * 1000),
                returned_rows=len(schema_tables),
                truncated=False,
            )

        return f"Schema for tables: {tables}\n\n{schema_info}"

    except (MissingAuthorizationContextError, MissingTenantContextError, SqlScopeError, TenantDataSourceError) as exc:
        _record_sql_scope_audit(
            runtime,
            decision="deny",
            query="sql_schema",
            operation="sql_schema",
            referenced_tables=tuple(sorted(tables)) if "tables" in locals() else (),
            reason=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="AUTHORIZATION_DENIED",
        )
        return "Error getting schema: authorization denied."
    except ValueError as e:
        return f"Configuration error: {e}"
    except Exception:
        _record_sql_scope_audit(
            runtime,
            decision="error",
            query="sql_schema",
            operation="sql_schema",
            referenced_tables=tuple(sorted(tables)) if "tables" in locals() else (),
            reason="Schema inspection failed",
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="DISCOVERY_FAILED",
        )
        return "Error getting schema: schema inspection failed."


@tool("sql_query", parse_docstring=True)
def sql_query(query: str, runtime: Runtime = None) -> str:
    """Execute a SQL query on the MySQL database and return results.

    This tool validates the query for safety before execution.
    By default, only SELECT queries are allowed.

    Args:
        query: The SQL query to execute. Must be a valid MySQL query.

    Returns:
        Query results formatted as a table, or error message.
    """
    started = time.monotonic()
    allow_write = os.getenv("MYSQL_ALLOW_WRITE", "false").lower() == "true" and _resolve_tenant_context_or_none(runtime) is None

    is_valid, error_msg = _validate_sql(query, allow_write)
    if not is_valid:
        _record_sql_scope_audit(
            runtime,
            decision="deny",
            query=query,
            operation="sql_query",
            reason=error_msg,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="QUERY_REJECTED",
        )
        return f"Query blocked: {error_msg}"

    scoped: ScopedQuery | None = None
    try:
        source = _resolve_sql_source(runtime)
        if isinstance(source, TenantDataSource):
            try:
                scoped = _prepare_scoped_query(query, runtime, source)
            except (MissingAuthorizationContextError, SqlScopeError) as exc:
                _record_sql_scope_audit(
                    runtime,
                    decision="deny",
                    query=query,
                    operation="sql_query",
                    reason=str(exc),
                )
                return f"Query blocked: {exc}"
        is_valid, error_msg = _validate_query_databases(query, source)
        if not is_valid:
            return f"Query blocked: {error_msg}"

        default_db = source.database if isinstance(source, TenantDataSource) else _get_default_database()

        db_to_use = default_db
        if "." in query:
            match = re.search(r"FROM\s+([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)", query, re.IGNORECASE)
            if match:
                db_to_use = match.group(1)

        if not db_to_use:
            return "Error: No database specified. Please use fully qualified table names (e.g., 'db1.users') or configure MYSQL_DATABASE in .env"

        db = _get_db(database=db_to_use, runtime=runtime)

        if scoped is None:
            query = _add_limit_if_needed(query)
            result = db.run(query)
        else:
            result = db.run(
                scoped.sql,
                parameters=dict(scoped.parameters),
                execution_options={"timeout": 30},
            )
            returned_rows, truncated = _result_stats(result)
            _record_sql_scope_audit(
                runtime,
                decision="allow",
                query=query,
                operation="sql_query",
                scoped=scoped,
                duration_ms=int((time.monotonic() - started) * 1000),
                returned_rows=returned_rows,
                truncated=truncated,
            )

        if not result:
            return "Query returned no results."

        return result

    except (MissingTenantContextError, MissingAuthorizationContextError) as e:
        _record_sql_scope_audit(
            runtime,
            decision="deny",
            query=query,
            operation="sql_query",
            scoped=scoped,
            reason=str(e),
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="AUTHORIZATION_DENIED",
        )
        return f"Query blocked: {e}"
    except ValueError as e:
        return f"Configuration error: {e}"
    except Exception as e:
        error_str = str(e)
        _record_sql_scope_audit(
            runtime,
            decision="error",
            query=query,
            operation="sql_query",
            scoped=scoped,
            reason="Database operation failed",
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="QUERY_EXECUTION_FAILED",
        )

        if "Unknown column" in error_str:
            match = re.search(r"Unknown column '([^']+)'", error_str)
            if match:
                col_name = match.group(1)
                return f"SQL Error: Unknown column '{col_name}'. Use sql_schema to check available columns."
        elif "Table" in error_str and "doesn't exist" in error_str:
            match = re.search(r"Table '([^']+)'", error_str)
            if match:
                table_name = match.group(1)
                return f"SQL Error: Table '{table_name}' doesn't exist. Use sql_list_tables to check available tables."
        elif "Unknown database" in error_str:
            match = re.search(r"Unknown database '([^']+)'", error_str)
            if match:
                db_name = match.group(1)
                return f"SQL Error: Unknown database '{db_name}'. Please check the database name."
        elif "syntax" in error_str.lower():
            return f"SQL Syntax Error: {e}. Please check your query syntax."

        return "Error executing query: database operation failed."


@tool("sql_query_checker", parse_docstring=True)
def sql_query_checker(query: str, runtime: Runtime = None) -> str:
    """Validate a SQL query for correctness before execution.

    Use this tool to double-check your query syntax before running it.

    Args:
        query: The SQL query to validate.

    Returns:
        Validation result indicating if the query is valid or has errors.
    """
    started = time.monotonic()
    allow_write = os.getenv("MYSQL_ALLOW_WRITE", "false").lower() == "true" and _resolve_tenant_context_or_none(runtime) is None

    is_valid, error_msg = _validate_sql(query, allow_write)
    if not is_valid:
        _record_sql_scope_audit(
            runtime,
            decision="deny",
            query=query,
            operation="sql_query_checker",
            reason=error_msg,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="QUERY_REJECTED",
        )
        return f"Validation failed: {error_msg}"

    scoped: ScopedQuery | None = None
    try:
        source = _resolve_sql_source(runtime)
        if isinstance(source, TenantDataSource):
            try:
                scoped = _prepare_scoped_query(query, runtime, source)
            except (MissingAuthorizationContextError, SqlScopeError) as exc:
                _record_sql_scope_audit(
                    runtime,
                    decision="deny",
                    query=query,
                    operation="sql_query_checker",
                    reason=str(exc),
                )
                return f"Validation failed: {exc}"
        is_valid, error_msg = _validate_query_databases(query, source)
        if not is_valid:
            return f"Validation failed: {error_msg}"

        default_db = source.database if isinstance(source, TenantDataSource) else _get_default_database()

        db_to_use = default_db
        if "." in query:
            match = re.search(r"FROM\s+([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)", query, re.IGNORECASE)
            if match:
                db_to_use = match.group(1)

        if not db_to_use:
            return "Validation skipped: No database specified. Query syntax check only."

        db = _get_db(database=db_to_use, runtime=runtime)

        checked_query = scoped.sql if scoped is not None else query
        parameters = dict(scoped.parameters) if scoped is not None else None
        db.run(
            f"EXPLAIN {checked_query}",
            parameters=parameters,
            execution_options={"timeout": 30},
        )

        if scoped is not None:
            _record_sql_scope_audit(
                runtime,
                decision="allow",
                query=query,
                operation="sql_query_checker",
                scoped=scoped,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        return f"Query is valid and ready to execute.\nQuery: {checked_query}"

    except (MissingTenantContextError, MissingAuthorizationContextError) as e:
        _record_sql_scope_audit(
            runtime,
            decision="deny",
            query=query,
            operation="sql_query_checker",
            scoped=scoped,
            reason=str(e),
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="AUTHORIZATION_DENIED",
        )
        return f"Validation failed: {e}"
    except Exception:
        _record_sql_scope_audit(
            runtime,
            decision="error",
            query=query,
            operation="sql_query_checker",
            scoped=scoped,
            reason="Query validation failed",
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category="QUERY_VALIDATION_FAILED",
        )
        return "Query validation failed: database operation failed."


SQL_TOOLS = [
    sql_show_databases,
    sql_list_tables,
    sql_schema,
    sql_query,
    sql_query_checker,
]
