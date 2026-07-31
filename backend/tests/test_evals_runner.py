from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import jwt
import pytest

from app.evals.collector import ObservationCollector
from app.evals.contracts import EvalCase, EvalSuite, LoadedSuite, TrialObservation
from app.evals.fixture_client import FixtureClient
from app.evals.gateway_client import GatewayClient, GatewayTrialResult
from app.evals.loader import load_suite
from app.evals.runner import EvalRunner, EvalRunnerSettings
from app.evals.semantic_client import SemanticEvidenceClient
from deerflow.runtime.authorization_context import AuthorizationContext

JWT_KEY = "eval-jwt-secret-at-least-thirty-two-bytes"


def _write_suite(tmp_path: Path) -> Path:
    cases_dir = tmp_path / "cases"
    suites_dir = tmp_path / "suites"
    cases_dir.mkdir()
    suites_dir.mkdir()
    case = {
        "schema_version": "1",
        "case_id": "semantic-site-count",
        "title": "Count visible sites",
        "category": "semantic_read",
        "risk": "medium",
        "target": {"assistant_id": "saas-query", "endpoint_mode": "wait"},
        "turns": [{"role": "user", "content": "Count visible sites"}],
        "fixture": {
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "principal_id": "public-user-001",
            "system_code": "demo",
            "role_codes": ["site_admin"],
            "scope": {"mode": "resource_set", "site_ids": ["site-demo-001"], "project_ids": []},
            "scenario": "one_visible",
        },
        "expect": {
            "answer": {"numeric_value": 1},
            "semantic": {"objects": ["Site"], "metrics": ["site.count"]},
            "routing": {"route_type": "simple", "source": "rules"},
            "trajectory": {"required_tools": ["semantic_query"], "forbidden_tools": ["bash"]},
            "invariants": ["scope_hash_unchanged", "no_write_side_effect"],
        },
        "graders": [
            "run_completed",
            "answer_exact_or_numeric",
            "semantic_contract",
            "scope_integrity",
            "forbidden_side_effect",
            "routing_decision",
            "tool_trajectory",
        ],
        "trials": 1,
        "timeout_seconds": 10,
    }
    (cases_dir / "smoke.jsonl").write_text(json.dumps(case) + "\n", encoding="utf-8")
    suite = """\
schema_version: "1"
suite_id: saas-agent-smoke
version: "1"
case_files:
  - ../cases/smoke.jsonl
gate:
  fail_on_any_p0: true
  minimum_p1_score: 0.8
"""
    suite_path = suites_dir / "smoke.yaml"
    suite_path.write_text(suite, encoding="utf-8")
    return suite_path


def _scope_hash(token: str) -> str:
    claims = jwt.decode(token, JWT_KEY, algorithms=["HS256"], options={"verify_aud": False})
    scope = claims["scope"]
    authorization = AuthorizationContext.from_mapping(
        {
            "principal_id": claims["sub"],
            "tenant_id": claims["tenant_id"],
            "tenant_code": claims["tenant_code"],
            "system_code": claims["system_code"],
            "role_codes": claims["role_codes"],
            "scope_mode": scope["mode"],
            "allowed_site_ids": scope["site_ids"],
            "allowed_project_ids": scope["project_ids"],
            "permission_version": claims["permission_version"],
        }
    )
    return authorization.scope_hash


@pytest.mark.anyio
async def test_runner_executes_real_http_contract_collects_evidence_and_writes_gate_report(tmp_path):
    requests: list[tuple[str, str]] = []
    state: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        host = urlparse(str(request.url)).hostname
        if host == "fixture.internal" and request.url.path.endswith("/reset"):
            payload = json.loads(request.content)
            state["trial_id"] = payload["trial_id"]
            return httpx.Response(200, json={"state": {"site_count": 1}})
        if host == "gateway.internal" and request.method == "POST":
            token = request.headers["X-SaaS-Authorization-Context"]
            payload = json.loads(request.content)
            metadata = payload["metadata"]
            thread_id = payload["config"]["configurable"]["thread_id"]
            assert request.headers["X-DeerFlow-Owner-User-Id"] == "public-user-001"
            assert metadata["eval_run_id"]
            assert metadata["eval_case_id"] == "semantic-site-count"
            state.update(token=token, metadata=metadata, thread_id=thread_id, scope_hash=_scope_hash(token))
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"type": "human", "content": "Count visible sites"},
                        {"type": "ai", "content": "There is 1 visible site."},
                    ]
                },
            )
        if host == "gateway.internal" and request.url.path.endswith("/runs"):
            return httpx.Response(
                200,
                json=[
                    {
                        "run_id": "run-1",
                        "thread_id": state["thread_id"],
                        "assistant_id": "saas-query",
                        "status": "success",
                        "metadata": {
                            **state["metadata"],
                            "scope_hash": state["scope_hash"],
                            "model_routing": {"route_type": "simple", "source": "rules", "model_name": "small"},
                        },
                        "created_at": "2026-07-28T00:00:00+00:00",
                        "updated_at": "2026-07-28T00:00:01+00:00",
                        "total_tokens": 100,
                        "llm_call_count": 1,
                    }
                ],
            )
        if host == "gateway.internal" and request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json=[
                    {"seq": 1, "event_type": "run.start", "content": {}, "metadata": {}},
                    {
                        "seq": 2,
                        "event_type": "tool.call",
                        "content": {
                            "tool_name": "semantic_query",
                            "tool_call_id": "call-1",
                            "caller": "lead_agent",
                            "status": "started",
                            "arguments_hash": "a" * 64,
                        },
                        "metadata": {},
                    },
                    {
                        "seq": 3,
                        "event_type": "llm.tool.result",
                        "content": {"semantic_trace_id": "trace-1"},
                        "metadata": {},
                    },
                ],
            )
        if host == "semantic.internal" and request.url.path == "/v1/audit/traces/trace-1":
            assert request.headers["X-SaaS-Authorization-Context"] == state["token"]
            return httpx.Response(
                200,
                json={
                    "semantic_trace_id": "trace-1",
                    "events": [
                        {
                            "id": "audit-1",
                            "semantic_trace_id": "trace-1",
                            "run_id": "run-1",
                            "thread_id": state["thread_id"],
                            "tool_call_id": "call-1",
                            "event_type": "metric.query",
                            "decision": "allow",
                            "details": {
                                "object_ids": ["Site"],
                                "metric_ids": ["site.count"],
                                "ontology_version": "1",
                                "policy_version": "1",
                            },
                            "scope_hash": state["scope_hash"],
                            "permission_version": "1",
                            "created_at": "2026-07-28T00:00:00+00:00",
                        }
                    ],
                },
            )
        if host == "fixture.internal" and request.url.path.endswith("/state"):
            return httpx.Response(200, json={"state": {"site_count": 1}, "unexpected_changes": []})
        return httpx.Response(404, json={"path": request.url.path})

    transport = httpx.MockTransport(handler)
    loaded = load_suite(_write_suite(tmp_path))
    settings = EvalRunnerSettings(
        environment="eval",
        output_root=tmp_path / "results",
        max_concurrency=2,
        default_timeout_seconds=30,
        gateway_url="http://gateway.internal",
        semantic_url="http://semantic.internal",
        fixture_url="http://fixture.internal",
        gateway_internal_token="gateway-secret",
        semantic_service_token="semantic-secret",
        fixture_token="fixture-secret",
        authorization_jwt_key=JWT_KEY,
        authorization_issuer="saas-gateway",
        gateway_audience="deerflow",
        semantic_audience="semantic-platform",
    )
    gateway = GatewayClient(settings, transport=transport)
    semantic = SemanticEvidenceClient(settings, transport=transport)
    fixture = FixtureClient(settings, transport=transport)
    collector = ObservationCollector(semantic=semantic, fixture=fixture)
    runner = EvalRunner(settings=settings, gateway=gateway, collector=collector)

    result = await runner.run(loaded)

    assert result.gate.status == "passed"
    assert len(result.observations) == 1
    assert result.observations[0].semantic.metrics == ("site.count",)
    assert result.observations[0].assets.ontology_version == "1"
    assert (result.output_dir / "report.json").exists()
    assert ("POST", "/api/runs/saas-query/wait") in requests
    assert any(path.endswith("/events") for _method, path in requests)
    assert ("GET", "/v1/audit/traces/trace-1") in requests


def test_runner_settings_reject_non_eval_environment_and_public_fixture_host(tmp_path):
    common = {
        "output_root": tmp_path,
        "gateway_url": "http://gateway.internal",
        "semantic_url": "http://semantic.internal",
        "fixture_url": "https://saas.example.com",
        "gateway_internal_token": "gateway-secret",
        "semantic_service_token": "semantic-secret",
        "fixture_token": "fixture-secret",
        "authorization_jwt_key": JWT_KEY,
    }

    with pytest.raises(ValueError, match="DEER_FLOW_ENV=eval"):
        EvalRunnerSettings(environment="production", **common)
    with pytest.raises(ValueError, match="fixture host"):
        EvalRunnerSettings(environment="eval", **common)


def test_runner_settings_from_env_prefers_dedicated_eval_signing_key(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.evals.runner.get_app_config",
        lambda: SimpleNamespace(
            evals=SimpleNamespace(
                enabled=True,
                output_dir=str(tmp_path),
                max_concurrency=1,
                default_timeout_seconds=60,
                evidence=SimpleNamespace(require_persistent_run_events=True),
            ),
            run_events=SimpleNamespace(backend="db"),
        ),
    )
    monkeypatch.setenv("DEER_FLOW_ENV", "eval")
    monkeypatch.setenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", "gateway")
    monkeypatch.setenv("DEER_FLOW_SEMANTIC_SERVICE_TOKEN", "semantic")
    monkeypatch.setenv("EVALS_FIXTURE_TOKEN", "fixture")
    monkeypatch.setenv("EVALS_AUTHORIZATION_JWT_KEY", "eval-signing-secret-at-least-32-bytes")
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", "production-verification-key-must-not-be-used")

    settings = EvalRunnerSettings.from_env(gateway_url="http://gateway.internal")

    assert settings.authorization_jwt_key == "eval-signing-secret-at-least-32-bytes"


def _runner_case(*, trials: int, timeout_seconds: int = 10) -> EvalCase:
    return EvalCase.model_validate(
        {
            "schema_version": "1",
            "case_id": "runner-isolation",
            "title": "Runner isolation",
            "category": "model_routing",
            "risk": "low",
            "target": {"assistant_id": "saas-query", "endpoint_mode": "wait"},
            "turns": [{"role": "user", "content": "hello"}],
            "fixture": {
                "tenant_id": "public-tenant-001",
                "tenant_code": "public_demo",
                "principal_id": "public-user-001",
                "system_code": "demo",
                "role_codes": ["viewer"],
                "scope": {"mode": "resource_set", "site_ids": ["site-demo-001"], "project_ids": []},
                "scenario": "one_visible",
            },
            "graders": ["run_completed"],
            "trials": trials,
            "timeout_seconds": timeout_seconds,
        }
    )


def _loaded(case: EvalCase) -> LoadedSuite:
    return LoadedSuite(
        suite=EvalSuite(
            suite_id="runner-test",
            version="1",
            case_files=("unused.jsonl",),
        ),
        cases=(case,),
        dataset_hash="a" * 64,
    )


class _ConcurrentGateway:
    def __init__(self, *, delay: float):
        self.delay = delay
        self.active = 0
        self.maximum_active = 0
        self.thread_ids: set[str] = set()
        self.jtis: set[str] = set()

    async def execute_trial(self, **kwargs):
        claims = jwt.decode(
            kwargs["authorization_token"],
            JWT_KEY,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        self.thread_ids.add(kwargs["thread_id"])
        self.jtis.add(claims["jti"])
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            import asyncio

            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        trial_index = kwargs["trial_index"]
        return GatewayTrialResult(
            thread_id=kwargs["thread_id"],
            run_id=f"run-{trial_index}",
            wait_response={"messages": [{"type": "ai", "content": "ok"}]},
            run={
                "run_id": f"run-{trial_index}",
                "assistant_id": "saas-query",
                "status": "success",
                "metadata": {},
            },
            events=[{"seq": 1, "event_type": "run.start", "content": {}, "metadata": {}}],
            latency_ms=1,
        )


class _RunnerCollector:
    def __init__(self):
        self.trial_ids: set[str] = set()

    async def reset_fixture(self, **kwargs):
        self.trial_ids.add(kwargs["trial_id"])
        return {}

    async def collect(self, **kwargs):
        gateway = kwargs["gateway"]
        return TrialObservation(
            eval_run_id=kwargs["eval_run_id"],
            case_id=kwargs["case"].case_id,
            trial_index=kwargs["trial_index"],
            thread_id=gateway.thread_id,
            run_id=gateway.run_id,
            expected_scope_hash=kwargs["expected_scope_hash"],
            run={"status": "success", "metadata": {}},
            final_response="ok",
        )


@pytest.mark.anyio
async def test_runner_limits_concurrency_and_isolates_trial_thread_jti_and_fixture(tmp_path):
    settings = EvalRunnerSettings(
        environment="eval",
        output_root=tmp_path,
        max_concurrency=2,
        gateway_url="http://gateway.internal",
        semantic_url="http://semantic.internal",
        fixture_url="http://fixture.internal",
        gateway_internal_token="gateway",
        semantic_service_token="semantic",
        fixture_token="fixture",
        authorization_jwt_key=JWT_KEY,
    )
    gateway = _ConcurrentGateway(delay=0.05)
    collector = _RunnerCollector()

    result = await EvalRunner(settings=settings, gateway=gateway, collector=collector).run(_loaded(_runner_case(trials=4)))

    assert result.gate.status == "passed"
    assert gateway.maximum_active == 2
    assert len(gateway.thread_ids) == 4
    assert len(gateway.jtis) == 4
    assert len(collector.trial_ids) == 4


@pytest.mark.anyio
async def test_runner_turns_timeout_into_fail_closed_observation(tmp_path):
    settings = EvalRunnerSettings(
        environment="eval",
        output_root=tmp_path,
        max_concurrency=1,
        gateway_url="http://gateway.internal",
        semantic_url="http://semantic.internal",
        fixture_url="http://fixture.internal",
        gateway_internal_token="gateway",
        semantic_service_token="semantic",
        fixture_token="fixture",
        authorization_jwt_key=JWT_KEY,
    )
    gateway = _ConcurrentGateway(delay=2)

    result = await EvalRunner(
        settings=settings,
        gateway=gateway,
        collector=_RunnerCollector(),
    ).run(_loaded(_runner_case(trials=1, timeout_seconds=1)))

    assert result.gate.status == "failed"
    assert result.observations[0].run.status == "error"
    assert "TimeoutError" in (result.observations[0].run.error or "")
    assert result.observations[0].evidence_quality.status == "collector_failed"


class _ResetFailingCollector(_RunnerCollector):
    async def reset_fixture(self, **kwargs):
        del kwargs
        raise RuntimeError("fixture password=must-not-persist")


@pytest.mark.anyio
async def test_runner_classifies_fixture_reset_failure_separately(tmp_path):
    settings = EvalRunnerSettings(
        environment="eval",
        output_root=tmp_path,
        max_concurrency=1,
        gateway_url="http://gateway.internal",
        semantic_url="http://semantic.internal",
        fixture_url="http://fixture.internal",
        gateway_internal_token="gateway",
        semantic_service_token="semantic",
        fixture_token="fixture",
        authorization_jwt_key=JWT_KEY,
    )

    result = await EvalRunner(
        settings=settings,
        gateway=_ConcurrentGateway(delay=0),
        collector=_ResetFailingCollector(),
    ).run(_loaded(_runner_case(trials=1)))

    observation = result.observations[0]
    assert result.gate.status == "incomplete"
    assert observation.evidence_quality.status == "fixture_failed"
    assert observation.evidence_quality.missing == ("fixture_before",)
    assert observation.run.status == "error"
    assert "must-not-persist" not in observation.model_dump_json()


@pytest.mark.anyio
async def test_runner_rejects_case_timeout_longer_than_authorization_jwt_ttl(tmp_path):
    settings = EvalRunnerSettings(
        environment="eval",
        output_root=tmp_path,
        max_concurrency=1,
        default_timeout_seconds=180,
        authorization_ttl_seconds=300,
        gateway_url="http://gateway.internal",
        semantic_url="http://semantic.internal",
        fixture_url="http://fixture.internal",
        gateway_internal_token="gateway",
        semantic_service_token="semantic",
        fixture_token="fixture",
        authorization_jwt_key=JWT_KEY,
    )

    with pytest.raises(ValueError, match="timeout_seconds.*authorization JWT TTL"):
        await EvalRunner(
            settings=settings,
            gateway=_ConcurrentGateway(delay=0),
            collector=_RunnerCollector(),
        ).run(_loaded(_runner_case(trials=1, timeout_seconds=301)))
