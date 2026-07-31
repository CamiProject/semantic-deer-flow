"""Runtime services for authorized ontology and semantic queries."""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langchain_community.utilities import SQLDatabase

from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.runtime.tenant_context import TenantContext
from deerflow.semantic.ontology import OntologyRegistry
from deerflow.semantic.query import (
    CompiledSemanticQuery,
    SemanticFilter,
    SemanticOrder,
    compile_metric_query,
    compile_metrics_query,
    compile_object_query,
)
from deerflow.semantic.sql_scope import SqlScopePolicyRegistry
from deerflow.tools.builtins.tenant_datasource import (
    TenantDataSource,
    build_tenant_mysql_uri,
    resolve_tenant_datasource,
)


class SemanticRuntimeError(RuntimeError):
    """Raised when a semantic query cannot be executed."""


def authorization_tenant_context(authorization: AuthorizationContext) -> TenantContext:
    return TenantContext(
        user_id=authorization.principal_id,
        tenant_id=authorization.tenant_id,
        tenant_code=authorization.tenant_code,
        tenant_name=None,
        system_code=authorization.system_code,
    )


def _normalize_rows(result: Any) -> list[dict[str, Any]]:
    if not result:
        return []
    if isinstance(result, list):
        return [dict(row) if isinstance(row, Mapping) else {"value": row} for row in result]
    if isinstance(result, tuple):
        return [{"value": result}]
    if isinstance(result, str):
        try:
            parsed = ast.literal_eval(result)
        except (SyntaxError, ValueError):
            return [{"value": result}]
        return _normalize_rows(parsed)
    return [{"value": result}]


@dataclass(frozen=True)
class SemanticQueryResult:
    rows: list[dict[str, Any]]
    columns: tuple[str, ...]
    semantic_kind: str
    semantic_id: str
    semantic_version: str
    ontology_version: str
    policy_version: str
    scope_hash: str
    source_refs: tuple[str, ...]
    truncated: bool
    as_of: str
    semantic_ids: tuple[str, ...]
    semantic_versions: Mapping[str, str]
    normalized_query_hash: str
    referenced_fields: tuple[str, ...]
    scope_predicates_applied: int

    @property
    def authorization_scope_hash(self) -> str:
        return self.scope_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "columns": list(self.columns),
            "semantic_kind": self.semantic_kind,
            "semantic_id": self.semantic_id,
            "semantic_version": self.semantic_version,
            "ontology_version": self.ontology_version,
            "policy_version": self.policy_version,
            "scope_hash": self.scope_hash,
            "authorization_scope_hash": self.authorization_scope_hash,
            "source_refs": list(self.source_refs),
            "truncated": self.truncated,
            "as_of": self.as_of,
            "semantic_ids": list(self.semantic_ids),
            "semantic_versions": dict(self.semantic_versions),
            "normalized_query_hash": self.normalized_query_hash,
            "referenced_fields": list(self.referenced_fields),
            "scope_predicates_applied": self.scope_predicates_applied,
        }


class SemanticQueryRuntime:
    """Compile and execute semantic requests against one tenant datasource."""

    def __init__(
        self,
        *,
        ontology: OntologyRegistry,
        sql_policy: SqlScopePolicyRegistry,
        datasource_resolver: Callable[[TenantContext], TenantDataSource] = resolve_tenant_datasource,
        database_factory: Callable[[str], SQLDatabase] | None = None,
    ) -> None:
        self.ontology = ontology
        self.sql_policy = sql_policy
        self._datasource_resolver = datasource_resolver
        self._database_factory = database_factory or (lambda uri: SQLDatabase.from_uri(uri, sample_rows_in_table_info=0))

    def _datasource(self, authorization: AuthorizationContext) -> TenantDataSource:
        return self._datasource_resolver(authorization_tenant_context(authorization))

    def _execute(
        self,
        compiled: CompiledSemanticQuery,
        datasource: TenantDataSource,
        *,
        requested_limit: int,
    ) -> SemanticQueryResult:
        database = self._database_factory(build_tenant_mysql_uri(datasource))
        raw = database.run(
            compiled.scoped_query.sql,
            include_columns=True,
            parameters=dict(compiled.scoped_query.parameters),
            execution_options={"timeout": 30},
        )
        rows = _normalize_rows(raw)
        return SemanticQueryResult(
            rows=rows,
            columns=compiled.columns,
            semantic_kind=compiled.semantic_kind,
            semantic_id=compiled.semantic_id,
            semantic_version=compiled.semantic_version,
            ontology_version=compiled.ontology_version,
            policy_version=compiled.scoped_query.policy_version,
            scope_hash=compiled.scoped_query.scope_hash,
            source_refs=compiled.scoped_query.referenced_tables,
            truncated=len(rows) >= requested_limit,
            as_of=datetime.now(UTC).isoformat(),
            semantic_ids=compiled.semantic_ids,
            semantic_versions=compiled.semantic_versions,
            normalized_query_hash=hashlib.sha256(compiled.scoped_query.sql.encode("utf-8")).hexdigest(),
            referenced_fields=compiled.scoped_query.referenced_fields,
            scope_predicates_applied=compiled.scoped_query.scope_predicates_applied,
        )

    def search_objects(
        self,
        *,
        authorization: AuthorizationContext,
        object_type: str,
        filters: Sequence[SemanticFilter] = (),
        properties: Sequence[str] | None = None,
        limit: int = 100,
    ) -> SemanticQueryResult:
        datasource = self._datasource(authorization)
        compiled = compile_object_query(
            registry=self.ontology,
            object_type=object_type,
            authorization=authorization,
            allowed_databases=datasource.allowed_databases,
            filters=filters,
            properties=properties,
            limit=limit,
            policy_registry=self.sql_policy,
        )
        return self._execute(compiled, datasource, requested_limit=limit)

    def get_object(
        self,
        *,
        authorization: AuthorizationContext,
        object_type: str,
        object_id: str,
    ) -> SemanticQueryResult:
        datasource = self._datasource(authorization)
        compiled = compile_object_query(
            registry=self.ontology,
            object_type=object_type,
            object_id=object_id,
            authorization=authorization,
            allowed_databases=datasource.allowed_databases,
            limit=1,
            policy_registry=self.sql_policy,
        )
        return self._execute(compiled, datasource, requested_limit=1)

    def query_metric(
        self,
        *,
        authorization: AuthorizationContext,
        metric_id: str,
        dimensions: Sequence[str] = (),
        filters: Sequence[SemanticFilter] = (),
        order_by: Sequence[SemanticOrder] = (),
        limit: int = 100,
    ) -> SemanticQueryResult:
        datasource = self._datasource(authorization)
        compiled = compile_metric_query(
            registry=self.ontology,
            metric_id=metric_id,
            authorization=authorization,
            allowed_databases=datasource.allowed_databases,
            dimensions=dimensions,
            filters=filters,
            order_by=order_by,
            limit=limit,
            policy_registry=self.sql_policy,
        )
        return self._execute(compiled, datasource, requested_limit=limit)

    def query_metrics(
        self,
        *,
        authorization: AuthorizationContext,
        metric_ids: Sequence[str],
        dimensions: Sequence[str] = (),
        filters: Sequence[SemanticFilter] = (),
        order_by: Sequence[SemanticOrder] = (),
        limit: int = 100,
    ) -> SemanticQueryResult:
        datasource = self._datasource(authorization)
        compiled = compile_metrics_query(
            registry=self.ontology,
            metric_ids=metric_ids,
            authorization=authorization,
            allowed_databases=datasource.allowed_databases,
            dimensions=dimensions,
            filters=filters,
            order_by=order_by,
            limit=limit,
            policy_registry=self.sql_policy,
        )
        return self._execute(compiled, datasource, requested_limit=limit)

    def resolve_business_context(
        self,
        *,
        authorization: AuthorizationContext,
        question: str,
        include_facts: bool = True,
        fact_limit: int = 5,
    ) -> dict[str, Any]:
        context = self.ontology.resolve(question, authorization=authorization)
        facts: list[dict[str, Any]] = []
        source_refs: set[str] = set()
        as_of = datetime.now(UTC).isoformat()
        if include_facts:
            for candidate in context["objects"][:3]:
                result = self.search_objects(
                    authorization=authorization,
                    object_type=candidate["id"],
                    limit=fact_limit,
                )
                facts.append(
                    {
                        "object_type": candidate["id"],
                        "rows": result.rows,
                        "source_refs": list(result.source_refs),
                        "as_of": result.as_of,
                        "authorization_scope_hash": result.authorization_scope_hash,
                    }
                )
                source_refs.update(result.source_refs)
        for obj in context["objects"]:
            source_refs.update(obj.get("source_refs") or ())
        for metric in context["metrics"]:
            source_refs.update(metric.get("source_refs") or ())
        return {
            **context,
            "facts": facts,
            "source_refs": sorted(source_refs),
            "as_of": as_of,
            "authorization_scope_hash": authorization.scope_hash,
            "scope_hash": authorization.scope_hash,
        }
