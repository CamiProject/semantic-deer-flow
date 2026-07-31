"""Tests for the Gateway two-stage model router."""

from __future__ import annotations

import json

import pytest

from app.gateway.model_routing import (
    ModelRoutingConfigError,
    RoutingInput,
    RoutingSignals,
    build_routing_input,
    route_model,
)
from app.gateway.model_routing.faiss_search import (
    FaissConfigurationError,
    FaissSearcher,
    FaissSearchError,
    FaissSearchResult,
    HashingEmbeddingProvider,
    build_faiss_index,
    get_faiss_searcher,
)
from app.gateway.model_routing.rules import classify_rules
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.config.model_routing_config import FaissRoutingConfig


def _app_config(*, mode: str = "enforce", simple_model: str | None = "simple-model", complex_model: str | None = "complex-model") -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "models": [
                {"name": "simple-model", "use": "pkg:Simple", "model": "simple"},
                {"name": "complex-model", "use": "pkg:Complex", "model": "complex"},
            ],
            "model_routing": {
                "mode": mode,
                "simple_model": simple_model,
                "complex_model": complex_model,
            },
        }
    )


class _FailIfCalledSearcher:
    async def search(self, _question: str):
        raise AssertionError("FAISS must not run after a deterministic rule decision")


class _FakeSearcher:
    def __init__(self, result: FaissSearchResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.questions: list[str] = []

    async def search(self, question: str):
        self.questions.append(question)
        if self.error:
            raise self.error
        return self.result


def test_rules_route_simple_read_without_faiss():
    result = classify_rules(RoutingInput("查询某场地本月能耗"))

    assert result.route_type == "simple"
    assert result.reason_codes == ("single_resource_read",)
    assert result.signals.difficulty_level == "low"


def test_rules_route_write_and_analysis_as_complex():
    assert classify_rules(RoutingInput("修改场地显示名称")).route_type == "complex"
    assert classify_rules(RoutingInput("对比多个场地趋势并解释原因")).route_type == "complex"


def test_rules_leave_ambiguous_question_for_faiss():
    result = classify_rules(RoutingInput("帮我看看这个数据"))

    assert result.route_type is None
    assert result.reason_codes == ("rule_undecided",)


@pytest.mark.asyncio
async def test_route_model_uses_configured_simple_model_and_skips_faiss():
    decision = await route_model(
        RoutingInput("查询某场地本月能耗"),
        app_config=_app_config(),
        searcher=_FailIfCalledSearcher(),
    )

    assert decision is not None
    assert decision.route_type == "simple"
    assert decision.model_name == "simple-model"
    assert decision.source == "rules"
    assert decision.decision_latency_ms is not None
    assert decision.decision_latency_ms >= 0


@pytest.mark.asyncio
async def test_route_model_uses_faiss_for_undecided_questions():
    searcher = _FakeSearcher(
        FaissSearchResult(
            "simple",
            confidence=0.83,
            reason_codes=("faiss_similarity_vote",),
            signals=RoutingSignals(risk_level="read", difficulty_level="low"),
            index_version="idx-1",
        )
    )

    decision = await route_model(
        RoutingInput("帮我看看这个数据"),
        app_config=_app_config(),
        searcher=searcher,
    )

    assert searcher.questions == ["帮我看看这个数据"]
    assert decision is not None
    assert decision.route_type == "simple"
    assert decision.model_name == "simple-model"
    assert decision.source == "faiss"
    assert decision.index_version == "idx-1"
    assert decision.decision_latency_ms is not None


@pytest.mark.asyncio
async def test_route_model_falls_back_to_complex_when_faiss_fails():
    decision = await route_model(
        RoutingInput("帮我看看这个数据"),
        app_config=_app_config(),
        searcher=_FakeSearcher(error=FaissSearchError("index unavailable")),
    )

    assert decision is not None
    assert decision.route_type == "complex"
    assert decision.model_name == "complex-model"
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_enforce_mode_reports_faiss_asset_configuration_errors():
    with pytest.raises(ModelRoutingConfigError, match="Invalid model_routing FAISS assets"):
        await route_model(
            RoutingInput("帮我看看这个数据"),
            app_config=_app_config(),
            searcher=_FakeSearcher(error=FaissConfigurationError("manifest missing")),
        )


@pytest.mark.asyncio
async def test_shadow_mode_falls_back_when_faiss_assets_are_invalid():
    decision = await route_model(
        RoutingInput("帮我看看这个数据"),
        app_config=_app_config(mode="shadow"),
        searcher=_FakeSearcher(error=FaissConfigurationError("manifest missing")),
    )

    assert decision is not None
    assert decision.route_type == "complex"
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_real_faiss_index_writes_and_validates_versioned_manifest(tmp_path):
    pytest.importorskip("faiss")
    examples_path = tmp_path / "examples.jsonl"
    index_path = tmp_path / "routes.faiss"
    metadata_path = tmp_path / "routes.meta.json"
    examples = [
        {
            "text": "查询某场地本月能耗",
            "route_type": "simple",
            "label_version": "test-1",
            "signals": {"risk_level": "read", "difficulty_level": "low"},
        },
        {
            "text": "修改场地显示名称",
            "route_type": "complex",
            "label_version": "test-1",
            "signals": {"risk_level": "high", "difficulty_level": "medium"},
        },
    ]
    examples_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in examples) + "\n", encoding="utf-8")
    config = FaissRoutingConfig(
        index_path=str(index_path),
        examples_path=str(examples_path),
        metadata_path=str(metadata_path),
        embedding_dimension=64,
        top_k=1,
        min_votes=1,
        similarity_threshold=0.9,
        label_version="test-1",
        index_version="idx-test-1",
    )

    build_faiss_index(config)
    manifest = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert manifest["index_version"] == "idx-test-1"
    assert manifest["label_version"] == "test-1"
    assert manifest["example_count"] == 2

    result = await FaissSearcher(config).search("查询某场地本月能耗")
    assert result is not None
    assert result.route_type == "simple"

    manifest["index_version"] = "stale-index"
    metadata_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FaissConfigurationError, match="metadata mismatch for index_version"):
        await FaissSearcher(config).search("查询某场地本月能耗")


@pytest.mark.asyncio
async def test_route_model_rejects_missing_configured_model():
    with pytest.raises(ModelRoutingConfigError, match="simple_model"):
        await route_model(
            RoutingInput("查询某场地本月能耗"),
            app_config=_app_config(simple_model="missing-model"),
        )


@pytest.mark.asyncio
async def test_disabled_mode_preserves_existing_model_behavior():
    decision = await route_model(
        RoutingInput("修改场地显示名称"),
        app_config=_app_config(mode="disabled"),
        searcher=_FailIfCalledSearcher(),
    )

    assert decision is None


def test_build_routing_input_keeps_only_latest_user_text():
    result = build_routing_input(
        {
            "messages": [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": [{"type": "text", "text": "new question"}]},
            ]
        },
        endpoint="saas-query",
    )

    assert result.question == "new question"
    assert result.endpoint == "saas-query"


def test_hashing_embedding_provider_is_deterministic_and_normalized():
    provider = HashingEmbeddingProvider(dimension=64, model_name="test-v1")
    first = provider.encode(["查询本月能耗", "修改场地名称"])
    second = provider.encode(["查询本月能耗", "修改场地名称"])

    assert first.shape == (2, 64)
    assert (first == second).all()


def test_faiss_searcher_cache_rotates_without_retaining_old_versions():
    first = get_faiss_searcher(FaissRoutingConfig(index_version="cache-1"))
    second = get_faiss_searcher(FaissRoutingConfig(index_version="cache-2"))

    assert first is not second
    assert second.config.index_version == "cache-2"


@pytest.mark.asyncio
async def test_start_run_enforce_overrides_client_model_before_agent_execution():
    from types import SimpleNamespace
    from unittest.mock import patch

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore

    from app.gateway.routers.thread_runs import RunCreateRequest
    from app.gateway.services import start_run
    from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
    from deerflow.runtime import RunManager
    from deerflow.runtime.runs.store.memory import MemoryRunStore

    app_config = _app_config()
    set_app_config(app_config)
    try:
        run_manager = RunManager(store=MemoryRunStore())
        state = SimpleNamespace(
            stream_bridge=SimpleNamespace(),
            run_manager=run_manager,
            checkpointer=InMemorySaver(),
            store=InMemoryStore(),
            run_event_store=SimpleNamespace(),
            run_events_config=None,
            thread_store=MemoryThreadMetaStore(InMemoryStore()),
        )
        request = SimpleNamespace(
            headers={},
            state=SimpleNamespace(),
            app=SimpleNamespace(state=state),
        )
        body = RunCreateRequest(
            input={"messages": [{"role": "user", "content": "查询某场地本月能耗"}]},
            config={"configurable": {"model_name": "client-selected-model"}},
            context={"model_name": "client-selected-model"},
        )
        captured: dict[str, object] = {}

        async def fake_run_agent(*_args, **kwargs):
            captured["config"] = kwargs["config"]

        with (
            patch("app.gateway.services.resolve_agent_factory", return_value=object()),
            patch("app.gateway.services.run_agent", side_effect=fake_run_agent),
        ):
            record = await start_run(body, "routing-thread", request)
            await record.task

        config = captured["config"]
        assert config["configurable"]["model_name"] == "simple-model"
        assert config["context"]["model_name"] == "simple-model"
        assert record.model_name == "simple-model"
        assert record.metadata["model_routing"]["route_type"] == "simple"
    finally:
        reset_app_config()
