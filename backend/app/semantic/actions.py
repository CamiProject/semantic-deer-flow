"""Persistent Action proposal, approval and execution state machine."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.semantic.models import ActionExecutionRow, ActionProposalRow, ActionTransitionRow
from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.semantic.ontology import ActionDefinition, OntologyError, OntologyRegistry

_SAFE_TARGET_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class ActionError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def _proposal_dict(row: ActionProposalRow) -> dict[str, Any]:
    return {
        "proposal_id": row.id,
        "action_id": row.action_id,
        "action_version": row.action_version,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "parameters": row.parameters,
        "reason": row.reason,
        "status": row.status,
        "approval_required": row.approval_required,
        "approved_by": row.approved_by,
        "scope_hash": row.scope_hash,
        "permission_version": row.permission_version,
        "expected_object_version": row.expected_object_version,
        "run_id": row.run_id,
        "thread_id": row.thread_id,
        "tool_call_id": row.tool_call_id,
        "semantic_trace_id": row.semantic_trace_id,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _proposal_command(row: ActionProposalRow) -> dict[str, Any]:
    return {
        **_proposal_dict(row),
        "authorization_snapshot": row.authorization_snapshot,
        "executor": row.executor,
        "idempotency_key": row.idempotency_key,
    }


def _execution_dict(row: ActionExecutionRow) -> dict[str, Any]:
    return {
        "execution_id": row.id,
        "proposal_id": row.proposal_id,
        "status": row.status,
        "result": row.result,
        "error_code": row.error_code,
        "error_detail": row.error_detail,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _execution_with_proposal(
    execution: ActionExecutionRow,
    proposal: ActionProposalRow,
) -> dict[str, Any]:
    return {
        **_execution_dict(execution),
        "action_id": proposal.action_id,
        "action_version": proposal.action_version,
        "target_type": proposal.target_type,
        "target_id": proposal.target_id,
        "scope_hash": proposal.scope_hash,
        "permission_version": proposal.permission_version,
    }


def _target_in_scope(action: ActionDefinition, target_id: str, authorization: AuthorizationContext) -> bool:
    if authorization.scope_mode == "tenant_all":
        return True
    if authorization.scope_mode != "resource_set":
        return False
    if action.scope_dimension == "site":
        return target_id in authorization.allowed_site_ids
    if action.scope_dimension == "project":
        return target_id in authorization.allowed_project_ids
    return False


def _proposal_matches_authorization(
    proposal: ActionProposalRow,
    authorization: AuthorizationContext,
) -> bool:
    return (
        proposal.principal_id == authorization.principal_id
        and proposal.tenant_id == authorization.tenant_id
        and proposal.system_code == authorization.system_code
        and proposal.scope_hash == authorization.scope_hash
        and proposal.permission_version == authorization.permission_version
    )


def validate_action_target(
    *,
    action: ActionDefinition,
    target_id: str,
    target_id_field: str,
    expected_object_version: str | None,
    target_snapshot: Mapping[str, Any],
) -> None:
    actual_target_id = target_snapshot.get(target_id_field)
    if actual_target_id is None or str(actual_target_id) != target_id:
        raise ActionError(
            "AUTHORIZATION_DENIED",
            "Action target is not visible in the authorized scope",
            status_code=403,
        )
    if expected_object_version is not None:
        actual_version = target_snapshot.get("version") or target_snapshot.get("updated_at")
        if actual_version is None or str(actual_version) != expected_object_version:
            raise ActionError("ACTION_CONFLICT", "Target object version changed", status_code=409)
    for precondition in action.preconditions:
        field = str(precondition.get("field") or "")
        operator = str(precondition.get("op") or "eq")
        expected = precondition.get("value")
        actual = target_snapshot.get(field)
        if operator != "eq" or actual != expected:
            raise ActionError(
                "ACTION_PRECONDITION_FAILED",
                f"Action precondition failed for {field}",
                status_code=409,
            )


class ActionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], ontology: OntologyRegistry) -> None:
        self._sf = session_factory
        self._ontology = ontology

    @staticmethod
    def _record_transition(
        session: AsyncSession,
        *,
        entity_type: str,
        entity_id: str,
        from_status: str | None,
        to_status: str,
        reason: str | None = None,
    ) -> None:
        session.add(
            ActionTransitionRow(
                entity_type=entity_type,
                entity_id=entity_id,
                from_status=from_status,
                to_status=to_status,
                reason=reason,
            )
        )

    async def list_transitions(self, *, entity_type: str, entity_id: str) -> list[str]:
        if entity_type not in {"proposal", "execution"}:
            raise ValueError("Unsupported Action transition entity type")
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(ActionTransitionRow.to_status)
                    .where(
                        ActionTransitionRow.entity_type == entity_type,
                        ActionTransitionRow.entity_id == entity_id,
                    )
                    .order_by(ActionTransitionRow.id.asc())
                )
            ).scalars()
            return list(rows)

    async def propose(
        self,
        *,
        authorization: AuthorizationContext,
        action_id: str,
        target_id: str,
        parameters: Mapping[str, Any],
        reason: str | None,
        idempotency_key: str,
        expected_object_version: str | None,
        request_context: Mapping[str, str],
    ) -> dict[str, Any]:
        if not _SAFE_TARGET_ID.fullmatch(target_id):
            raise ActionError("INVALID_ACTION_PARAMETERS", "Invalid Action target identifier")
        try:
            action = self._ontology.authorize_action(action_id, authorization)
        except OntologyError as exc:
            raise ActionError("AUTHORIZATION_DENIED", str(exc), status_code=403) from exc
        if not _target_in_scope(action, target_id, authorization):
            raise ActionError("AUTHORIZATION_DENIED", "Action target is outside the authorized scope", status_code=403)
        try:
            validated = action.validate_parameters(parameters)
        except OntologyError as exc:
            raise ActionError("INVALID_ACTION_PARAMETERS", str(exc)) from exc

        now = datetime.now(UTC)
        row = ActionProposalRow(
            id=str(uuid.uuid4()),
            action_id=action.name,
            action_version=action.version,
            target_type=action.target_type,
            target_id=target_id,
            parameters=validated,
            reason=reason,
            principal_id=authorization.principal_id,
            tenant_id=authorization.tenant_id,
            system_code=authorization.system_code,
            scope_hash=authorization.scope_hash,
            permission_version=authorization.permission_version,
            authorization_snapshot=authorization.to_runtime_dict(),
            status="PROPOSED",
            approval_required=action.approval_required,
            idempotency_key=idempotency_key,
            expected_object_version=expected_object_version,
            executor=dict(action.executor),
            run_id=request_context["run_id"],
            thread_id=request_context["thread_id"],
            tool_call_id=request_context["tool_call_id"],
            semantic_trace_id=request_context["semantic_trace_id"],
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            self._record_transition(
                session,
                entity_type="proposal",
                entity_id=row.id,
                from_status=None,
                to_status="PROPOSED",
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = (
                    await session.execute(
                        select(ActionProposalRow).where(
                            ActionProposalRow.principal_id == authorization.principal_id,
                            ActionProposalRow.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one()
                if (
                    existing.action_id != action.name
                    or existing.target_id != target_id
                    or existing.parameters != validated
                    or existing.reason != reason
                    or existing.expected_object_version != expected_object_version
                    or existing.scope_hash != authorization.scope_hash
                ):
                    raise ActionError(
                        "ACTION_CONFLICT",
                        "Idempotency key was already used for a different Action proposal",
                        status_code=409,
                    )
                return _proposal_dict(existing)
            await session.refresh(row)
            return _proposal_dict(row)

    async def get_proposal(self, proposal_id: str, *, authorization: AuthorizationContext) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = await session.get(ActionProposalRow, proposal_id)
            if row is None or not _proposal_matches_authorization(row, authorization):
                return None
            return _proposal_dict(row)

    async def get_proposal_evidence(
        self,
        proposal_id: str,
        *,
        authorization: AuthorizationContext,
    ) -> dict[str, Any] | None:
        """Return read-only Action state and transitions for the owning Scope."""
        async with self._sf() as session:
            proposal = await session.get(ActionProposalRow, proposal_id)
            if proposal is None or not _proposal_matches_authorization(proposal, authorization):
                return None
            execution = (await session.execute(select(ActionExecutionRow).where(ActionExecutionRow.proposal_id == proposal_id))).scalar_one_or_none()
            proposal_transitions = list(
                (
                    await session.execute(
                        select(ActionTransitionRow.to_status)
                        .where(
                            ActionTransitionRow.entity_type == "proposal",
                            ActionTransitionRow.entity_id == proposal_id,
                        )
                        .order_by(ActionTransitionRow.id.asc())
                    )
                ).scalars()
            )
            execution_transitions: list[str] = []
            if execution is not None:
                execution_transitions = list(
                    (
                        await session.execute(
                            select(ActionTransitionRow.to_status)
                            .where(
                                ActionTransitionRow.entity_type == "execution",
                                ActionTransitionRow.entity_id == execution.id,
                            )
                            .order_by(ActionTransitionRow.id.asc())
                        )
                    ).scalars()
                )
            return {
                "proposal": _proposal_dict(proposal),
                "proposal_transitions": proposal_transitions,
                "execution": _execution_with_proposal(execution, proposal) if execution is not None else None,
                "execution_transitions": execution_transitions,
            }

    async def approve(self, proposal_id: str, *, authorization: AuthorizationContext, approved_by: str) -> dict[str, Any]:
        async with self._sf() as session:
            row = await session.get(ActionProposalRow, proposal_id)
            if row is None or row.principal_id != authorization.principal_id or row.tenant_id != authorization.tenant_id:
                raise ActionError("ACTION_NOT_FOUND", "Action proposal not found", status_code=404)
            if row.scope_hash != authorization.scope_hash or row.permission_version != authorization.permission_version:
                raise ActionError("SCOPE_CHANGED", "Authorization scope changed; create a new proposal", status_code=409)
            if row.status == "READY":
                return _proposal_dict(row)
            if row.status != "PENDING_APPROVAL":
                raise ActionError("ACTION_CONFLICT", f"Proposal cannot be approved from {row.status}", status_code=409)
            previous_status = row.status
            row.status = "READY"
            row.approved_by = approved_by
            row.approved_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            self._record_transition(
                session,
                entity_type="proposal",
                entity_id=row.id,
                from_status=previous_status,
                to_status="READY",
                reason="approved",
            )
            await session.commit()
            await session.refresh(row)
            return _proposal_dict(row)

    async def preview(
        self,
        proposal_id: str,
        *,
        authorization: AuthorizationContext,
        target_snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        async with self._sf() as session:
            row = await session.get(ActionProposalRow, proposal_id)
            if row is None or row.principal_id != authorization.principal_id or row.tenant_id != authorization.tenant_id:
                raise ActionError("ACTION_NOT_FOUND", "Action proposal not found", status_code=404)
            if row.scope_hash != authorization.scope_hash or row.permission_version != authorization.permission_version:
                raise ActionError("SCOPE_CHANGED", "Authorization scope changed; create a new proposal", status_code=409)
            if row.status not in {"PROPOSED", "PENDING_APPROVAL", "READY"}:
                raise ActionError("ACTION_CONFLICT", f"Proposal cannot be previewed from {row.status}", status_code=409)
            action = self._ontology.action(row.action_id)
            target_type = self._ontology.object(action.target_type)
            validate_action_target(
                action=action,
                target_id=row.target_id,
                target_id_field=target_type.id_field,
                expected_object_version=row.expected_object_version,
                target_snapshot=target_snapshot,
            )
            previous_status = row.status
            row.status = "VALIDATED"
            self._record_transition(
                session,
                entity_type="proposal",
                entity_id=row.id,
                from_status=previous_status,
                to_status="VALIDATED",
            )
            row.status = "PREVIEWED"
            self._record_transition(
                session,
                entity_type="proposal",
                entity_id=row.id,
                from_status="VALIDATED",
                to_status="PREVIEWED",
            )
            next_status = "PENDING_APPROVAL" if row.approval_required else "READY"
            row.status = next_status
            self._record_transition(
                session,
                entity_type="proposal",
                entity_id=row.id,
                from_status="PREVIEWED",
                to_status=next_status,
            )
            row.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            preview = _proposal_dict(row)
            preview["target_snapshot"] = dict(target_snapshot)
            return preview

    async def enqueue_execution(self, proposal_id: str, *, authorization: AuthorizationContext) -> dict[str, Any]:
        async with self._sf() as session:
            proposal = await session.get(ActionProposalRow, proposal_id)
            if proposal is None or proposal.principal_id != authorization.principal_id or proposal.tenant_id != authorization.tenant_id:
                raise ActionError("ACTION_NOT_FOUND", "Action proposal not found", status_code=404)
            if proposal.scope_hash != authorization.scope_hash or proposal.permission_version != authorization.permission_version:
                raise ActionError("SCOPE_CHANGED", "Authorization scope changed; create a new proposal", status_code=409)
            if proposal.status == "PENDING_APPROVAL":
                raise ActionError("ACTION_APPROVAL_REQUIRED", "Action approval is required", status_code=409)
            if proposal.status not in {"READY", "EXECUTING", "SUCCEEDED", "FAILED"}:
                raise ActionError("ACTION_CONFLICT", f"Proposal cannot execute from {proposal.status}", status_code=409)
            existing = (await session.execute(select(ActionExecutionRow).where(ActionExecutionRow.proposal_id == proposal_id))).scalar_one_or_none()
            if existing is not None:
                return _execution_with_proposal(existing, proposal)
            now = datetime.now(UTC)
            execution = ActionExecutionRow(
                id=str(uuid.uuid4()),
                proposal_id=proposal_id,
                status="READY",
                created_at=now,
                updated_at=now,
            )
            session.add(execution)
            self._record_transition(
                session,
                entity_type="execution",
                entity_id=execution.id,
                from_status=None,
                to_status="READY",
            )
            await session.commit()
            await session.refresh(execution)
            return _execution_with_proposal(execution, proposal)

    async def get_execution(self, execution_id: str, *, authorization: AuthorizationContext) -> dict[str, Any] | None:
        async with self._sf() as session:
            execution = await session.get(ActionExecutionRow, execution_id)
            if execution is None:
                return None
            proposal = await session.get(ActionProposalRow, execution.proposal_id)
            if proposal is None or not _proposal_matches_authorization(proposal, authorization):
                return None
            return _execution_with_proposal(execution, proposal)

    async def claim_ready(self, *, worker_id: str, lease_seconds: int) -> tuple[dict[str, Any], dict[str, Any]] | None:
        now = datetime.now(UTC)
        stmt = (
            select(ActionExecutionRow)
            .where(
                ActionExecutionRow.status.in_(["READY", "EXECUTING", "COMPENSATING"]),
                or_(
                    ActionExecutionRow.status == "READY",
                    ActionExecutionRow.lease_expires_at < now,
                ),
            )
            .order_by(ActionExecutionRow.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        async with self._sf() as session:
            execution = (await session.execute(stmt)).scalar_one_or_none()
            if execution is None:
                return None
            proposal = await session.get(ActionProposalRow, execution.proposal_id)
            if proposal is None or proposal.status not in {"READY", "EXECUTING", "COMPENSATING"}:
                return None
            execution_previous_status = execution.status
            proposal_previous_status = proposal.status
            claimed_status = "COMPENSATING" if execution_previous_status == "COMPENSATING" else "EXECUTING"
            execution.status = claimed_status
            execution.lease_owner = worker_id
            execution.lease_expires_at = now + timedelta(seconds=lease_seconds)
            execution.started_at = execution.started_at or now
            execution.updated_at = now
            proposal.status = claimed_status
            proposal.updated_at = now
            self._record_transition(
                session,
                entity_type="execution",
                entity_id=execution.id,
                from_status=execution_previous_status,
                to_status=claimed_status,
                reason=("lease_recovered" if execution_previous_status in {"EXECUTING", "COMPENSATING"} else None),
            )
            self._record_transition(
                session,
                entity_type="proposal",
                entity_id=proposal.id,
                from_status=proposal_previous_status,
                to_status=claimed_status,
                reason=("lease_recovered" if proposal_previous_status in {"EXECUTING", "COMPENSATING"} else None),
            )
            await session.commit()
            return _proposal_command(proposal), _execution_dict(execution)

    async def begin_compensation(
        self,
        *,
        execution_id: str,
        worker_id: str,
        error_code: str,
        error_detail: str,
    ) -> None:
        async with self._sf() as session:
            execution = await session.get(ActionExecutionRow, execution_id)
            if execution is None or execution.lease_owner != worker_id or execution.status != "EXECUTING":
                raise ActionError(
                    "ACTION_CONFLICT",
                    "Execution cannot enter compensation from its current state",
                    status_code=409,
                )
            proposal = await session.get(ActionProposalRow, execution.proposal_id)
            now = datetime.now(UTC)
            execution.status = "COMPENSATING"
            execution.error_code = error_code
            execution.error_detail = error_detail
            execution.updated_at = now
            self._record_transition(
                session,
                entity_type="execution",
                entity_id=execution.id,
                from_status="EXECUTING",
                to_status="COMPENSATING",
                reason=error_code,
            )
            if proposal is not None:
                previous_status = proposal.status
                proposal.status = "COMPENSATING"
                proposal.updated_at = now
                self._record_transition(
                    session,
                    entity_type="proposal",
                    entity_id=proposal.id,
                    from_status=previous_status,
                    to_status="COMPENSATING",
                    reason=error_code,
                )
            await session.commit()

    async def finish_compensation(
        self,
        *,
        execution_id: str,
        worker_id: str,
        result: Mapping[str, Any] | None = None,
        compensation_error: bool = False,
    ) -> None:
        async with self._sf() as session:
            execution = await session.get(ActionExecutionRow, execution_id)
            if execution is None or execution.lease_owner != worker_id or execution.status != "COMPENSATING":
                raise ActionError(
                    "ACTION_CONFLICT",
                    "Execution compensation lease is not owned by worker",
                    status_code=409,
                )
            proposal = await session.get(ActionProposalRow, execution.proposal_id)
            status = "FAILED" if compensation_error else "COMPENSATED"
            now = datetime.now(UTC)
            execution.status = status
            execution.result = dict(result) if result is not None else None
            if compensation_error:
                execution.error_code = "COMPENSATION_FAILED"
                execution.error_detail = "Action compensation failed"
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.finished_at = now
            execution.updated_at = now
            self._record_transition(
                session,
                entity_type="execution",
                entity_id=execution.id,
                from_status="COMPENSATING",
                to_status=status,
                reason=execution.error_code,
            )
            if proposal is not None:
                previous_status = proposal.status
                proposal.status = status
                proposal.updated_at = now
                self._record_transition(
                    session,
                    entity_type="proposal",
                    entity_id=proposal.id,
                    from_status=previous_status,
                    to_status=status,
                    reason=execution.error_code,
                )
            await session.commit()

    async def finish(
        self,
        *,
        execution_id: str,
        worker_id: str,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        async with self._sf() as session:
            execution = await session.get(ActionExecutionRow, execution_id)
            if execution is None or execution.lease_owner != worker_id:
                raise ActionError("ACTION_CONFLICT", "Execution lease is not owned by worker", status_code=409)
            proposal = await session.get(ActionProposalRow, execution.proposal_id)
            status = "FAILED" if error_code else "SUCCEEDED"
            now = datetime.now(UTC)
            execution.status = status
            execution.result = dict(result) if result is not None else None
            execution.error_code = error_code
            execution.error_detail = error_detail
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.finished_at = now
            execution.updated_at = now
            if proposal is not None:
                proposal_previous_status = proposal.status
                proposal.status = status
                proposal.updated_at = now
                self._record_transition(
                    session,
                    entity_type="proposal",
                    entity_id=proposal.id,
                    from_status=proposal_previous_status,
                    to_status=status,
                    reason=error_code,
                )
            self._record_transition(
                session,
                entity_type="execution",
                entity_id=execution.id,
                from_status="EXECUTING",
                to_status=status,
                reason=error_code,
            )
            await session.commit()
