"""Normalize Gateway, Semantic, Action and Fixture evidence into one Trial."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.evals.contracts import (
    ActionObservation,
    AssetFingerprint,
    EvalCase,
    EvidenceQuality,
    FixtureOutcome,
    RunObservation,
    SemanticObservation,
    SqlObservation,
    TrajectoryEvent,
    TrialObservation,
)
from app.evals.gateway_client import GatewayTrialResult

_RUN_METADATA_FIELDS = frozenset(
    {
        "run_profile",
        "scope_hash",
        "permission_version",
        "eval_run_id",
        "eval_case_id",
        "eval_trial_index",
        "eval_dataset_hash",
        "model_routing",
    }
)
_ACTION_PROPOSAL_FIELDS = frozenset(
    {
        "proposal_id",
        "action_id",
        "action_version",
        "target_type",
        "target_id",
        "status",
        "approval_required",
        "scope_hash",
        "permission_version",
        "expected_object_version",
        "run_id",
        "thread_id",
        "tool_call_id",
        "semantic_trace_id",
        "created_at",
        "updated_at",
    }
)
_ACTION_EXECUTION_FIELDS = frozenset(
    {
        "execution_id",
        "proposal_id",
        "status",
        "error_code",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
        "action_id",
        "action_version",
        "target_type",
        "target_id",
        "scope_hash",
        "permission_version",
    }
)


def find_correlation_values(value: Any, key: str) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for nested_key, nested_value in value.items():
            if nested_key == key and isinstance(nested_value, str) and nested_value:
                found.add(nested_value)
            found.update(find_correlation_values(nested_value, key))
    elif isinstance(value, list | tuple):
        for item in value:
            found.update(find_correlation_values(item, key))
    return found


def _final_response(payload: dict[str, Any]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, Mapping):
            continue
        message_type = message.get("type") or message.get("role")
        if message_type not in {"ai", "assistant"}:
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def _latency_ms(run: Mapping[str, Any], fallback: int) -> int:
    try:
        created = datetime.fromisoformat(str(run["created_at"]).replace("Z", "+00:00"))
        updated = datetime.fromisoformat(str(run["updated_at"]).replace("Z", "+00:00"))
        return max(0, int((updated - created).total_seconds() * 1000))
    except (KeyError, TypeError, ValueError):
        return fallback


def _string_values(details: Mapping[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        raw = details.get(key)
        if isinstance(raw, str):
            values.add(raw)
        elif isinstance(raw, list | tuple):
            values.update(str(item) for item in raw if item is not None)
    return values


def _project_fields(value: Mapping[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    return {key: value[key] for key in allowed if key in value}


class ObservationCollector:
    def __init__(self, *, semantic: Any, fixture: Any) -> None:
        self._semantic = semantic
        self._fixture = fixture

    async def reset_fixture(
        self,
        *,
        case: EvalCase,
        eval_run_id: str,
        trial_id: str,
        thread_id: str,
        expected_scope_hash: str,
    ) -> dict[str, Any]:
        return await self._fixture.reset(
            case=case,
            eval_run_id=eval_run_id,
            trial_id=trial_id,
            thread_id=thread_id,
            expected_scope_hash=expected_scope_hash,
        )

    async def advance_action(self, **kwargs: Any) -> dict[str, Any]:
        return await self._semantic.approve_and_execute(**kwargs)

    async def collect(
        self,
        *,
        case: EvalCase,
        eval_run_id: str,
        trial_index: int,
        trial_id: str,
        expected_scope_hash: str,
        authorization_token: str,
        before_state: dict[str, Any],
        gateway: GatewayTrialResult,
        git_commit: str | None,
    ) -> TrialObservation:
        missing: list[str] = []
        errors: list[str] = []
        if not any(event.get("event_type") == "run.start" for event in gateway.events):
            missing.append("run_events")

        trajectory: list[TrajectoryEvent] = []
        for event in gateway.events:
            event_type = str(event.get("event_type") or "")
            if not event_type.startswith("tool."):
                continue
            content = event.get("content") if isinstance(event.get("content"), Mapping) else {}
            trajectory.append(
                TrajectoryEvent(
                    event_type=event_type,
                    tool_name=content.get("tool_name"),
                    tool_call_id=content.get("tool_call_id"),
                    caller=content.get("caller"),
                    status=content.get("status"),
                    arguments_hash=content.get("arguments_hash"),
                    evidence_ref=f"run:{gateway.run_id}:event:{event.get('seq', 'unknown')}",
                )
            )

        trace_ids = sorted(find_correlation_values(gateway.events, "semantic_trace_id"))
        semantic_events: list[dict[str, Any]] = []
        for trace_id in trace_ids:
            try:
                payload = await self._semantic.get_trace(
                    trace_id,
                    authorization_token=authorization_token,
                    run_id=gateway.run_id,
                    thread_id=gateway.thread_id,
                )
                if payload and isinstance(payload.get("events"), list):
                    semantic_events.extend(item for item in payload["events"] if isinstance(item, dict))
            except Exception as exc:
                errors.append(f"semantic_trace:{trace_id}:{type(exc).__name__}")
        if case.expect.semantic is not None and not semantic_events:
            missing.append("semantic_audit")

        objects: set[str] = set()
        metrics: set[str] = set()
        actions: set[str] = set()
        scope_hashes: set[str] = set()
        ontology_version: str | None = None
        policy_version: str | None = None
        scope_predicates = 0
        sql_attempted = False
        sql_read_only: bool | None = None
        sql_policy_applied: bool | None = None
        sql_tables: set[str] = set()
        sql_scope_hashes: set[str] = set()
        sql_scope_predicates = 0
        for event in semantic_events:
            details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
            objects.update(_string_values(details, "object_id", "object_ids", "object_type", "object_types", "objects"))
            metrics.update(_string_values(details, "metric_id", "metric_ids", "metrics"))
            actions.update(_string_values(details, "action_id", "action_ids", "actions"))
            scope_hash = event.get("scope_hash")
            if isinstance(scope_hash, str) and scope_hash:
                scope_hashes.add(scope_hash)
            ontology_version = str(details.get("ontology_version") or ontology_version or "") or None
            policy_version = str(details.get("policy_version") or policy_version or "") or None
            scope_predicates = max(scope_predicates, int(details.get("scope_predicates_applied") or 0))
            if "sql" in str(event.get("event_type") or "").lower() or "referenced_tables" in details:
                sql_attempted = True
                sql_read_only = details.get("read_only") if isinstance(details.get("read_only"), bool) else sql_read_only
                sql_policy_applied = details.get("policy_applied") if isinstance(details.get("policy_applied"), bool) else sql_policy_applied
                sql_tables.update(_string_values(details, "referenced_tables"))
                sql_scope_predicates = max(sql_scope_predicates, int(details.get("scope_predicates_applied") or 0))
                if isinstance(scope_hash, str) and scope_hash:
                    sql_scope_hashes.add(scope_hash)

        proposal_ids = sorted(find_correlation_values(gateway.events, "proposal_id"))
        rejection_codes = find_correlation_values(gateway.events, "code")
        rejection_codes.update(find_correlation_values(gateway.events, "error_code"))
        proposals: list[dict[str, Any]] = []
        executions: list[dict[str, Any]] = []
        transitions: list[dict[str, Any]] = []
        action_scope_hashes: set[str] = set()
        for proposal_id in proposal_ids:
            try:
                payload = await self._semantic.get_action(
                    proposal_id,
                    authorization_token=authorization_token,
                    run_id=gateway.run_id,
                    thread_id=gateway.thread_id,
                )
            except Exception as exc:
                errors.append(f"action:{proposal_id}:{type(exc).__name__}")
                continue
            if not payload:
                continue
            proposal = payload.get("proposal")
            execution = payload.get("execution")
            if isinstance(proposal, dict):
                proposals.append(_project_fields(proposal, _ACTION_PROPOSAL_FIELDS))
                if isinstance(proposal.get("scope_hash"), str):
                    action_scope_hashes.add(proposal["scope_hash"])
            if isinstance(execution, dict):
                executions.append(_project_fields(execution, _ACTION_EXECUTION_FIELDS))
                if isinstance(execution.get("scope_hash"), str):
                    action_scope_hashes.add(execution["scope_hash"])
            transitions.extend({"entity_type": "proposal", "entity_id": proposal_id, "to_status": status} for status in payload.get("proposal_transitions", []))
            if isinstance(execution, dict):
                transitions.extend({"entity_type": "execution", "entity_id": execution.get("execution_id"), "to_status": status} for status in payload.get("execution_transitions", []))
        called_tools = {event.tool_name for event in trajectory if event.event_type == "tool.call" and event.tool_name}
        expected_action = case.expect.action
        audited_preflight_rejection = bool(
            expected_action and expected_action.outcome == "rejected" and expected_action.allow_preflight_rejection and semantic_events and ({"get_object", "search_objects", "list_available_actions"}.intersection(called_tools))
        )
        if expected_action is not None and not proposals and not ((expected_action.outcome == "rejected" and rejection_codes) or audited_preflight_rejection):
            missing.append("action_evidence")

        try:
            fixture_payload = await self._fixture.state(trial_id=trial_id)
            after_state = fixture_payload.get("state")
            if not isinstance(after_state, dict):
                raise ValueError("fixture state is missing")
            unexpected_changes = fixture_payload.get("unexpected_changes") or []
        except Exception as exc:
            after_state = {}
            unexpected_changes = []
            missing.append("fixture_outcome")
            errors.append(f"fixture:{type(exc).__name__}")

        raw_run_metadata = gateway.run.get("metadata") if isinstance(gateway.run.get("metadata"), dict) else {}
        run_metadata = _project_fields(raw_run_metadata, _RUN_METADATA_FIELDS)
        routing = run_metadata.get("model_routing") if isinstance(run_metadata.get("model_routing"), dict) else {}
        if any(error.startswith("fixture:") for error in errors):
            evidence_status = "fixture_failed"
        else:
            evidence_status = "complete" if not missing and not errors else "incomplete"
        return TrialObservation(
            eval_run_id=eval_run_id,
            case_id=case.case_id,
            trial_index=trial_index,
            thread_id=gateway.thread_id,
            run_id=gateway.run_id,
            expected_scope_hash=expected_scope_hash,
            run=RunObservation(
                status=str(gateway.run.get("status") or "unknown"),
                metadata=run_metadata,
                error="RUN_ERROR_RECORDED" if gateway.run.get("error") else None,
                total_tokens=int(gateway.run.get("total_tokens") or 0),
                llm_call_count=int(gateway.run.get("llm_call_count") or 0),
                latency_ms=_latency_ms(gateway.run, gateway.latency_ms),
            ),
            final_response=_final_response(gateway.wait_response),
            trajectory=tuple(trajectory),
            semantic=SemanticObservation(
                objects=tuple(sorted(objects)),
                metrics=tuple(sorted(metrics)),
                actions=tuple(sorted(actions)),
                ontology_version=ontology_version,
                policy_version=policy_version,
                scope_hashes=tuple(sorted(scope_hashes)),
                scope_predicates_applied=scope_predicates,
                audit_event_count=len(semantic_events),
                trace_ids=tuple(trace_ids),
            ),
            sql=SqlObservation(
                attempted=sql_attempted,
                read_only=sql_read_only,
                policy_applied=sql_policy_applied,
                scope_predicates_applied=sql_scope_predicates,
                referenced_tables=tuple(sorted(sql_tables)),
                scope_hashes=tuple(sorted(sql_scope_hashes)),
            ),
            action=ActionObservation(
                proposals=tuple(proposals),
                executions=tuple(executions),
                transitions=tuple(transitions),
                scope_hashes=tuple(sorted(action_scope_hashes)),
                rejection_codes=tuple(sorted(rejection_codes)),
            ),
            outcome=FixtureOutcome(
                before=before_state,
                after=after_state,
                unexpected_changes=tuple(str(item) for item in unexpected_changes),
            ),
            assets=AssetFingerprint(
                git_commit=git_commit,
                assistant_id=str(gateway.run.get("assistant_id") or case.target.assistant_id),
                model_name=routing.get("model_name"),
                router_version=routing.get("router_version"),
                rules_version=routing.get("rules_version"),
                index_version=routing.get("index_version"),
                ontology_version=ontology_version,
                policy_version=policy_version,
            ),
            evidence_quality=EvidenceQuality(
                status=evidence_status,
                missing=tuple(dict.fromkeys(missing)),
                errors=tuple(errors),
            ),
        )
