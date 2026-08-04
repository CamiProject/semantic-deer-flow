"""Independent FastAPI service for ontology, semantic queries and Actions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.auth.saas_authorization import SaasAuthorizationError
from app.semantic.actions import ActionError, ActionRepository
from app.semantic.approval import verify_action_approval
from app.semantic.audit import SemanticAuditRepository
from app.semantic.auth import require_semantic_authorization
from app.semantic.config import get_semantic_settings
from app.semantic.database import (
    create_semantic_engine,
    create_semantic_session_factory,
    initialize_semantic_database,
)
from app.semantic.request_context import (
    SemanticRequestContext,
    require_semantic_request_context,
)
from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.semantic.demo import create_public_demo_runtime, public_demo_enabled
from deerflow.semantic.faiss_recall import OntologyFaissRecaller, OntologyRecallResult
from deerflow.semantic.ontology import OntologyError, get_ontology_registry
from deerflow.semantic.query import SemanticFilter, SemanticOrder
from deerflow.semantic.runtime import SemanticQueryRuntime
from deerflow.semantic.sql_scope import SqlScopeError, get_sql_scope_policy_registry


class FilterRequest(BaseModel):
    field: str = Field(min_length=1, max_length=128)
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in"]
    value: Any


class ResolveRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    include_facts: bool = True
    fact_limit: int = Field(default=5, ge=1, le=20)


class ObjectSearchRequest(BaseModel):
    object_type: str = Field(min_length=1, max_length=128)
    filters: list[FilterRequest] = Field(default_factory=list, max_length=20)
    properties: list[str] | None = Field(default=None, max_length=50)
    limit: int = Field(default=100, ge=1, le=1000)


class OrderRequest(BaseModel):
    field: str = Field(min_length=1, max_length=128)
    direction: Literal["asc", "desc"] = "asc"


class MetricQueryRequest(BaseModel):
    metrics: list[str] = Field(min_length=1, max_length=10)
    dimensions: list[str] = Field(default_factory=list, max_length=20)
    filters: list[FilterRequest] = Field(default_factory=list, max_length=20)
    order_by: list[OrderRequest] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_metrics(self) -> MetricQueryRequest:
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError("metrics must not contain duplicates")
        return self


class ActionProposalRequest(BaseModel):
    action_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=2000)
    expected_object_version: str | None = Field(default=None, max_length=128)


class ActionApprovalRequest(BaseModel):
    approval_token: str = Field(min_length=1, max_length=8192)


def _filters(values: list[FilterRequest]) -> list[SemanticFilter]:
    return [SemanticFilter(field=value.field, operator=value.op, value=value.value) for value in values]


def _orders(values: list[OrderRequest]) -> list[SemanticOrder]:
    return [SemanticOrder(field=value.field, direction=value.direction) for value in values]


def _runtime(request: Request) -> SemanticQueryRuntime:
    return request.app.state.semantic_runtime


def _actions(request: Request) -> ActionRepository:
    return request.app.state.action_repository


async def _record_audit(
    request: Request,
    *,
    request_context: SemanticRequestContext,
    authorization: AuthorizationContext,
    event_type: str,
    details: dict[str, Any],
    decision: str = "allow",
) -> None:
    repository: SemanticAuditRepository = request.app.state.semantic_audit_repository
    await repository.record(
        request_context=request_context,
        authorization=authorization,
        event_type=event_type,
        decision=decision,
        details=details,
    )


def _traced(payload: dict[str, Any], context: SemanticRequestContext) -> dict[str, Any]:
    return {**payload, "semantic_trace_id": context.semantic_trace_id}


def _has_semantic_matches(context: dict[str, Any]) -> bool:
    return any(context.get(kind) for kind in ("objects", "metrics", "actions"))


def _resolve_with_recall(
    *,
    runtime: SemanticQueryRuntime,
    recaller: OntologyFaissRecaller,
    authorization: AuthorizationContext,
    question: str,
    include_facts: bool,
    fact_limit: int,
) -> tuple[dict[str, Any], dict[str, Any], OntologyRecallResult]:
    exact = runtime.ontology.resolve(question)
    recall = recaller.recall(question)
    result = runtime.resolve_business_context(
        authorization=authorization,
        question=question,
        include_facts=include_facts,
        fact_limit=fact_limit,
        candidate_ids=recall.candidate_ids,
    )
    unfiltered = runtime.ontology.resolve(
        question,
        candidate_ids=recall.candidate_ids,
    )
    sources = []
    if _has_semantic_matches(exact):
        sources.append("string")
    if recall.source == "faiss":
        sources.append("faiss")
    result = {
        **result,
        "resolution": {
            "sources": sources,
            "semantic_recall": recall.audit_metadata(),
        },
    }
    return result, unfiltered, recall


def _handle_domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ActionError):
        return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.detail})
    if isinstance(exc, OntologyError):
        message = str(exc)
        if message.startswith("Unknown "):
            return HTTPException(
                status_code=404,
                detail={"code": "ONTOLOGY_NOT_FOUND", "message": message},
            )
        if "not authorized" in message:
            return HTTPException(
                status_code=403,
                detail={"code": "AUTHORIZATION_DENIED", "message": message},
            )
        if "budget" in message.lower():
            return HTTPException(
                status_code=400,
                detail={"code": "QUERY_BUDGET_EXCEEDED", "message": message},
            )
        return HTTPException(
            status_code=400,
            detail={"code": "INVALID_SEMANTIC_QUERY", "message": message},
        )
    if isinstance(exc, SqlScopeError):
        return HTTPException(
            status_code=503,
            detail={
                "code": "POLICY_UNAVAILABLE",
                "message": "Semantic query policy could not authorize the compiled plan",
            },
        )
    return HTTPException(
        status_code=500,
        detail={"code": "EXECUTION_FAILED", "message": "Semantic execution failed"},
    )


def create_app(*, settings=None, ontology=None, sql_policy=None) -> FastAPI:
    resolved_settings = settings or get_semantic_settings()
    resolved_ontology = ontology or get_ontology_registry()
    resolved_sql_policy = sql_policy or get_sql_scope_policy_registry()
    resolved_recaller = OntologyFaissRecaller(
        resolved_ontology,
        resolved_settings.semantic_recall,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_semantic_engine(resolved_settings.database_url)
        await initialize_semantic_database(engine)
        session_factory = create_semantic_session_factory(engine)
        app.state.engine = engine
        app.state.settings = resolved_settings
        app.state.semantic_runtime = (
            create_public_demo_runtime(
                ontology=resolved_ontology,
                sql_policy=resolved_sql_policy,
            )
            if public_demo_enabled()
            else SemanticQueryRuntime(
                ontology=resolved_ontology,
                sql_policy=resolved_sql_policy,
            )
        )
        app.state.semantic_recaller = resolved_recaller
        app.state.action_repository = ActionRepository(session_factory, resolved_ontology)
        app.state.semantic_audit_repository = SemanticAuditRepository(session_factory)
        yield
        await engine.dispose()

    app = FastAPI(title="Semantic DeerFlow Platform", version="1", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def semantic_validation_error(
        _request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "code": "INVALID_SEMANTIC_QUERY",
                    "message": "Semantic request validation failed",
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/audit/traces/{trace_id}")
    async def get_semantic_trace_evidence(
        trace_id: str,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        _request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        if not resolved_settings.evals_evidence_enabled:
            raise HTTPException(status_code=404, detail="Not found")
        events = await request.app.state.semantic_audit_repository.list_for_trace(
            trace_id,
            authorization=authorization,
        )
        if not events:
            raise HTTPException(
                status_code=404,
                detail={"code": "AUDIT_NOT_FOUND", "message": "Semantic audit trace not found"},
            )
        return {"semantic_trace_id": trace_id, "events": events}

    @app.post("/v1/ontology/resolve")
    async def resolve_ontology(
        body: ResolveRequest,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        try:
            result, unfiltered, recall = await asyncio.to_thread(
                _resolve_with_recall,
                runtime=_runtime(request),
                recaller=request.app.state.semantic_recaller,
                authorization=authorization,
                question=body.question,
                include_facts=body.include_facts,
                fact_limit=body.fact_limit,
            )
            authorized_action_ids = {str(item.get("id")) for item in result.get("actions", []) if isinstance(item, dict) and item.get("id")}
            action_authorization = None
            if unfiltered.get("actions") and not authorized_action_ids:
                action_authorization = {
                    "status": "denied",
                    "code": "AUTHORIZATION_DENIED",
                }
            await _record_audit(
                request,
                request_context=request_context,
                authorization=authorization,
                event_type="ontology.resolve",
                details={
                    "ontology_version": result.get("ontology_version"),
                    "policy_version": result.get("policy_version"),
                    "object_ids": [item.get("id") for item in result.get("objects", [])],
                    "metric_ids": [item.get("id") for item in result.get("metrics", [])],
                    "action_ids": [item.get("id") for item in result.get("actions", [])],
                    "fact_group_count": len(result.get("facts", [])),
                    "source_refs": result.get("source_refs", []),
                    "action_decision": action_authorization,
                    "resolution_sources": result["resolution"]["sources"],
                    "semantic_recall": recall.audit_metadata(),
                },
                decision="deny" if action_authorization else "allow",
            )
            if action_authorization:
                result = {**result, "action_authorization": action_authorization}
            return _traced(result, request_context)
        except Exception as exc:
            raise _handle_domain_error(exc) from exc

    @app.post("/v1/objects/search")
    async def search_objects(
        body: ObjectSearchRequest,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                _runtime(request).search_objects,
                authorization=authorization,
                object_type=body.object_type,
                filters=_filters(body.filters),
                properties=body.properties,
                limit=body.limit,
            )
            await _record_audit(
                request,
                request_context=request_context,
                authorization=authorization,
                event_type="object.search",
                details={
                    "object_type": body.object_type,
                    "property_names": body.properties or [],
                    "filter_fields": [item.field for item in body.filters],
                    "row_count": len(result.rows),
                    "ontology_version": result.ontology_version,
                    "policy_version": result.policy_version,
                    "source_refs": result.source_refs,
                    "query_hash": result.normalized_query_hash,
                    "referenced_fields": result.referenced_fields,
                    "scope_predicates_applied": result.scope_predicates_applied,
                    "truncated": result.truncated,
                },
            )
            return _traced(result.to_dict(), request_context)
        except Exception as exc:
            raise _handle_domain_error(exc) from exc

    @app.get("/v1/objects/{object_type}/{object_id}")
    async def get_object(
        object_type: str,
        object_id: str,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                _runtime(request).get_object,
                authorization=authorization,
                object_type=object_type,
                object_id=object_id,
            )
            await _record_audit(
                request,
                request_context=request_context,
                authorization=authorization,
                event_type="object.get",
                details={
                    "object_type": object_type,
                    "object_id": object_id,
                    "found": bool(result.rows),
                    "ontology_version": result.ontology_version,
                    "policy_version": result.policy_version,
                    "source_refs": result.source_refs,
                },
            )
            return _traced(result.to_dict(), request_context)
        except Exception as exc:
            raise _handle_domain_error(exc) from exc

    @app.post("/v1/queries")
    async def query_metric(
        body: MetricQueryRequest,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                _runtime(request).query_metrics,
                authorization=authorization,
                metric_ids=body.metrics,
                dimensions=body.dimensions,
                filters=_filters(body.filters),
                order_by=_orders(body.order_by),
                limit=body.limit,
            )
            await _record_audit(
                request,
                request_context=request_context,
                authorization=authorization,
                event_type="metric.query",
                details={
                    "metric_ids": body.metrics,
                    "dimensions": body.dimensions,
                    "filter_fields": [item.field for item in body.filters],
                    "order_fields": [item.field for item in body.order_by],
                    "row_count": len(result.rows),
                    "semantic_versions": result.semantic_versions,
                    "ontology_version": result.ontology_version,
                    "policy_version": result.policy_version,
                    "source_refs": result.source_refs,
                    "query_hash": result.normalized_query_hash,
                    "referenced_fields": result.referenced_fields,
                    "scope_predicates_applied": result.scope_predicates_applied,
                    "truncated": result.truncated,
                },
            )
            return _traced(result.to_dict(), request_context)
        except Exception as exc:
            raise _handle_domain_error(exc) from exc

    @app.get("/v1/metrics/{metric_id}")
    async def explain_metric(
        metric_id: str,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        try:
            metric = resolved_ontology.authorize_metric(metric_id, authorization)
            payload = {
                "id": metric.name,
                "label": metric.label,
                "version": metric.version,
                "object_type": metric.object_type,
                "aggregation": metric.aggregation,
                "field": metric.field,
                "dimensions": list(metric.dimensions),
                "filters": list(metric.filters),
                "unit": metric.unit,
                "grain": metric.grain,
                "time_semantics": metric.time_semantics,
                "source_refs": list(metric.source_refs),
                "ontology_version": resolved_ontology.version,
            }
            await _record_audit(
                request,
                request_context=request_context,
                authorization=authorization,
                event_type="metric.explain",
                details={
                    "metric_id": metric.name,
                    "metric_version": metric.version,
                    "ontology_version": resolved_ontology.version,
                },
            )
            return _traced(payload, request_context)
        except Exception as exc:
            raise _handle_domain_error(exc) from exc

    @app.get("/v1/actions")
    async def list_actions(
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        actions = []
        for action in resolved_ontology.available_actions(authorization):
            actions.append(
                {
                    "id": action.name,
                    "label": action.label,
                    "version": action.version,
                    "target_type": action.target_type,
                    "scope_dimension": action.scope_dimension,
                    "parameters": {
                        name: {
                            "type": parameter.value_type,
                            "required": parameter.required,
                            "minimum": parameter.minimum,
                            "maximum": parameter.maximum,
                            "min_length": parameter.min_length,
                            "max_length": parameter.max_length,
                        }
                        for name, parameter in action.parameters.items()
                    },
                    "approval_required": action.approval_required,
                }
            )
        await _record_audit(
            request,
            request_context=request_context,
            authorization=authorization,
            event_type="action.list",
            details={
                "action_ids": [item["id"] for item in actions],
                "ontology_version": resolved_ontology.version,
                "policy_version": resolved_ontology.policy_version,
            },
        )
        return _traced(
            {
                "actions": actions,
                "ontology_version": resolved_ontology.version,
                "policy_version": resolved_ontology.policy_version,
                "authorization_scope_hash": authorization.scope_hash,
            },
            request_context,
        )

    @app.post("/v1/actions/proposals")
    async def propose_action(
        body: ActionProposalRequest,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ) -> dict[str, Any]:
        try:
            result = await _actions(request).propose(
                authorization=authorization,
                action_id=body.action_id,
                target_id=body.target_id,
                parameters=body.parameters,
                reason=body.reason,
                idempotency_key=idempotency_key,
                expected_object_version=body.expected_object_version,
                request_context=request_context.to_dict(),
            )
            await _record_audit(
                request,
                request_context=request_context,
                authorization=authorization,
                event_type="action.propose",
                details={
                    "proposal_id": result["proposal_id"],
                    "action_id": result["action_id"],
                    "action_version": result["action_version"],
                    "target_type": result["target_type"],
                    "target_id": result["target_id"],
                    "status": result["status"],
                },
            )
            return _traced(result, request_context)
        except Exception as exc:
            raise _handle_domain_error(exc) from exc

    @app.post("/v1/actions/proposals/{proposal_id}/preview")
    async def preview_action(
        proposal_id: str,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        proposal = await _actions(request).get_proposal(proposal_id, authorization=authorization)
        if proposal is None:
            raise HTTPException(status_code=404, detail={"code": "ACTION_NOT_FOUND", "message": "Action proposal not found"})
        try:
            target = await asyncio.to_thread(
                _runtime(request).get_object,
                authorization=authorization,
                object_type=proposal["target_type"],
                object_id=proposal["target_id"],
            )
            if not target.rows:
                raise ActionError("AUTHORIZATION_DENIED", "Action target is not visible", status_code=403)
            result = await _actions(request).preview(
                proposal_id,
                authorization=authorization,
                target_snapshot=target.rows[0],
            )
            await _record_audit(
                request,
                request_context=request_context,
                authorization=authorization,
                event_type="action.preview",
                details={
                    "proposal_id": result["proposal_id"],
                    "action_id": result["action_id"],
                    "action_version": result["action_version"],
                    "target_type": result["target_type"],
                    "target_id": result["target_id"],
                    "status": result["status"],
                    "approval_required": result["approval_required"],
                },
            )
            return _traced(result, request_context)
        except Exception as exc:
            raise _handle_domain_error(exc) from exc

    @app.post("/v1/actions/proposals/{proposal_id}/approve")
    async def approve_action(
        proposal_id: str,
        body: ActionApprovalRequest,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        proposal = await _actions(request).get_proposal(proposal_id, authorization=authorization)
        if proposal is None:
            raise HTTPException(status_code=404, detail={"code": "ACTION_NOT_FOUND", "message": "Action proposal not found"})
        try:
            approved_by = verify_action_approval(
                body.approval_token,
                proposal_id=proposal_id,
                principal_id=authorization.principal_id,
                scope_hash=authorization.scope_hash,
            )
            result = await _actions(request).approve(
                proposal_id,
                authorization=authorization,
                approved_by=approved_by,
            )
            await _record_audit(
                request,
                request_context=request_context,
                authorization=authorization,
                event_type="action.approve",
                details={
                    "proposal_id": result["proposal_id"],
                    "action_id": result["action_id"],
                    "action_version": result["action_version"],
                    "target_id": result["target_id"],
                    "status": result["status"],
                    "approved_by": result["approved_by"],
                },
            )
            return _traced(result, request_context)
        except SaasAuthorizationError as exc:
            raise HTTPException(status_code=403, detail={"code": "AUTHORIZATION_DENIED", "message": str(exc)}) from exc
        except Exception as exc:
            raise _handle_domain_error(exc) from exc

    @app.post("/v1/actions/proposals/{proposal_id}/execute")
    async def execute_action(
        proposal_id: str,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        try:
            result = await _actions(request).enqueue_execution(
                proposal_id,
                authorization=authorization,
            )
            await _record_audit(
                request,
                request_context=request_context,
                authorization=authorization,
                event_type="action.enqueue",
                details={
                    "proposal_id": result["proposal_id"],
                    "execution_id": result["execution_id"],
                    "status": result["status"],
                },
            )
            return _traced(result, request_context)
        except Exception as exc:
            raise _handle_domain_error(exc) from exc

    @app.get("/v1/actions/proposals/{proposal_id}/evidence")
    async def get_action_evidence(
        proposal_id: str,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        _request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        if not resolved_settings.evals_evidence_enabled:
            raise HTTPException(status_code=404, detail="Not found")
        evidence = await _actions(request).get_proposal_evidence(
            proposal_id,
            authorization=authorization,
        )
        if evidence is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "ACTION_NOT_FOUND", "message": "Action proposal not found"},
            )
        return evidence

    @app.get("/v1/actions/executions/{execution_id}")
    async def get_action_status(
        execution_id: str,
        request: Request,
        authorization: AuthorizationContext = Depends(require_semantic_authorization),
        request_context: SemanticRequestContext = Depends(require_semantic_request_context),
    ) -> dict[str, Any]:
        execution = await _actions(request).get_execution(
            execution_id,
            authorization=authorization,
        )
        if execution is None:
            raise HTTPException(status_code=404, detail={"code": "ACTION_NOT_FOUND", "message": "Action execution not found"})
        await _record_audit(
            request,
            request_context=request_context,
            authorization=authorization,
            event_type="action.status",
            details={
                "proposal_id": execution["proposal_id"],
                "execution_id": execution["execution_id"],
                "status": execution["status"],
                "error_code": execution["error_code"],
            },
        )
        return _traced(execution, request_context)

    return app
