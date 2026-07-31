"""Versioned contracts shared by Evals loaders, runners and graders."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SchemaVersion = Literal["1"]
Risk = Literal["low", "medium", "high", "critical"]
Priority = Literal["P0", "P1", "P2"]
ScoreStatus = Literal["passed", "failed", "incomplete"]

_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "connection",
    "cookie",
    "jdbc",
    "password",
    "secret",
    "token",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalGate(StrictModel):
    fail_on_any_p0: bool = True
    # Kept for loading older suites and legacy report comparison; Quality Score drives release decisions.
    minimum_p1_score: float = Field(default=0.8, ge=0, le=1)
    minimum_quality_score: float = Field(default=8.0, ge=0, le=10)
    conditional_quality_score: float = Field(default=7.0, ge=0, le=10)
    maximum_token_regression: float | None = Field(default=None, ge=0)
    maximum_latency_regression: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_quality_thresholds(self) -> EvalGate:
        if not self.fail_on_any_p0:
            raise ValueError("P0 hard gates cannot be disabled")
        if self.conditional_quality_score > self.minimum_quality_score:
            raise ValueError("conditional_quality_score cannot exceed minimum_quality_score")
        return self


class EvalSuite(StrictModel):
    schema_version: SchemaVersion = "1"
    suite_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    version: str = Field(min_length=1, max_length=128)
    case_files: tuple[str, ...] = Field(min_length=1)
    default_trials: int = Field(default=1, ge=1, le=10)
    high_risk_trials: int = Field(default=3, ge=1, le=10)
    gate: EvalGate = Field(default_factory=EvalGate)


class EvalTarget(StrictModel):
    assistant_id: str = Field(default="saas-query", min_length=1, max_length=128)
    endpoint_mode: Literal["wait"] = "wait"


class EvalTurn(StrictModel):
    role: Literal["user"] = "user"
    content: str = Field(min_length=1, max_length=20_000)


class EvalScope(StrictModel):
    mode: Literal["tenant_all", "resource_set"]
    site_ids: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> EvalScope:
        if len(set(self.site_ids)) != len(self.site_ids) or len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError("scope resource identifiers must be unique")
        if self.mode == "tenant_all" and (self.site_ids or self.project_ids):
            raise ValueError("tenant_all scope cannot include resource identifiers")
        return self


class EvalFixture(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    tenant_code: str = Field(min_length=1, max_length=128)
    tenant_name: str = Field(default="DeerFlow Eval Tenant", min_length=1, max_length=256)
    principal_id: str = Field(min_length=1, max_length=128)
    system_code: str = Field(min_length=1, max_length=128)
    permission_version: str = Field(default="1", min_length=1, max_length=128)
    role_codes: tuple[str, ...] = ()
    scope: EvalScope
    scenario: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_eval_identity(self) -> EvalFixture:
        if not self.tenant_id.startswith("public-") or not self.principal_id.startswith("public-"):
            raise ValueError("evaluation fixtures must use public synthetic tenant and principal identities")
        return self


class AnswerExpectation(StrictModel):
    exact_text: str | None = None
    contains: tuple[str, ...] = ()
    contains_any: tuple[str, ...] = ()
    numeric_value: float | None = None
    tolerance: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_assertion(self) -> AnswerExpectation:
        if self.exact_text is None and not self.contains and not self.contains_any and self.numeric_value is None:
            raise ValueError("answer expectation requires exact_text, contains, contains_any or numeric_value")
        return self


class SemanticExpectation(StrictModel):
    objects: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    ontology_version: str | None = None
    policy_version: str | None = None
    scope_predicates_min: int = Field(default=0, ge=0)


class RoutingExpectation(StrictModel):
    route_type: Literal["simple", "complex"]
    source: Literal["rules", "faiss", "fallback"] | None = None
    model_name: str | None = None


class TrajectoryExpectation(StrictModel):
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()


class SqlExpectation(StrictModel):
    required: bool = True
    read_only: bool = True
    scope_predicates_min: int = Field(default=0, ge=0)
    allowed_tables: tuple[str, ...] = ()


class ActionExpectation(StrictModel):
    outcome: Literal["success", "rejected", "proposed"] = "proposed"
    workflow: Literal["observe", "approve_and_execute"] = "observe"
    action_id: str | None = None
    target_id: str | None = None
    proposal_status: str | None = None
    execution_status: str | None = None
    rejection_code: str | None = None
    allow_preflight_rejection: bool = False
    expected_after: dict[str, Any] = Field(default_factory=dict)


class EvalExpectation(StrictModel):
    answer: AnswerExpectation | None = None
    semantic: SemanticExpectation | None = None
    routing: RoutingExpectation | None = None
    trajectory: TrajectoryExpectation = Field(default_factory=TrajectoryExpectation)
    sql: SqlExpectation | None = None
    action: ActionExpectation | None = None
    invariants: tuple[str, ...] = ()


class EvalCase(StrictModel):
    schema_version: SchemaVersion = "1"
    case_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    title: str = Field(min_length=1, max_length=256)
    category: Literal[
        "conversation",
        "semantic_read",
        "sql_scope",
        "action",
        "model_routing",
        "research",
        "failure_recovery",
    ]
    risk: Risk
    tags: tuple[str, ...] = ()
    target: EvalTarget
    turns: tuple[EvalTurn, ...] = Field(min_length=1)
    fixture: EvalFixture
    expect: EvalExpectation = Field(default_factory=EvalExpectation)
    graders: tuple[str, ...] = Field(min_length=1)
    trials: int | None = Field(default=None, ge=1, le=10)
    timeout_seconds: int | None = Field(default=None, ge=1, le=1800)

    @model_validator(mode="after")
    def validate_unique_values(self) -> EvalCase:
        if len(set(self.graders)) != len(self.graders):
            raise ValueError("graders must be unique")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags must be unique")
        return self


class RunObservation(StrictModel):
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    total_tokens: int = Field(default=0, ge=0)
    llm_call_count: int = Field(default=0, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)


class TrajectoryEvent(StrictModel):
    event_type: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    caller: str | None = None
    status: str | None = None
    arguments_hash: str | None = None
    evidence_ref: str | None = None


class SemanticObservation(StrictModel):
    objects: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    ontology_version: str | None = None
    policy_version: str | None = None
    scope_hashes: tuple[str, ...] = ()
    scope_predicates_applied: int = Field(default=0, ge=0)
    audit_event_count: int = Field(default=0, ge=0)
    trace_ids: tuple[str, ...] = ()


class SqlObservation(StrictModel):
    attempted: bool = False
    read_only: bool | None = None
    policy_applied: bool | None = None
    scope_predicates_applied: int = Field(default=0, ge=0)
    referenced_tables: tuple[str, ...] = ()
    scope_hashes: tuple[str, ...] = ()


class ActionObservation(StrictModel):
    proposals: tuple[dict[str, Any], ...] = ()
    executions: tuple[dict[str, Any], ...] = ()
    transitions: tuple[dict[str, Any], ...] = ()
    scope_hashes: tuple[str, ...] = ()
    rejection_codes: tuple[str, ...] = ()


class FixtureOutcome(StrictModel):
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    unexpected_changes: tuple[str, ...] = ()


class AssetFingerprint(StrictModel):
    git_commit: str | None = None
    assistant_id: str | None = None
    model_name: str | None = None
    router_version: str | None = None
    rules_version: str | None = None
    index_version: str | None = None
    ontology_version: str | None = None
    policy_version: str | None = None
    prompt_hash: str | None = None
    tool_schema_hash: str | None = None
    config_hash: str | None = None


class EvidenceQuality(StrictModel):
    status: Literal["complete", "incomplete", "fixture_failed", "collector_failed"] = "complete"
    missing: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _find_sensitive_path(value: Any, path: str = "raw_evidence") -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS):
                return f"{path}.{key_text}"
            found = _find_sensitive_path(item, f"{path}.{key_text}")
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_sensitive_path(item, f"{path}[{index}]")
            if found:
                return found
    return None


class TrialObservation(StrictModel):
    schema_version: SchemaVersion = "1"
    eval_run_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    trial_index: int = Field(ge=0)
    thread_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    expected_scope_hash: str = Field(min_length=1, max_length=128)
    run: RunObservation
    final_response: str = ""
    trajectory: tuple[TrajectoryEvent, ...] = ()
    semantic: SemanticObservation = Field(default_factory=SemanticObservation)
    sql: SqlObservation = Field(default_factory=SqlObservation)
    action: ActionObservation = Field(default_factory=ActionObservation)
    outcome: FixtureOutcome = Field(default_factory=FixtureOutcome)
    assets: AssetFingerprint = Field(default_factory=AssetFingerprint)
    evidence_quality: EvidenceQuality = Field(default_factory=EvidenceQuality)
    raw_evidence: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="after")
    def reject_sensitive_raw_evidence(self) -> TrialObservation:
        path = _find_sensitive_path(self.raw_evidence)
        if path:
            raise ValueError(f"sensitive evidence key is not allowed: {path}")
        return self


class ScoreResult(StrictModel):
    schema_version: SchemaVersion = "1"
    case_id: str
    trial_index: int = Field(ge=0)
    grader_id: str
    grader_version: str = "1"
    dimension: str
    priority: Priority
    status: ScoreStatus
    score: float | None = Field(default=None, ge=0, le=1)
    passed: bool
    hard_gate: bool
    reason_code: str
    summary: str
    evidence_refs: tuple[str, ...] = ()
    grader_error: str | None = None

    @model_validator(mode="after")
    def validate_status(self) -> ScoreResult:
        if self.status == "passed" and (not self.passed or self.score is None):
            raise ValueError("passed status requires passed=true and a score")
        if self.status != "passed" and self.passed:
            raise ValueError("failed or incomplete score cannot be marked as passed")
        if self.status == "incomplete" and self.score is not None:
            raise ValueError("incomplete score must not contain a numeric score")
        return self


class LoadedSuite(StrictModel):
    suite: EvalSuite
    cases: tuple[EvalCase, ...]
    dataset_hash: str = Field(min_length=64, max_length=64)


class GateResult(StrictModel):
    status: ScoreStatus
    passed: bool
    hard_gate_status: ScoreStatus
    p0_failures: int = Field(ge=0)
    incomplete_required: int = Field(ge=0)
    p1_score: float | None = Field(default=None, ge=0, le=1)
    quality_score: float | None = Field(default=None, ge=0, le=10)
    quality_dimensions: dict[str, float] = Field(default_factory=dict)
    release_recommendation: Literal["release", "conditional", "hold"]
    reason_codes: tuple[str, ...] = ()
