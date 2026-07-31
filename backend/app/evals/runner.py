"""One-shot concurrent Eval Runner over the real Gateway HTTP lifecycle."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jwt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evals.collector import find_correlation_values
from app.evals.contracts import EvalCase, GateResult, LoadedSuite, ScoreResult, TrialObservation
from app.evals.gate import evaluate_gate
from app.evals.graders import grade_trial
from app.evals.report import write_report
from deerflow.config import get_app_config
from deerflow.runtime.authorization_context import AuthorizationContext


class EvalRunnerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str
    output_root: Path
    max_concurrency: int = Field(default=3, ge=1, le=20)
    default_timeout_seconds: int = Field(default=180, ge=1, le=1800)
    require_persistent_run_events: bool = True
    gateway_url: str
    semantic_url: str
    fixture_url: str
    gateway_internal_token: str = Field(min_length=1)
    semantic_service_token: str = Field(min_length=1)
    fixture_token: str = Field(min_length=1)
    authorization_jwt_key: str = Field(min_length=32)
    authorization_algorithm: str = "HS256"
    authorization_issuer: str = "saas-gateway"
    gateway_audience: str = "deerflow"
    semantic_audience: str = "semantic-platform"
    approval_jwt_key: str | None = None
    approval_jwt_issuer: str | None = None
    authorization_ttl_seconds: int = Field(default=300, ge=30, le=300)
    allowed_hosts: tuple[str, ...] = (
        "localhost",
        "127.0.0.1",
        "::1",
        "gateway",
        "semantic-api",
        "eval-fixture",
    )

    @model_validator(mode="after")
    def validate_isolated_environment(self) -> EvalRunnerSettings:
        if self.environment != "eval":
            raise ValueError("Evals requires DEER_FLOW_ENV=eval")
        if self.default_timeout_seconds > self.authorization_ttl_seconds:
            raise ValueError("Evals default timeout_seconds cannot exceed the authorization JWT TTL")
        for label, raw_url in (
            ("gateway", self.gateway_url),
            ("semantic", self.semantic_url),
            ("fixture", self.fixture_url),
        ):
            parsed = urlsplit(raw_url)
            hostname = (parsed.hostname or "").lower()
            allowed = hostname in self.allowed_hosts or hostname.endswith((".internal", ".test")) or hostname.startswith("eval-")
            if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password or not allowed:
                raise ValueError(f"{label} host is not in the Evals allowlist")
        return self

    @classmethod
    def from_env(
        cls,
        *,
        gateway_url: str,
        suite_output_root: str | Path | None = None,
    ) -> EvalRunnerSettings:
        app_config = get_app_config()
        if not app_config.evals.enabled:
            raise ValueError("Evals is disabled in config.yaml")
        if app_config.evals.evidence.require_persistent_run_events and app_config.run_events.backend != "db":
            raise ValueError("Formal Evals requires config.yaml run_events.backend=db")
        return cls(
            environment=os.environ.get("DEER_FLOW_ENV", ""),
            output_root=Path(suite_output_root or os.environ.get("EVALS_OUTPUT_DIR", app_config.evals.output_dir)),
            max_concurrency=int(os.environ.get("EVALS_MAX_CONCURRENCY", str(app_config.evals.max_concurrency))),
            default_timeout_seconds=int(
                os.environ.get(
                    "EVALS_DEFAULT_TIMEOUT_SECONDS",
                    str(app_config.evals.default_timeout_seconds),
                )
            ),
            require_persistent_run_events=app_config.evals.evidence.require_persistent_run_events,
            gateway_url=gateway_url,
            semantic_url=os.environ.get("DEER_FLOW_SEMANTIC_API_URL", "http://localhost:8003"),
            fixture_url=os.environ.get("EVALS_FIXTURE_BASE_URL", "http://127.0.0.1:8004"),
            gateway_internal_token=os.environ.get("DEER_FLOW_INTERNAL_AUTH_TOKEN", ""),
            semantic_service_token=os.environ.get("DEER_FLOW_SEMANTIC_SERVICE_TOKEN", ""),
            fixture_token=os.environ.get("EVALS_FIXTURE_TOKEN", ""),
            authorization_jwt_key=(os.environ.get("EVALS_AUTHORIZATION_JWT_KEY") or os.environ.get("SAAS_AUTHORIZATION_JWT_KEY", "")),
            authorization_algorithm=os.environ.get("EVALS_AUTHORIZATION_JWT_ALGORITHM", "HS256"),
            authorization_issuer=os.environ.get("SAAS_AUTHORIZATION_JWT_ISSUER", "saas-gateway"),
            gateway_audience=os.environ.get("SAAS_AUTHORIZATION_JWT_AUDIENCE", "deerflow"),
            semantic_audience=os.environ.get("SAAS_AUTHORIZATION_SEMANTIC_AUDIENCE", "semantic-platform"),
            approval_jwt_key=os.environ.get("SAAS_ACTION_APPROVAL_JWT_KEY") or None,
            approval_jwt_issuer=os.environ.get("SAAS_ACTION_APPROVAL_JWT_ISSUER") or None,
        )


@dataclass(frozen=True)
class EvalRunResult:
    eval_run_id: str
    output_dir: Path
    observations: tuple[TrialObservation, ...]
    scores: tuple[ScoreResult, ...]
    gate: GateResult


def _git_commit() -> str | None:
    configured = os.environ.get("GIT_COMMIT_SHA")
    if configured:
        return configured[:128]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip()[:128] or None


def _authorization_context(case: EvalCase) -> AuthorizationContext:
    scope = case.fixture.scope
    return AuthorizationContext.from_mapping(
        {
            "principal_id": case.fixture.principal_id,
            "tenant_id": case.fixture.tenant_id,
            "tenant_code": case.fixture.tenant_code,
            "system_code": case.fixture.system_code,
            "role_codes": case.fixture.role_codes,
            "scope_mode": scope.mode,
            "allowed_site_ids": scope.site_ids,
            "allowed_project_ids": scope.project_ids,
            "permission_version": case.fixture.permission_version,
        }
    )


def issue_eval_authorization_token(
    case: EvalCase,
    *,
    settings: EvalRunnerSettings,
    jti: str,
) -> tuple[str, AuthorizationContext]:
    authorization = _authorization_context(case)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": settings.authorization_issuer,
            "aud": [settings.gateway_audience, settings.semantic_audience],
            "sub": case.fixture.principal_id,
            "tenant_id": case.fixture.tenant_id,
            "tenant_code": case.fixture.tenant_code,
            "tenant_name": case.fixture.tenant_name,
            "system_code": case.fixture.system_code,
            "role_codes": list(case.fixture.role_codes),
            "scope": {
                "mode": case.fixture.scope.mode,
                "site_ids": list(case.fixture.scope.site_ids),
                "project_ids": list(case.fixture.scope.project_ids),
            },
            "permission_version": case.fixture.permission_version,
            "iat": now,
            "exp": now + settings.authorization_ttl_seconds,
            "jti": jti,
        },
        settings.authorization_jwt_key,
        algorithm=settings.authorization_algorithm,
    )
    return token, authorization


class EvalRunner:
    def __init__(self, *, settings: EvalRunnerSettings, gateway: Any, collector: Any) -> None:
        self._settings = settings
        self._gateway = gateway
        self._collector = collector

    async def run(self, loaded: LoadedSuite) -> EvalRunResult:
        for case in loaded.cases:
            timeout_seconds = case.timeout_seconds or self._settings.default_timeout_seconds
            if timeout_seconds > self._settings.authorization_ttl_seconds:
                raise ValueError(f"Case {case.case_id} timeout_seconds cannot exceed the authorization JWT TTL")
        eval_run_id = f"eval-{uuid.uuid4()}"
        started_at = time.time()
        git_commit = await asyncio.to_thread(_git_commit)
        semaphore = asyncio.Semaphore(self._settings.max_concurrency)
        jobs: list[tuple[EvalCase, int]] = []
        for case in loaded.cases:
            default_trials = loaded.suite.high_risk_trials if case.risk in {"high", "critical"} else loaded.suite.default_trials
            trial_count = case.trials or default_trials
            jobs.extend((case, index) for index in range(trial_count))

        async def execute(case: EvalCase, trial_index: int) -> TrialObservation:
            async with semaphore:
                return await self._execute_trial(
                    loaded=loaded,
                    case=case,
                    eval_run_id=eval_run_id,
                    trial_index=trial_index,
                    git_commit=git_commit,
                )

        observations = list(await asyncio.gather(*(execute(case, index) for case, index in jobs)))
        observations.sort(key=lambda item: (item.case_id, item.trial_index))
        case_by_id = {case.case_id: case for case in loaded.cases}
        scores: list[ScoreResult] = []
        for observation in observations:
            scores.extend(grade_trial(case_by_id[observation.case_id], observation))
        gate_config = loaded.suite.gate
        gate = evaluate_gate(
            scores,
            fail_on_any_p0=gate_config.fail_on_any_p0,
            minimum_p1_score=gate_config.minimum_p1_score,
            minimum_quality_score=gate_config.minimum_quality_score,
            conditional_quality_score=gate_config.conditional_quality_score,
        )
        output_dir = write_report(
            output_root=self._settings.output_root,
            eval_run_id=eval_run_id,
            manifest={
                "schema_version": "1",
                "suite_id": loaded.suite.suite_id,
                "suite_version": loaded.suite.version,
                "dataset_hash": loaded.dataset_hash,
                "git_commit": git_commit or "unknown",
                "started_at_unix": started_at,
                "finished_at_unix": time.time(),
                "max_concurrency": self._settings.max_concurrency,
                "trial_count": len(observations),
            },
            observations=observations,
            scores=scores,
            fail_on_any_p0=gate_config.fail_on_any_p0,
            minimum_p1_score=gate_config.minimum_p1_score,
            minimum_quality_score=gate_config.minimum_quality_score,
            conditional_quality_score=gate_config.conditional_quality_score,
        )
        return EvalRunResult(
            eval_run_id=eval_run_id,
            output_dir=output_dir,
            observations=tuple(observations),
            scores=tuple(scores),
            gate=gate,
        )

    async def _execute_trial(
        self,
        *,
        loaded: LoadedSuite,
        case: EvalCase,
        eval_run_id: str,
        trial_index: int,
        git_commit: str | None,
    ) -> TrialObservation:
        trial_id = f"trial-{uuid.uuid4()}"
        thread_id = str(uuid.uuid4())
        token, authorization = issue_eval_authorization_token(
            case,
            settings=self._settings,
            jti=str(uuid.uuid4()),
        )
        timeout_seconds = case.timeout_seconds or self._settings.default_timeout_seconds
        before_state: dict[str, Any] = {}

        def failed_observation(
            exc: Exception,
            *,
            evidence_status: str,
            missing: list[str],
        ) -> TrialObservation:
            return TrialObservation(
                eval_run_id=eval_run_id,
                case_id=case.case_id,
                trial_index=trial_index,
                thread_id=thread_id,
                run_id=f"unavailable-{trial_id}",
                expected_scope_hash=authorization.scope_hash,
                run={
                    "status": "error",
                    "metadata": {},
                    "error": type(exc).__name__,
                },
                outcome={"before": before_state, "after": {}, "unexpected_changes": []},
                assets={"git_commit": git_commit, "assistant_id": case.target.assistant_id},
                evidence_quality={
                    "status": evidence_status,
                    "missing": missing,
                    "errors": [type(exc).__name__],
                },
            )

        try:
            async with asyncio.timeout(timeout_seconds):
                try:
                    before_state = await self._collector.reset_fixture(
                        case=case,
                        eval_run_id=eval_run_id,
                        trial_id=trial_id,
                        thread_id=thread_id,
                        expected_scope_hash=authorization.scope_hash,
                    )
                except Exception as exc:
                    return failed_observation(
                        exc,
                        evidence_status="fixture_failed",
                        missing=["fixture_before"],
                    )
                gateway_result = await self._gateway.execute_trial(
                    case=case,
                    eval_run_id=eval_run_id,
                    trial_index=trial_index,
                    dataset_hash=loaded.dataset_hash,
                    thread_id=thread_id,
                    authorization_token=token,
                    timeout_seconds=timeout_seconds,
                )
                if case.expect.action is not None and case.expect.action.workflow == "approve_and_execute":
                    proposal_ids = sorted(find_correlation_values(gateway_result.events, "proposal_id"))
                    if len(proposal_ids) != 1:
                        raise RuntimeError(f"approve_and_execute requires exactly one proposal; observed {len(proposal_ids)}")
                    await self._collector.advance_action(
                        proposal_id=proposal_ids[0],
                        principal_id=case.fixture.principal_id,
                        expected_scope_hash=authorization.scope_hash,
                        authorization_token=token,
                        run_id=gateway_result.run_id,
                        thread_id=thread_id,
                        timeout_seconds=timeout_seconds,
                    )
                return await self._collector.collect(
                    case=case,
                    eval_run_id=eval_run_id,
                    trial_index=trial_index,
                    trial_id=trial_id,
                    expected_scope_hash=authorization.scope_hash,
                    authorization_token=token,
                    before_state=before_state,
                    gateway=gateway_result,
                    git_commit=git_commit,
                )
        except Exception as exc:
            return failed_observation(
                exc,
                evidence_status="collector_failed",
                missing=["run", "run_events", "semantic_audit", "fixture_outcome"],
            )
