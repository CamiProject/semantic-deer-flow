from __future__ import annotations

import pytest

from app.evals.collector import ObservationCollector
from app.evals.contracts import EvalCase
from app.evals.gateway_client import GatewayTrialResult

pytestmark = pytest.mark.anyio


def _action_case() -> EvalCase:
    return EvalCase.model_validate(
        {
            "case_id": "action-projection",
            "title": "Project Action evidence",
            "category": "action",
            "risk": "high",
            "target": {"assistant_id": "saas-query"},
            "turns": [{"role": "user", "content": "Rename the site"}],
            "fixture": {
                "tenant_id": "public-tenant-001",
                "tenant_code": "public_demo",
                "principal_id": "public-user-001",
                "system_code": "demo",
                "role_codes": ["site_admin"],
                "scope": {
                    "mode": "resource_set",
                    "site_ids": ["site-demo-001"],
                    "project_ids": [],
                },
                "scenario": "site_display_name_old",
            },
            "expect": {
                "action": {
                    "outcome": "success",
                    "action_id": "site.update_display_name",
                }
            },
            "graders": ["action_contract"],
        }
    )


class _SemanticEvidence:
    async def get_action(self, proposal_id, **kwargs):
        del kwargs
        assert proposal_id == "proposal-1"
        return {
            "proposal": {
                "proposal_id": proposal_id,
                "action_id": "site.update_display_name",
                "action_version": "1",
                "target_type": "Site",
                "target_id": "site-demo-001",
                "parameters": {"name": "sensitive display name"},
                "reason": "sensitive user-provided reason",
                "approved_by": "private-approver",
                "status": "SUCCEEDED",
                "approval_required": True,
                "scope_hash": "scope-1",
                "permission_version": "1",
                "expected_object_version": "7",
                "run_id": "run-1",
                "thread_id": "thread-1",
                "tool_call_id": "tool-1",
                "semantic_trace_id": "semantic-1",
                "created_at": "2026-07-29T00:00:00+00:00",
                "updated_at": "2026-07-29T00:00:01+00:00",
            },
            "execution": {
                "execution_id": "execution-1",
                "proposal_id": proposal_id,
                "status": "SUCCEEDED",
                "result": {"private_domain_payload": "must-not-persist"},
                "error_code": None,
                "error_detail": "must-not-persist",
                "started_at": "2026-07-29T00:00:00+00:00",
                "finished_at": "2026-07-29T00:00:01+00:00",
                "created_at": "2026-07-29T00:00:00+00:00",
                "updated_at": "2026-07-29T00:00:01+00:00",
                "action_id": "site.update_display_name",
                "action_version": "1",
                "target_type": "Site",
                "target_id": "site-demo-001",
                "scope_hash": "scope-1",
                "permission_version": "1",
            },
            "proposal_transitions": ["PROPOSED", "SUCCEEDED"],
            "execution_transitions": ["QUEUED", "SUCCEEDED"],
        }


class _Fixture:
    async def state(self, *, trial_id):
        assert trial_id == "trial-1"
        return {"state": {"site_name": "Updated"}, "unexpected_changes": []}


async def test_collector_projects_action_and_run_evidence_to_minimum_safe_fields():
    collector = ObservationCollector(semantic=_SemanticEvidence(), fixture=_Fixture())
    observation = await collector.collect(
        case=_action_case(),
        eval_run_id="eval-1",
        trial_index=0,
        trial_id="trial-1",
        expected_scope_hash="scope-1",
        authorization_token="signed-context",
        before_state={"site_name": "Old"},
        gateway=GatewayTrialResult(
            thread_id="thread-1",
            run_id="run-1",
            wait_response={"messages": [{"type": "ai", "content": "Proposed"}]},
            run={
                "assistant_id": "saas-query",
                "status": "error",
                "error": "database password=must-not-persist",
                "metadata": {
                    "run_profile": "saas-query",
                    "scope_hash": "scope-1",
                    "permission_version": "1",
                    "eval_run_id": "eval-1",
                    "model_routing": {"route_type": "complex", "source": "rules"},
                    "client_private_metadata": "must-not-persist",
                },
            },
            events=[
                {"seq": 1, "event_type": "run.start", "content": {}, "metadata": {}},
                {
                    "seq": 2,
                    "event_type": "custom",
                    "content": {"proposal_id": "proposal-1"},
                    "metadata": {},
                },
            ],
            latency_ms=5,
        ),
        git_commit="abc",
    )

    proposal = observation.action.proposals[0]
    execution = observation.action.executions[0]
    assert set(proposal) == {
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
    assert set(execution) == {
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
    assert set(observation.run.metadata) == {
        "run_profile",
        "scope_hash",
        "permission_version",
        "eval_run_id",
        "model_routing",
    }
    assert observation.run.error == "RUN_ERROR_RECORDED"
    serialized = observation.model_dump_json()
    assert "sensitive display name" not in serialized
    assert "must-not-persist" not in serialized
    assert "private-approver" not in serialized


class _BrokenFixture:
    async def state(self, *, trial_id):
        del trial_id
        raise RuntimeError("fixture unavailable")


async def test_collector_classifies_fixture_state_failure_separately():
    collector = ObservationCollector(semantic=_SemanticEvidence(), fixture=_BrokenFixture())
    observation = await collector.collect(
        case=_action_case(),
        eval_run_id="eval-1",
        trial_index=0,
        trial_id="trial-1",
        expected_scope_hash="scope-1",
        authorization_token="signed-context",
        before_state={},
        gateway=GatewayTrialResult(
            thread_id="thread-1",
            run_id="run-1",
            wait_response={},
            run={"assistant_id": "saas-query", "status": "success", "metadata": {}},
            events=[
                {"seq": 1, "event_type": "run.start", "content": {}, "metadata": {}},
                {"seq": 2, "event_type": "custom", "content": {"proposal_id": "proposal-1"}},
            ],
            latency_ms=5,
        ),
        git_commit=None,
    )

    assert observation.evidence_quality.status == "fixture_failed"
    assert observation.evidence_quality.missing == ("fixture_outcome",)


class _PreflightSemanticEvidence:
    async def get_trace(self, trace_id, **kwargs):
        del kwargs
        assert trace_id == "semantic-preflight-1"
        return {
            "events": [
                {
                    "event_type": "object.get",
                    "scope_hash": "scope-1",
                    "details": {"object_type": "Site", "scope_predicates_applied": 1},
                }
            ]
        }


class _DeniedActionSemanticEvidence:
    async def get_trace(self, trace_id, **kwargs):
        del kwargs
        assert trace_id == "semantic-action-denied"
        return {
            "events": [
                {
                    "event_type": "ontology.resolve",
                    "decision": "deny",
                    "scope_hash": "scope-1",
                    "details": {
                        "action_ids": [],
                        "action_decision": {
                            "status": "denied",
                            "code": "AUTHORIZATION_DENIED",
                        },
                    },
                }
            ]
        }


async def test_collector_accepts_audited_preflight_rejection_as_complete_action_evidence():
    payload = _action_case().model_dump(mode="json")
    payload["expect"]["action"] = {
        "outcome": "rejected",
        "action_id": "site.update_display_name",
        "target_id": "site-demo-002",
        "rejection_code": "AUTHORIZATION_DENIED",
        "allow_preflight_rejection": True,
    }
    case = EvalCase.model_validate(payload)
    collector = ObservationCollector(semantic=_PreflightSemanticEvidence(), fixture=_Fixture())

    observation = await collector.collect(
        case=case,
        eval_run_id="eval-1",
        trial_index=0,
        trial_id="trial-1",
        expected_scope_hash="scope-1",
        authorization_token="signed-context",
        before_state={"site_name": "Updated"},
        gateway=GatewayTrialResult(
            thread_id="thread-1",
            run_id="run-1",
            wait_response={"messages": [{"type": "ai", "content": "Target is outside Scope"}]},
            run={"assistant_id": "saas-query", "status": "success", "metadata": {"scope_hash": "scope-1"}},
            events=[
                {"seq": 1, "event_type": "run.start", "content": {}, "metadata": {}},
                {
                    "seq": 2,
                    "event_type": "tool.call",
                    "content": {"tool_name": "get_object", "tool_call_id": "tool-1"},
                    "semantic_trace_id": "semantic-preflight-1",
                },
            ],
            latency_ms=5,
        ),
        git_commit=None,
    )

    assert observation.action.proposals == ()
    assert observation.action.executions == ()
    assert observation.evidence_quality.status == "complete"
    assert "action_evidence" not in observation.evidence_quality.missing


async def test_collector_accepts_deterministic_action_authorization_preflight():
    payload = _action_case().model_dump(mode="json")
    payload["expect"]["action"] = {
        "outcome": "rejected",
        "action_id": "site.update_display_name",
        "target_id": "site-demo-001",
        "rejection_code": "AUTHORIZATION_DENIED",
        "allow_preflight_rejection": True,
    }
    case = EvalCase.model_validate(payload)
    collector = ObservationCollector(semantic=_DeniedActionSemanticEvidence(), fixture=_Fixture())

    observation = await collector.collect(
        case=case,
        eval_run_id="eval-1",
        trial_index=0,
        trial_id="trial-1",
        expected_scope_hash="scope-1",
        authorization_token="signed-context",
        before_state={"site_name": "Updated"},
        gateway=GatewayTrialResult(
            thread_id="thread-1",
            run_id="run-1",
            wait_response={"messages": [{"type": "ai", "content": "Action denied"}]},
            run={"assistant_id": "saas-query", "status": "success", "metadata": {"scope_hash": "scope-1"}},
            events=[
                {"seq": 1, "event_type": "run.start", "content": {}, "metadata": {}},
                {
                    "seq": 2,
                    "event_type": "custom",
                    "content": {
                        "type": "saas_query_action_preflight_denied",
                        "code": "AUTHORIZATION_DENIED",
                        "semantic_trace_id": "semantic-action-denied",
                    },
                },
            ],
            latency_ms=5,
        ),
        git_commit=None,
    )

    assert observation.semantic.audit_event_count == 1
    assert observation.action.rejection_codes == ("AUTHORIZATION_DENIED",)
    assert observation.action.proposals == ()
    assert observation.action.executions == ()
    assert observation.evidence_quality.status == "complete"
