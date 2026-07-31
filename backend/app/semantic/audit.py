"""Structured Semantic Platform audit storage without query data or secrets."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.semantic.models import SemanticAuditRow
from app.semantic.request_context import SemanticRequestContext
from deerflow.runtime.authorization_context import AuthorizationContext

_FORBIDDEN_DETAIL_FRAGMENTS = (
    "authorization",
    "connection",
    "jdbc",
    "password",
    "result",
    "rows",
    "sql",
    "token",
    "url",
)
_MAX_DETAIL_DEPTH = 4


def _safe_value(value: Any, *, depth: int) -> Any:
    if depth >= _MAX_DETAIL_DEPTH:
        return "[truncated]"
    if isinstance(value, Mapping):
        return _safe_details(value, depth=depth + 1)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _safe_details(
    value: Mapping[str, Any],
    *,
    depth: int = 0,
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:100]:
        key = str(raw_key)[:128]
        lowered = key.lower()
        if any(fragment in lowered for fragment in _FORBIDDEN_DETAIL_FRAGMENTS):
            continue
        safe[key] = _safe_value(raw_value, depth=depth)
    return safe


class SemanticAuditRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def record(
        self,
        *,
        request_context: SemanticRequestContext,
        authorization: AuthorizationContext,
        event_type: str,
        decision: str,
        details: Mapping[str, Any],
    ) -> None:
        row = SemanticAuditRow(
            id=str(uuid.uuid4()),
            semantic_trace_id=request_context.semantic_trace_id,
            run_id=request_context.run_id,
            thread_id=request_context.thread_id,
            tool_call_id=request_context.tool_call_id,
            principal_id=authorization.principal_id,
            tenant_id=authorization.tenant_id,
            system_code=authorization.system_code,
            scope_hash=authorization.scope_hash,
            permission_version=authorization.permission_version,
            event_type=str(event_type)[:128],
            decision=str(decision)[:32],
            details=_safe_details(details),
            created_at=datetime.now(UTC),
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()

    async def list_for_trace(
        self,
        semantic_trace_id: str,
        *,
        authorization: AuthorizationContext,
    ) -> list[dict[str, Any]]:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(SemanticAuditRow)
                    .where(
                        SemanticAuditRow.semantic_trace_id == semantic_trace_id,
                        SemanticAuditRow.principal_id == authorization.principal_id,
                        SemanticAuditRow.tenant_id == authorization.tenant_id,
                        SemanticAuditRow.system_code == authorization.system_code,
                        SemanticAuditRow.scope_hash == authorization.scope_hash,
                        SemanticAuditRow.permission_version == authorization.permission_version,
                    )
                    .order_by(SemanticAuditRow.created_at.asc())
                )
            ).scalars()
            return [
                {
                    "id": row.id,
                    "semantic_trace_id": row.semantic_trace_id,
                    "run_id": row.run_id,
                    "thread_id": row.thread_id,
                    "tool_call_id": row.tool_call_id,
                    "event_type": row.event_type,
                    "decision": row.decision,
                    "details": row.details,
                    "scope_hash": row.scope_hash,
                    "permission_version": row.permission_version,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
