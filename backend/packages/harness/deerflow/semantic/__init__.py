"""Business semantic and scoped data-access primitives."""

from .sql_scope import (
    ScopedQuery,
    SqlScopeError,
    SqlScopePolicyRegistry,
    TableScopePolicy,
    guard_sql_query,
)

__all__ = [
    "ScopedQuery",
    "SqlScopeError",
    "SqlScopePolicyRegistry",
    "TableScopePolicy",
    "guard_sql_query",
]
