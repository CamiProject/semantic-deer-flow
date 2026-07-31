"""AST-based SQL policy enforcement for trusted SaaS authorization scopes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from deerflow.runtime.authorization_context import AuthorizationContext

SQL_SCOPE_POLICY_PATH_ENV_VAR = "DEER_FLOW_SQL_SCOPE_POLICY_PATH"
DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 1000
MAX_QUERY_OFFSET = 10000
SYSTEM_DATABASES = frozenset({"mysql", "information_schema", "performance_schema", "sys"})
_ACCESS_MODES = frozenset({"scoped", "reference", "forbidden"})
_SCOPE_DIMENSIONS = frozenset({"site", "project"})
_FORBIDDEN_SELECT_NODES = frozenset(
    {
        "Command",
        "CurrentCatalog",
        "CurrentRole",
        "CurrentSchema",
        "CurrentUser",
        "Hint",
        "Into",
        "Lock",
        "Parameter",
        "PropertyEQ",
        "SessionParameter",
        "Transaction",
        "Use",
    }
)
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "benchmark",
        "connection_id",
        "current_catalog",
        "current_role",
        "current_schema",
        "current_user",
        "database",
        "get_lock",
        "is_free_lock",
        "is_used_lock",
        "last_insert_id",
        "load_file",
        "master_pos_wait",
        "release_all_locks",
        "release_lock",
        "row_count",
        "schema",
        "session_user",
        "sleep",
        "system_user",
        "sys_eval",
        "sys_exec",
        "user",
        "version",
        "found_rows",
    }
)


class SqlScopeError(ValueError):
    """Raised when SQL cannot be proven safe for the current scope."""


@dataclass(frozen=True)
class TableScopePolicy:
    access: str
    scope_dimension: str | None = None
    scope_column: str | None = None
    allowed_fields: frozenset[str] | None = None
    hidden_fields: frozenset[str] = frozenset()
    masked_fields: frozenset[str] = frozenset()
    aggregate_only_fields: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(cls, table_name: str, value: Mapping[str, Any]) -> TableScopePolicy:
        access = str(value.get("access") or "").strip().lower()
        if access not in _ACCESS_MODES:
            raise SqlScopeError(f"Table {table_name!r} has invalid access policy")
        scope_dimension = value.get("scope_dimension")
        scope_dimension = str(scope_dimension).strip().lower() if scope_dimension is not None else None
        scope_column = value.get("scope_column")
        scope_column = str(scope_column).strip() if scope_column is not None else None
        if access == "scoped" and (scope_dimension not in _SCOPE_DIMENSIONS or not scope_column):
            raise SqlScopeError(f"Table {table_name!r} requires scope_dimension and scope_column")
        allowed = value.get("allowed_fields")
        allowed_fields = frozenset(str(item).strip() for item in allowed) if isinstance(allowed, list) else None
        hidden = value.get("hidden_fields") or []
        masked = value.get("masked_fields") or []
        aggregate_only = value.get("aggregate_only_fields") or []
        if not all(isinstance(item, list) for item in (hidden, masked, aggregate_only)):
            raise SqlScopeError(f"Table {table_name!r} field classifications must be lists")
        hidden_fields = frozenset(str(item).strip() for item in hidden)
        masked_fields = frozenset(str(item).strip() for item in masked)
        aggregate_only_fields = frozenset(str(item).strip() for item in aggregate_only)
        if hidden_fields.intersection(masked_fields) or hidden_fields.intersection(aggregate_only_fields) or masked_fields.intersection(aggregate_only_fields):
            raise SqlScopeError(f"Table {table_name!r} field classifications must not overlap")
        return cls(
            access=access,
            scope_dimension=scope_dimension,
            scope_column=scope_column,
            allowed_fields=allowed_fields,
            hidden_fields=hidden_fields,
            masked_fields=masked_fields,
            aggregate_only_fields=aggregate_only_fields,
        )


@dataclass(frozen=True)
class SqlScopePolicyRegistry:
    version: str
    tables: Mapping[str, TableScopePolicy]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SqlScopePolicyRegistry:
        version = str(value.get("version") or "").strip()
        tables = value.get("tables")
        if not version or not isinstance(tables, Mapping):
            raise SqlScopeError("SQL scope policy requires version and tables")
        parsed = {str(name).strip().lower(): TableScopePolicy.from_mapping(str(name), policy) for name, policy in tables.items() if isinstance(policy, Mapping)}
        return cls(version=version, tables=parsed)

    @classmethod
    def from_file(cls, path: str | Path) -> SqlScopePolicyRegistry:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = yaml.safe_load(handle) or {}
        if not isinstance(value, Mapping):
            raise SqlScopeError("SQL scope policy must be a mapping")
        return cls.from_mapping(value)

    def policy_for(self, table_name: str) -> TableScopePolicy:
        policy = self.tables.get(table_name.lower())
        if policy is None or policy.access == "forbidden":
            raise SqlScopeError(f"Table {table_name!r} is not allowed by SQL scope policy")
        return policy

    def visible_tables(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, policy in self.tables.items() if policy.access != "forbidden"))


@lru_cache(maxsize=4)
def _load_policy(path: str) -> SqlScopePolicyRegistry:
    return SqlScopePolicyRegistry.from_file(path)


def get_sql_scope_policy_registry() -> SqlScopePolicyRegistry:
    configured = os.environ.get(SQL_SCOPE_POLICY_PATH_ENV_VAR)
    path = Path(configured) if configured else Path(__file__).with_name("default_sql_scope_policy.yaml")
    return _load_policy(str(path.resolve()))


@dataclass(frozen=True)
class ScopedQuery:
    sql: str
    parameters: Mapping[str, Any]
    referenced_tables: tuple[str, ...]
    policy_version: str
    scope_hash: str
    referenced_fields: tuple[str, ...] = ()
    scope_predicates_applied: int = 0


def _scope_values(context: AuthorizationContext, dimension: str) -> tuple[str, ...]:
    if context.scope_mode == "tenant_all":
        return ()
    if context.scope_mode == "none":
        raise SqlScopeError("Authorization scope denies data access")
    if context.scope_mode == "scope_ref":
        raise SqlScopeError("scope_ref must be resolved before SQL execution")
    values = context.allowed_site_ids if dimension == "site" else context.allowed_project_ids
    if not values:
        raise SqlScopeError(f"Authorization scope contains no {dimension} resources")
    return values


def _inside_aggregate(expression: exp.Expression) -> bool:
    parent = expression.parent
    while parent is not None and not isinstance(parent, exp.Select):
        if isinstance(parent, exp.AggFunc):
            return True
        parent = parent.parent
    return False


def _validate_columns(statement: exp.Expression, aliases: Mapping[str, TableScopePolicy]) -> None:
    for star in statement.find_all(exp.Star):
        if _inside_aggregate(star):
            continue
        table_name = str(star.parent.args.get("table") or "").lower() if star.parent else ""
        candidates = [aliases[table_name]] if table_name in aliases else list(aliases.values())
        if any(policy.hidden_fields or policy.masked_fields or policy.aggregate_only_fields or policy.allowed_fields is not None for policy in candidates):
            raise SqlScopeError("SELECT * is not allowed for field-restricted tables")

    for column in statement.find_all(exp.Column):
        name = column.name.lower()
        qualifier = column.table.lower()
        candidates = [aliases[qualifier]] if qualifier in aliases else list(aliases.values())
        if any(name in policy.hidden_fields for policy in candidates):
            raise SqlScopeError(f"Field {name!r} is hidden by SQL scope policy")
        if any(name in policy.masked_fields for policy in candidates):
            raise SqlScopeError(f"Field {name!r} is masked and cannot be selected by SQL agents")
        if any(name in policy.aggregate_only_fields for policy in candidates) and not _inside_aggregate(column):
            raise SqlScopeError(f"Field {name!r} is aggregate-only by SQL scope policy")
        restrictive = [policy for policy in candidates if policy.allowed_fields is not None]
        if restrictive and not all(name in (policy.allowed_fields or ()) for policy in restrictive):
            raise SqlScopeError(f"Field {name!r} is not allowed by SQL scope policy")


def _parameterized_predicate(column: str, prefix: str, values: tuple[str, ...]) -> tuple[exp.Expression, dict[str, str]]:
    parameters = {f"{prefix}_{index}": value for index, value in enumerate(values)}
    placeholders = [exp.Placeholder(this=name) for name in parameters]
    return exp.In(this=exp.column(column), expressions=placeholders), parameters


def guard_sql_query(
    query: str,
    *,
    authorization: AuthorizationContext,
    allowed_databases: tuple[str, ...],
    registry: SqlScopePolicyRegistry | None = None,
    limit: int = DEFAULT_QUERY_LIMIT,
) -> ScopedQuery:
    """Parse, authorize and scope one read-only MySQL query."""
    registry = registry or get_sql_scope_policy_registry()
    try:
        statements = parse(query, read="mysql")
    except ParseError as exc:
        raise SqlScopeError("SQL could not be parsed") from exc
    if len(statements) != 1:
        raise SqlScopeError("Exactly one SQL statement is required")
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise SqlScopeError("Only SELECT queries are allowed")
    for node in statement.walk():
        if type(node).__name__ in _FORBIDDEN_SELECT_NODES:
            raise SqlScopeError(f"SQL construct {type(node).__name__!r} is not allowed")
        if isinstance(node, exp.Func):
            function_name = (str(node.name) if isinstance(node, exp.Anonymous) else str(node.sql_name() or getattr(node, "name", ""))).lower()
            if function_name in _FORBIDDEN_FUNCTIONS:
                raise SqlScopeError(f"SQL function {function_name!r} is not allowed")

    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    shadowed_policy_tables = cte_names.intersection(registry.tables)
    if shadowed_policy_tables:
        names = ", ".join(sorted(shadowed_policy_tables))
        raise SqlScopeError(f"CTE alias shadows SQL policy table: {names}")
    table_nodes = [table for table in statement.find_all(exp.Table) if table.db or table.name.lower() not in cte_names]
    if not table_nodes:
        raise SqlScopeError("Query must reference at least one authorized table")

    aliases: dict[str, TableScopePolicy] = {}
    table_policies: list[tuple[exp.Table, TableScopePolicy]] = []
    referenced: set[str] = set()
    allowed_database_set = {name.lower() for name in allowed_databases}
    for table in table_nodes:
        database = table.db.lower()
        if database and (database in SYSTEM_DATABASES or database not in allowed_database_set):
            raise SqlScopeError(f"Database {table.db!r} is not allowed")
        name = table.name.lower()
        policy = registry.policy_for(name)
        referenced.add(name)
        aliases[table.alias_or_name.lower()] = policy
        aliases.setdefault(name, policy)
        table_policies.append((table, policy))

    _validate_columns(statement, aliases)

    parameters: dict[str, Any] = {}
    scope_predicates_applied = 0
    for index, (table, policy) in enumerate(table_policies):
        if policy.access != "scoped" or authorization.scope_mode == "tenant_all":
            continue
        assert policy.scope_dimension is not None
        assert policy.scope_column is not None
        values = _scope_values(authorization, policy.scope_dimension)
        predicate, scoped_parameters = _parameterized_predicate(policy.scope_column, f"scope_{index}", values)
        parameters.update(scoped_parameters)

        alias = table.alias_or_name
        source = table.copy()
        source.set("alias", None)
        scoped_source = exp.select("*").from_(source).where(predicate).subquery(alias)
        table.replace(scoped_source)
        scope_predicates_applied += 1

    bounded_limit = max(1, min(int(limit), MAX_QUERY_LIMIT))
    for limit_node in statement.find_all(exp.Limit):
        expression = limit_node.expression
        if not isinstance(expression, exp.Literal) or not expression.is_int:
            raise SqlScopeError("LIMIT must be a fixed integer")
        requested = int(expression.this)
        if requested > bounded_limit:
            limit_node.set("expression", exp.Literal.number(bounded_limit))
    for offset_node in statement.find_all(exp.Offset):
        expression = offset_node.expression
        if not isinstance(expression, exp.Literal) or not expression.is_int:
            raise SqlScopeError("OFFSET must be a fixed integer")
        if int(expression.this) > MAX_QUERY_OFFSET:
            raise SqlScopeError(f"OFFSET exceeds {MAX_QUERY_OFFSET}")
    if statement.args.get("limit") is None:
        statement = statement.limit(bounded_limit)

    return ScopedQuery(
        sql=statement.sql(dialect="mysql"),
        parameters=parameters,
        referenced_tables=tuple(sorted(referenced)),
        policy_version=registry.version,
        scope_hash=authorization.scope_hash,
        referenced_fields=tuple(sorted({column.name.lower() for column in statement.find_all(exp.Column)})),
        scope_predicates_applied=scope_predicates_applied,
    )
