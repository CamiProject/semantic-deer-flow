"""Compilation of typed semantic requests into scoped SQL."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from sqlglot import exp

from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.semantic.ontology import MetricDefinition, ObjectDefinition, OntologyError, OntologyRegistry
from deerflow.semantic.sql_scope import ScopedQuery, SqlScopePolicyRegistry, guard_sql_query

_FILTER_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "in"})
_MAX_IN_FILTER_VALUES = 100


@dataclass(frozen=True)
class SemanticFilter:
    field: str
    operator: str
    value: Any

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SemanticFilter:
        field = str(value.get("field") or "").strip()
        operator = str(value.get("op") or value.get("operator") or "").strip().lower()
        if not field or operator not in _FILTER_OPERATORS:
            raise OntologyError("Invalid semantic filter")
        return cls(field=field, operator=operator, value=value.get("value"))


@dataclass(frozen=True)
class SemanticOrder:
    field: str
    direction: str = "asc"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SemanticOrder:
        field = str(value.get("field") or "").strip()
        direction = str(value.get("direction") or "asc").strip().lower()
        if not field or direction not in {"asc", "desc"}:
            raise OntologyError("Invalid semantic order")
        return cls(field=field, direction=direction)


@dataclass(frozen=True)
class CompiledSemanticQuery:
    scoped_query: ScopedQuery
    columns: tuple[str, ...]
    ontology_version: str
    semantic_kind: str
    semantic_id: str
    semantic_version: str
    semantic_ids: tuple[str, ...] = ()
    semantic_versions: Mapping[str, str] = dataclass_field(default_factory=dict)


def _condition(column: str, operator: str, parameter_prefix: str, value: Any) -> tuple[exp.Expression, dict[str, Any]]:
    col = exp.column(column)
    if operator == "in":
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
            raise OntologyError("IN filter requires a non-empty list")
        if len(value) > _MAX_IN_FILTER_VALUES:
            raise OntologyError("IN filter exceeds the semantic filter budget")
        parameters = {f"{parameter_prefix}_{index}": item for index, item in enumerate(value)}
        return exp.In(this=col, expressions=[exp.Placeholder(this=name) for name in parameters]), parameters
    parameter = parameter_prefix
    placeholder = exp.Placeholder(this=parameter)
    operators = {
        "eq": exp.EQ,
        "ne": exp.NEQ,
        "gt": exp.GT,
        "gte": exp.GTE,
        "lt": exp.LT,
        "lte": exp.LTE,
    }
    return operators[operator](this=col, expression=placeholder), {parameter: value}


def _apply_filters(
    statement: exp.Select,
    *,
    obj: ObjectDefinition,
    filters: Sequence[SemanticFilter],
    allowed_fields: tuple[str, ...] | None = None,
) -> tuple[exp.Select, dict[str, Any]]:
    parameters: dict[str, Any] = {}
    for index, semantic_filter in enumerate(filters):
        if allowed_fields is not None and semantic_filter.field not in allowed_fields:
            raise OntologyError(f"Filter {semantic_filter.field!r} is not allowed")
        prop = obj.property(semantic_filter.field)
        if not prop.filterable:
            raise OntologyError(f"Property {semantic_filter.field!r} is not filterable")
        condition, values = _condition(
            prop.column,
            semantic_filter.operator,
            f"filter_{index}",
            semantic_filter.value,
        )
        parameters.update(values)
        statement = statement.where(condition)
    return statement, parameters


def _guard(
    sql: str,
    *,
    authorization: AuthorizationContext,
    allowed_databases: tuple[str, ...],
    policy_registry: SqlScopePolicyRegistry | None,
    limit: int,
) -> ScopedQuery:
    return guard_sql_query(
        sql,
        authorization=authorization,
        allowed_databases=allowed_databases,
        registry=policy_registry,
        limit=limit,
    )


def compile_object_query(
    *,
    registry: OntologyRegistry,
    object_type: str,
    authorization: AuthorizationContext,
    allowed_databases: tuple[str, ...],
    filters: Sequence[SemanticFilter] = (),
    properties: Sequence[str] | None = None,
    object_id: str | None = None,
    limit: int = 100,
    policy_registry: SqlScopePolicyRegistry | None = None,
) -> CompiledSemanticQuery:
    obj = registry.authorize_object(object_type, authorization)
    if properties is None:
        selected = tuple(name for name in obj.properties if registry._role_allowed(obj.property(name).allowed_roles, authorization))
    else:
        selected = tuple(properties)
    if not selected:
        raise OntologyError("Object query requires at least one property")
    expressions = [
        exp.alias_(
            exp.column(registry.authorize_property(obj.name, name, authorization).column),
            name,
            quoted=True,
        )
        for name in selected
    ]
    statement = exp.select(*expressions).from_(obj.table)
    query_filters = list(filters)
    if object_id is not None:
        query_filters.append(SemanticFilter(field=obj.id_field, operator="eq", value=object_id))
    for semantic_filter in query_filters:
        registry.authorize_property(obj.name, semantic_filter.field, authorization)
    statement, parameters = _apply_filters(statement, obj=obj, filters=query_filters)
    scoped = _guard(
        statement.sql(dialect="mysql"),
        authorization=authorization,
        allowed_databases=allowed_databases,
        policy_registry=policy_registry,
        limit=limit,
    )
    return CompiledSemanticQuery(
        scoped_query=ScopedQuery(
            sql=scoped.sql,
            parameters={**parameters, **scoped.parameters},
            referenced_tables=scoped.referenced_tables,
            policy_version=scoped.policy_version,
            scope_hash=scoped.scope_hash,
            referenced_fields=scoped.referenced_fields,
            scope_predicates_applied=scoped.scope_predicates_applied,
        ),
        columns=selected,
        ontology_version=registry.version,
        semantic_kind="object",
        semantic_id=obj.name,
        semantic_version=registry.version,
        semantic_ids=(obj.name,),
        semantic_versions={obj.name: obj.version},
    )


def _aggregate(metric: MetricDefinition, obj: ObjectDefinition) -> exp.Expression:
    if metric.aggregation == "count":
        return exp.Count(this=exp.Star())
    assert metric.field is not None
    column = exp.column(obj.property(metric.field).column)
    functions = {
        "sum": exp.Sum,
        "avg": exp.Avg,
        "min": exp.Min,
        "max": exp.Max,
    }
    return functions[metric.aggregation](this=column)


def compile_metrics_query(
    *,
    registry: OntologyRegistry,
    metric_ids: Sequence[str],
    authorization: AuthorizationContext,
    allowed_databases: tuple[str, ...],
    dimensions: Sequence[str] = (),
    filters: Sequence[SemanticFilter] = (),
    order_by: Sequence[SemanticOrder] = (),
    limit: int = 100,
    policy_registry: SqlScopePolicyRegistry | None = None,
) -> CompiledSemanticQuery:
    unique_metric_ids = tuple(dict.fromkeys(str(item).strip() for item in metric_ids if str(item).strip()))
    if not unique_metric_ids:
        raise OntologyError("Metric query requires at least one metric")
    if len(unique_metric_ids) > 10:
        raise OntologyError("Metric query exceeds the metric budget")
    metrics = tuple(registry.authorize_metric(metric_id, authorization) for metric_id in unique_metric_ids)
    object_types = {metric.object_type for metric in metrics}
    if len(object_types) != 1:
        raise OntologyError("Metrics from different object types cannot share one query")
    obj = registry.object(metrics[0].object_type)
    allowed_dimensions = set(metrics[0].dimensions)
    allowed_filters = set(metrics[0].filters)
    for metric in metrics[1:]:
        allowed_dimensions.intersection_update(metric.dimensions)
        allowed_filters.intersection_update(metric.filters)
    unknown_dimensions = set(dimensions) - allowed_dimensions
    if unknown_dimensions:
        raise OntologyError(f"Metric dimensions are not allowed: {', '.join(sorted(unknown_dimensions))}")
    for field_name in (*dimensions, *(semantic_filter.field for semantic_filter in filters)):
        registry.authorize_property(obj.name, field_name, authorization)

    dimension_exprs = [exp.alias_(exp.column(obj.property(name).column), name, quoted=True) for name in dimensions]
    metric_exprs = [exp.alias_(_aggregate(metric, obj), metric.name, quoted=True) for metric in metrics]
    statement = exp.select(*dimension_exprs, *metric_exprs).from_(obj.table)
    statement, parameters = _apply_filters(
        statement,
        obj=obj,
        filters=filters,
        allowed_fields=tuple(sorted(allowed_filters)),
    )
    if dimensions:
        statement = statement.group_by(*(exp.column(obj.property(name).column) for name in dimensions))
    allowed_order_fields = set(dimensions) | set(unique_metric_ids)
    for order in order_by:
        if order.field not in allowed_order_fields:
            raise OntologyError(f"Metric order field {order.field!r} is not allowed")
        statement = statement.order_by(
            exp.Ordered(
                this=exp.Column(this=exp.Identifier(this=order.field, quoted=True)),
                desc=order.direction == "desc",
            ),
            append=True,
        )
    scoped = _guard(
        statement.sql(dialect="mysql"),
        authorization=authorization,
        allowed_databases=allowed_databases,
        policy_registry=policy_registry,
        limit=limit,
    )
    return CompiledSemanticQuery(
        scoped_query=ScopedQuery(
            sql=scoped.sql,
            parameters={**parameters, **scoped.parameters},
            referenced_tables=scoped.referenced_tables,
            policy_version=scoped.policy_version,
            scope_hash=scoped.scope_hash,
            referenced_fields=scoped.referenced_fields,
            scope_predicates_applied=scoped.scope_predicates_applied,
        ),
        columns=tuple(dimensions) + unique_metric_ids,
        ontology_version=registry.version,
        semantic_kind="metric",
        semantic_id=unique_metric_ids[0] if len(unique_metric_ids) == 1 else "metrics",
        semantic_version=metrics[0].version if len(metrics) == 1 else registry.version,
        semantic_ids=unique_metric_ids,
        semantic_versions={metric.name: metric.version for metric in metrics},
    )


def compile_metric_query(
    *,
    registry: OntologyRegistry,
    metric_id: str,
    authorization: AuthorizationContext,
    allowed_databases: tuple[str, ...],
    dimensions: Sequence[str] = (),
    filters: Sequence[SemanticFilter] = (),
    order_by: Sequence[SemanticOrder] = (),
    limit: int = 100,
    policy_registry: SqlScopePolicyRegistry | None = None,
) -> CompiledSemanticQuery:
    return compile_metrics_query(
        registry=registry,
        metric_ids=[metric_id],
        authorization=authorization,
        allowed_databases=allowed_databases,
        dimensions=dimensions,
        filters=filters,
        order_by=order_by,
        limit=limit,
        policy_registry=policy_registry,
    )
