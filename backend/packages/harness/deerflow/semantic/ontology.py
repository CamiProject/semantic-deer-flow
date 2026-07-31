"""Versioned ontology, metric and action registry."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from deerflow.runtime.authorization_context import AuthorizationContext

ONTOLOGY_PATH_ENV_VAR = "DEER_FLOW_ONTOLOGY_PATH"
_SAFE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_AGGREGATIONS = frozenset({"count", "sum", "avg", "min", "max"})
_CARDINALITIES = frozenset({"one_to_one", "one_to_many", "many_to_one", "many_to_many"})


class OntologyError(ValueError):
    """Raised when ontology metadata or a semantic request is invalid."""


def _name(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_NAME_RE.fullmatch(text):
        raise OntologyError(f"Invalid {field}: {text!r}")
    return text


def _keywords(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise OntologyError("keywords must be a list")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise OntologyError(f"{field} must be a list")
    return tuple(_name(item, field) for item in value)


@dataclass(frozen=True)
class PropertyDefinition:
    name: str
    column: str
    value_type: str
    label: str
    unit: str | None
    sensitivity: str
    filterable: bool
    aggregatable: bool
    allowed_roles: tuple[str, ...]


@dataclass(frozen=True)
class ObjectDefinition:
    name: str
    table: str
    id_field: str
    label: str
    keywords: tuple[str, ...]
    properties: Mapping[str, PropertyDefinition]
    version: str
    allowed_roles: tuple[str, ...]

    def property(self, name: str) -> PropertyDefinition:
        prop = self.properties.get(name)
        if prop is None:
            raise OntologyError(f"Unknown property {self.name}.{name}")
        return prop


@dataclass(frozen=True)
class LinkDefinition:
    name: str
    from_object: str
    to_object: str
    from_field: str
    to_field: str
    cardinality: str
    version: str


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    label: str
    object_type: str
    aggregation: str
    field: str | None
    dimensions: tuple[str, ...]
    filters: tuple[str, ...]
    unit: str | None
    keywords: tuple[str, ...]
    version: str
    grain: str | None
    time_semantics: str | None
    source_refs: tuple[str, ...]
    allowed_roles: tuple[str, ...]


@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    value_type: str
    required: bool
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None

    def validate(self, value: Any) -> Any:
        if value is None:
            if self.required:
                raise OntologyError(f"Action parameter {self.name!r} is required")
            return None
        if self.value_type == "string":
            if not isinstance(value, str):
                raise OntologyError(f"Action parameter {self.name!r} must be a string")
            if self.min_length is not None and len(value) < self.min_length:
                raise OntologyError(f"Action parameter {self.name!r} is too short")
            if self.max_length is not None and len(value) > self.max_length:
                raise OntologyError(f"Action parameter {self.name!r} is too long")
        elif self.value_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OntologyError(f"Action parameter {self.name!r} must be a number")
            if self.minimum is not None and value < self.minimum:
                raise OntologyError(f"Action parameter {self.name!r} is below minimum")
            if self.maximum is not None and value > self.maximum:
                raise OntologyError(f"Action parameter {self.name!r} is above maximum")
        elif self.value_type == "boolean":
            if not isinstance(value, bool):
                raise OntologyError(f"Action parameter {self.name!r} must be a boolean")
        else:
            raise OntologyError(f"Unsupported action parameter type {self.value_type!r}")
        return value


@dataclass(frozen=True)
class ActionDefinition:
    name: str
    version: str
    label: str
    keywords: tuple[str, ...]
    target_type: str
    scope_dimension: str
    parameters: Mapping[str, ParameterDefinition]
    preconditions: tuple[Mapping[str, Any], ...]
    approval_required: bool
    executor: Mapping[str, Any]
    compensation: Mapping[str, Any] | None
    allowed_roles: tuple[str, ...]

    def validate_parameters(self, values: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(values) - set(self.parameters)
        if unknown:
            raise OntologyError(f"Unknown action parameters: {', '.join(sorted(unknown))}")
        return {name: definition.validate(values.get(name)) for name, definition in self.parameters.items() if values.get(name) is not None or definition.required}


@dataclass(frozen=True)
class OntologyRegistry:
    version: str
    policy_version: str
    mapping_version: str
    objects: Mapping[str, ObjectDefinition]
    links: Mapping[str, LinkDefinition]
    metrics: Mapping[str, MetricDefinition]
    actions: Mapping[str, ActionDefinition]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> OntologyRegistry:
        version = str(value.get("version") or "").strip()
        if not version:
            raise OntologyError("Ontology version is required")

        objects: dict[str, ObjectDefinition] = {}
        for raw_name, raw in (value.get("objects") or {}).items():
            if not isinstance(raw, Mapping):
                raise OntologyError(f"Object {raw_name!r} must be a mapping")
            name = _name(raw_name, "object name")
            properties = {
                _name(prop_name, "property name"): PropertyDefinition(
                    name=_name(prop_name, "property name"),
                    column=_name(prop.get("column"), "property column"),
                    value_type=str(prop.get("type") or "string"),
                    label=str(prop.get("label") or prop_name),
                    unit=str(prop.get("unit")) if prop.get("unit") is not None else None,
                    sensitivity=str(prop.get("sensitivity") or "internal"),
                    filterable=bool(prop.get("filterable", True)),
                    aggregatable=bool(prop.get("aggregatable", str(prop.get("type") or "string") == "number")),
                    allowed_roles=_string_tuple(prop.get("allowed_roles"), "property allowed role"),
                )
                for prop_name, prop in (raw.get("properties") or {}).items()
                if isinstance(prop, Mapping)
            }
            id_field = _name(raw.get("id_field"), "id_field")
            if id_field not in properties:
                raise OntologyError(f"Object {name!r} id_field is not a property")
            objects[name] = ObjectDefinition(
                name=name,
                table=_name(raw.get("table"), "object table"),
                id_field=id_field,
                label=str(raw.get("label") or name),
                keywords=_keywords(raw.get("keywords")),
                properties=properties,
                version=str(raw.get("version") or version),
                allowed_roles=_string_tuple(raw.get("allowed_roles"), "object allowed role"),
            )

        links = {
            _name(link_name, "link name"): LinkDefinition(
                name=_name(link_name, "link name"),
                from_object=_name(raw.get("from_object"), "from_object"),
                to_object=_name(raw.get("to_object"), "to_object"),
                from_field=_name(raw.get("from_field"), "from_field"),
                to_field=_name(raw.get("to_field"), "to_field"),
                cardinality=str(raw.get("cardinality") or "many_to_one"),
                version=str(raw.get("version") or version),
            )
            for link_name, raw in (value.get("links") or {}).items()
            if isinstance(raw, Mapping)
        }

        metrics: dict[str, MetricDefinition] = {}
        for raw_name, raw in (value.get("metrics") or {}).items():
            if not isinstance(raw, Mapping):
                raise OntologyError(f"Metric {raw_name!r} must be a mapping")
            name = _name(raw_name, "metric name")
            object_type = _name(raw.get("object_type"), "metric object_type")
            obj = objects.get(object_type)
            if obj is None:
                raise OntologyError(f"Metric {name!r} references unknown object {object_type!r}")
            aggregation = str(raw.get("aggregation") or "").lower()
            if aggregation not in _AGGREGATIONS:
                raise OntologyError(f"Metric {name!r} has unsupported aggregation")
            field = raw.get("field")
            field = _name(field, "metric field") if field is not None else None
            if aggregation != "count" and field is None:
                raise OntologyError(f"Metric {name!r} requires a field")
            if field is not None:
                metric_property = obj.property(field)
                if not metric_property.aggregatable:
                    raise OntologyError(f"Metric {name!r} field {field!r} is not aggregatable")
            dimensions = tuple(_name(item, "metric dimension") for item in (raw.get("dimensions") or []))
            filters = tuple(_name(item, "metric filter") for item in (raw.get("filters") or []))
            for prop in dimensions + filters:
                definition = obj.property(prop)
                if prop in filters and not definition.filterable:
                    raise OntologyError(f"Metric {name!r} filter {prop!r} is not filterable")
            metrics[name] = MetricDefinition(
                name=name,
                label=str(raw.get("label") or name),
                object_type=object_type,
                aggregation=aggregation,
                field=field,
                dimensions=dimensions,
                filters=filters,
                unit=str(raw.get("unit")) if raw.get("unit") is not None else None,
                keywords=_keywords(raw.get("keywords")),
                version=str(raw.get("version") or version),
                grain=str(raw.get("grain")) if raw.get("grain") is not None else None,
                time_semantics=(str(raw.get("time_semantics")) if raw.get("time_semantics") is not None else None),
                source_refs=_string_tuple(
                    raw.get("source_refs") or [obj.table],
                    "metric source reference",
                ),
                allowed_roles=_string_tuple(raw.get("allowed_roles"), "metric allowed role"),
            )

        actions: dict[str, ActionDefinition] = {}
        for raw_name, raw in (value.get("actions") or {}).items():
            if not isinstance(raw, Mapping):
                raise OntologyError(f"Action {raw_name!r} must be a mapping")
            name = _name(raw_name, "action name")
            target_type = _name(raw.get("target_type"), "action target_type")
            target_object = objects.get(target_type)
            if target_object is None:
                raise OntologyError(f"Action {name!r} references unknown target type")
            parameters = {
                _name(param_name, "parameter name"): ParameterDefinition(
                    name=_name(param_name, "parameter name"),
                    value_type=str(param.get("type") or "string"),
                    required=bool(param.get("required", False)),
                    minimum=param.get("minimum"),
                    maximum=param.get("maximum"),
                    min_length=param.get("min_length"),
                    max_length=param.get("max_length"),
                )
                for param_name, param in (raw.get("parameters") or {}).items()
                if isinstance(param, Mapping)
            }
            unsupported_parameter_types = {parameter.value_type for parameter in parameters.values() if parameter.value_type not in {"string", "number", "boolean"}}
            if unsupported_parameter_types:
                raise OntologyError(f"Action {name!r} has unsupported parameter types")
            approval = raw.get("approval") or {}
            authorization = raw.get("authorization") or {}
            executor = raw.get("executor") or {}
            compensation = raw.get("compensation")
            if not isinstance(approval, Mapping) or not isinstance(authorization, Mapping) or not isinstance(executor, Mapping) or (compensation is not None and not isinstance(compensation, Mapping)):
                raise OntologyError(f"Action {name!r} has invalid approval or executor")
            raw_preconditions = raw.get("preconditions") or []
            if not isinstance(raw_preconditions, list):
                raise OntologyError(f"Action {name!r} preconditions must be a list")
            preconditions: list[dict[str, Any]] = []
            for raw_precondition in raw_preconditions:
                if not isinstance(raw_precondition, Mapping):
                    raise OntologyError(f"Action {name!r} has invalid precondition")
                field = _name(raw_precondition.get("field"), "action precondition field")
                target_object.property(field)
                operator = str(raw_precondition.get("op") or "eq").strip().lower()
                if operator != "eq":
                    raise OntologyError(f"Action {name!r} has unsupported precondition operator")
                preconditions.append({"field": field, "op": operator, "value": raw_precondition.get("value")})
            executor_type = str(executor.get("type") or "").strip()
            executor_method = str(executor.get("method") or "POST").strip().upper()
            executor_path = str(executor.get("path") or "").strip()
            executor_result_fields = executor.get("result_fields") or []
            executor_is_invalid = (
                executor_type != "domain_api"
                or executor_method not in {"POST", "PUT", "PATCH", "DELETE"}
                or not executor_path.startswith("/")
                or executor_path.startswith("//")
                or executor_path.count("{target_id}") != 1
                or not isinstance(executor_result_fields, list)
                or len(executor_result_fields) > 50
            )
            if executor_is_invalid:
                raise OntologyError(f"Action {name!r} has invalid domain API executor")
            normalized_executor_result_fields = list(dict.fromkeys(_name(item, "action executor result field") for item in executor_result_fields))
            normalized_compensation: dict[str, Any] | None = None
            if compensation is not None:
                compensation_type = str(compensation.get("type") or "").strip()
                compensation_method = str(compensation.get("method") or "POST").strip().upper()
                compensation_path = str(compensation.get("path") or "").strip()
                compensation_result_fields = compensation.get("result_fields") or []
                compensation_is_invalid = (
                    compensation_type != "domain_api"
                    or compensation_method not in {"POST", "PUT", "PATCH", "DELETE"}
                    or not compensation_path.startswith("/")
                    or compensation_path.startswith("//")
                    or compensation_path.count("{target_id}") != 1
                    or not isinstance(compensation_result_fields, list)
                    or len(compensation_result_fields) > 50
                )
                if compensation_is_invalid:
                    raise OntologyError(f"Action {name!r} has invalid compensation executor")
                normalized_compensation = {
                    **dict(compensation),
                    "type": compensation_type,
                    "method": compensation_method,
                    "path": compensation_path,
                    "result_fields": list(dict.fromkeys(_name(item, "action compensation result field") for item in compensation_result_fields)),
                }
            actions[name] = ActionDefinition(
                name=name,
                version=str(raw.get("version") or version),
                label=str(raw.get("label") or name),
                keywords=_keywords(raw.get("keywords")),
                target_type=target_type,
                scope_dimension=str(raw.get("scope_dimension") or "").strip().lower(),
                parameters=parameters,
                preconditions=tuple(preconditions),
                approval_required=bool(approval.get("required", False)),
                executor={
                    **dict(executor),
                    "type": executor_type,
                    "method": executor_method,
                    "path": executor_path,
                    "result_fields": normalized_executor_result_fields,
                },
                compensation=normalized_compensation,
                allowed_roles=_string_tuple(
                    authorization.get("allowed_roles"),
                    "action allowed role",
                ),
            )
            if actions[name].scope_dimension not in {"site", "project"}:
                raise OntologyError(f"Action {name!r} requires site or project scope_dimension")

        for link in links.values():
            if link.cardinality not in _CARDINALITIES:
                raise OntologyError(f"Link {link.name!r} has invalid cardinality")
            from_object = objects.get(link.from_object)
            to_object = objects.get(link.to_object)
            if from_object is None or to_object is None:
                raise OntologyError(f"Link {link.name!r} references an unknown object")
            from_object.property(link.from_field)
            to_object.property(link.to_field)

        return cls(
            version=version,
            policy_version=str(value.get("policy_version") or version),
            mapping_version=str(value.get("mapping_version") or version),
            objects=objects,
            links=links,
            metrics=metrics,
            actions=actions,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> OntologyRegistry:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = yaml.safe_load(handle) or {}
        if not isinstance(value, Mapping):
            raise OntologyError("Ontology document must be a mapping")
        return cls.from_mapping(value)

    def object(self, name: str) -> ObjectDefinition:
        value = self.objects.get(name)
        if value is None:
            raise OntologyError(f"Unknown object type {name!r}")
        return value

    def metric(self, name: str) -> MetricDefinition:
        value = self.metrics.get(name)
        if value is None:
            raise OntologyError(f"Unknown metric {name!r}")
        return value

    def action(self, name: str) -> ActionDefinition:
        value = self.actions.get(name)
        if value is None:
            raise OntologyError(f"Unknown action {name!r}")
        return value

    @staticmethod
    def _role_allowed(allowed_roles: tuple[str, ...], authorization: AuthorizationContext) -> bool:
        return not allowed_roles or bool(set(allowed_roles).intersection(authorization.role_codes))

    def authorize_object(
        self,
        name: str,
        authorization: AuthorizationContext,
    ) -> ObjectDefinition:
        obj = self.object(name)
        if not self._role_allowed(obj.allowed_roles, authorization):
            raise OntologyError(f"Object type {name!r} is not authorized")
        return obj

    def authorize_property(
        self,
        object_name: str,
        property_name: str,
        authorization: AuthorizationContext,
    ) -> PropertyDefinition:
        obj = self.authorize_object(object_name, authorization)
        prop = obj.property(property_name)
        if not self._role_allowed(prop.allowed_roles, authorization):
            raise OntologyError(f"Property {object_name}.{property_name} is not authorized")
        return prop

    def authorize_metric(
        self,
        name: str,
        authorization: AuthorizationContext,
    ) -> MetricDefinition:
        metric = self.metric(name)
        self.authorize_object(metric.object_type, authorization)
        if not self._role_allowed(metric.allowed_roles, authorization):
            raise OntologyError(f"Metric {name!r} is not authorized")
        return metric

    def authorize_action(
        self,
        name: str,
        authorization: AuthorizationContext,
    ) -> ActionDefinition:
        action = self.action(name)
        self.authorize_object(action.target_type, authorization)
        if not self._role_allowed(action.allowed_roles, authorization):
            raise OntologyError(f"Action {name!r} is not authorized")
        return action

    def available_actions(self, authorization: AuthorizationContext) -> tuple[ActionDefinition, ...]:
        return tuple(action for action in self.actions.values() if self._role_allowed(action.allowed_roles, authorization) and self._role_allowed(self.object(action.target_type).allowed_roles, authorization))

    def resolve(
        self,
        question: str,
        authorization: AuthorizationContext | None = None,
    ) -> dict[str, Any]:
        normalized = question.casefold()

        def matches(name: str, label: str, keywords: tuple[str, ...]) -> bool:
            return any(value.casefold() in normalized for value in (name, label, *keywords))

        matched_objects = [obj for obj in self.objects.values() if matches(obj.name, obj.label, obj.keywords) and (authorization is None or self._role_allowed(obj.allowed_roles, authorization))]
        matched_object_names = {obj.name for obj in matched_objects}

        def property_is_visible(prop: PropertyDefinition) -> bool:
            return authorization is None or self._role_allowed(
                prop.allowed_roles,
                authorization,
            )

        def link_is_visible(link: LinkDefinition) -> bool:
            if authorization is None:
                return True
            from_object = self.object(link.from_object)
            to_object = self.object(link.to_object)
            return (
                self._role_allowed(from_object.allowed_roles, authorization)
                and self._role_allowed(to_object.allowed_roles, authorization)
                and property_is_visible(from_object.property(link.from_field))
                and property_is_visible(to_object.property(link.to_field))
            )

        return {
            "ontology_version": self.version,
            "objects": [
                {
                    "id": obj.name,
                    "label": obj.label,
                    "version": obj.version,
                    "properties": sorted(prop.name for prop in obj.properties.values() if property_is_visible(prop)),
                    "property_definitions": [
                        {
                            "id": prop.name,
                            "label": prop.label,
                            "type": prop.value_type,
                            "unit": prop.unit,
                            "sensitivity": prop.sensitivity,
                            "filterable": prop.filterable,
                            "aggregatable": prop.aggregatable,
                        }
                        for prop in obj.properties.values()
                        if property_is_visible(prop)
                    ],
                    "source_refs": [obj.table],
                }
                for obj in matched_objects
            ],
            "links": [
                {
                    "id": link.name,
                    "version": link.version,
                    "from_object": link.from_object,
                    "to_object": link.to_object,
                    "from_field": link.from_field,
                    "to_field": link.to_field,
                    "cardinality": link.cardinality,
                }
                for link in self.links.values()
                if (link.from_object in matched_object_names or link.to_object in matched_object_names) and link_is_visible(link)
            ],
            "metrics": [
                {
                    "id": metric.name,
                    "label": metric.label,
                    "version": metric.version,
                    "unit": metric.unit,
                    "grain": metric.grain,
                    "time_semantics": metric.time_semantics,
                    "dimensions": list(metric.dimensions),
                    "filters": list(metric.filters),
                    "source_refs": list(metric.source_refs),
                }
                for metric in self.metrics.values()
                if matches(metric.name, metric.label, metric.keywords)
                and (
                    authorization is None
                    or (
                        self._role_allowed(metric.allowed_roles, authorization)
                        and self._role_allowed(
                            self.object(metric.object_type).allowed_roles,
                            authorization,
                        )
                    )
                )
            ],
            "actions": [
                {
                    "id": action.name,
                    "label": action.label,
                    "version": action.version,
                    "target_type": action.target_type,
                    "approval_required": action.approval_required,
                }
                for action in self.actions.values()
                if matches(action.name, action.label, action.keywords)
                and (
                    authorization is None
                    or (
                        self._role_allowed(action.allowed_roles, authorization)
                        and self._role_allowed(
                            self.object(action.target_type).allowed_roles,
                            authorization,
                        )
                    )
                )
            ],
            "policy_version": self.policy_version,
            "mapping_version": self.mapping_version,
        }


@lru_cache(maxsize=4)
def _load_registry(path: str) -> OntologyRegistry:
    return OntologyRegistry.from_file(path)


def get_ontology_registry() -> OntologyRegistry:
    configured = os.environ.get(ONTOLOGY_PATH_ENV_VAR)
    path = Path(configured) if configured else Path(__file__).with_name("default_ontology.yaml")
    return _load_registry(str(path.resolve()))
