"""Stable Semantic Query and Action tools for SaaS-only agent profiles."""

from __future__ import annotations

from typing import Any

from langchain.tools import tool

from deerflow.semantic.client import SemanticPlatformClient
from deerflow.tools.types import Runtime


def _client(runtime: Runtime) -> SemanticPlatformClient:
    return SemanticPlatformClient.from_runtime(runtime)


@tool("resolve_business_context", parse_docstring=True)
async def resolve_business_context(
    question: str,
    include_facts: bool = True,
    fact_limit: int = 5,
    runtime: Runtime = None,
) -> dict[str, Any]:
    """Resolve a question to authorized ontology objects, metrics, actions and facts.

    Args:
        question: The user's business question in natural language.
        include_facts: Whether to include a small authorized fact sample.
        fact_limit: Maximum authorized facts per matched object.

    Returns:
        Structured ontology candidates, lineage and authorization scope hash.
    """
    return await _client(runtime).resolve_business_context(
        question=question,
        include_facts=include_facts,
        fact_limit=fact_limit,
    )


@tool("search_objects", parse_docstring=True)
async def search_objects(
    object_type: str,
    filters: list[dict[str, Any]] | None = None,
    properties: list[str] | None = None,
    limit: int = 100,
    runtime: Runtime = None,
) -> dict[str, Any]:
    """Search an authorized business object using ontology property names.

    Args:
        object_type: Controlled ontology object identifier.
        filters: Structured property filters with field, op and value.
        properties: Optional ontology properties to return.
        limit: Maximum rows to return.

    Returns:
        Authorized typed object rows with lineage.
    """
    return await _client(runtime).search_objects(
        object_type=object_type,
        filters=list(filters or []),
        properties=properties,
        limit=limit,
    )


@tool("get_object", parse_docstring=True)
async def get_object(
    object_type: str,
    object_id: str,
    runtime: Runtime = None,
) -> dict[str, Any]:
    """Get one authorized ontology object by its business identifier.

    Args:
        object_type: Controlled ontology object identifier.
        object_id: Business object identifier within the authorized scope.

    Returns:
        The authorized typed object row and lineage.
    """
    return await _client(runtime).get_object(object_type=object_type, object_id=object_id)


@tool("query_metrics", parse_docstring=True)
async def query_metrics(
    metrics: list[str],
    dimensions: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    order_by: list[dict[str, Any]] | None = None,
    limit: int = 100,
    runtime: Runtime = None,
) -> dict[str, Any]:
    """Query controlled business metrics without providing SQL or database names.

    Args:
        metrics: One or more controlled ontology metric identifiers.
        dimensions: Allowed business dimensions for the selected metrics.
        filters: Structured business filters with field, op and value.
        order_by: Structured ordering with field and asc or desc direction.
        limit: Maximum grouped rows to return.

    Returns:
        Typed metric rows with metric versions, lineage and scope hash.
    """
    return await _client(runtime).query_metrics(
        metrics=metrics,
        dimensions=list(dimensions or []),
        filters=list(filters or []),
        order_by=list(order_by or []),
        limit=limit,
    )


@tool("explain_metric", parse_docstring=True)
async def explain_metric(metric_id: str, runtime: Runtime = None) -> dict[str, Any]:
    """Explain one authorized metric's business definition and allowed dimensions.

    Args:
        metric_id: Controlled ontology metric identifier.

    Returns:
        Metric definition, version, unit, filters and dimensions.
    """
    return await _client(runtime).explain_metric(metric_id=metric_id)


@tool("list_available_actions", parse_docstring=True)
async def list_available_actions(runtime: Runtime = None) -> dict[str, Any]:
    """List Actions available to the current authorized principal.

    Returns:
        Authorized Action definitions without executor credentials or SQL.
    """
    return await _client(runtime).list_available_actions()


@tool("propose_action", parse_docstring=True)
async def propose_action(
    action_id: str,
    target_id: str,
    parameters: dict[str, Any],
    reason: str | None = None,
    expected_object_version: str | None = None,
    runtime: Runtime = None,
) -> dict[str, Any]:
    """Persist and preview an idempotent Action proposal; this does not execute it.

    Args:
        action_id: Controlled Action identifier from list_available_actions.
        target_id: Authorized target object identifier.
        parameters: Parameters declared by the Action definition.
        reason: Optional business reason for the proposal.
        expected_object_version: Optional optimistic concurrency version.

    Returns:
        Preview result with the persisted proposal identifier and approval status.
    """
    client = _client(runtime)
    proposal = await client.propose_action(
        action_id=action_id,
        target_id=target_id,
        parameters=parameters,
        reason=reason,
        expected_object_version=expected_object_version,
    )
    return await client.preview_action(proposal_id=proposal["proposal_id"])


@tool("preview_action", parse_docstring=True)
async def preview_action(proposal_id: str, runtime: Runtime = None) -> dict[str, Any]:
    """Validate and preview a persisted Action proposal against current object state.

    Args:
        proposal_id: Identifier returned by propose_action.

    Returns:
        Preview, current target snapshot and approval requirement.
    """
    return await _client(runtime).preview_action(proposal_id=proposal_id)


@tool("execute_action", parse_docstring=True)
async def execute_action(proposal_id: str, runtime: Runtime = None) -> dict[str, Any]:
    """Queue a validated persisted Action proposal for isolated execution.

    Args:
        proposal_id: Ready proposal identifier. Approval must happen out of band.

    Returns:
        Durable execution identifier and status.
    """
    return await _client(runtime).execute_action(proposal_id=proposal_id)


@tool("get_action_status", parse_docstring=True)
async def get_action_status(execution_id: str, runtime: Runtime = None) -> dict[str, Any]:
    """Get the durable status and result of an Action execution.

    Args:
        execution_id: Identifier returned by execute_action.

    Returns:
        Current execution status, result or sanitized failure.
    """
    return await _client(runtime).get_action_status(execution_id=execution_id)


SEMANTIC_READ_TOOLS = [
    resolve_business_context,
    search_objects,
    get_object,
    query_metrics,
    explain_metric,
]

SEMANTIC_ACTION_TOOLS = [
    list_available_actions,
    propose_action,
    preview_action,
    execute_action,
    get_action_status,
]

SEMANTIC_VALIDATION_TOOLS = [explain_metric]

SEMANTIC_TOOLS = [*SEMANTIC_READ_TOOLS, *SEMANTIC_ACTION_TOOLS]
