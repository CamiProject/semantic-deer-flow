from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from deerflow.runtime.secret_context import SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY
from deerflow.semantic.client import SemanticClientError
from deerflow.subagents.config import SubagentConfig


class FakeStatus:
    COMPLETED = "completed"
    FAILED = "failed"


class FakeResult:
    def __init__(self, *, status="completed", result=None, error=None, ai_messages=None):
        self.status = SimpleNamespace(value=status)
        self.result = result
        self.error = error
        self.token_usage_records = []
        self.ai_messages = ai_messages or []


def _app_config():
    return SimpleNamespace(
        tool_search=SimpleNamespace(enabled=False),
        subagents=SimpleNamespace(
            agents={},
            custom_agents={},
            timeout_seconds=300,
            max_turns=None,
            get_model_for=lambda _name: None,
            get_skills_for=lambda _name: None,
        ),
    )


def _config(name):
    return SubagentConfig(
        name=name,
        description=name,
        system_prompt="base",
        tools=[],
        model="inherit",
        max_turns=5,
        timeout_seconds=30,
    )


@pytest.mark.parametrize(
    "legacy_context",
    [None, {"run_id": "stale-run", "thread_id": "stale-thread"}],
)
def test_saas_query_runtime_config_reads_authoritative_pregel_context(legacy_context):
    from deerflow.agents.saas_query import agent as module

    runtime_context = {
        "run_id": "run-1",
        "thread_id": "thread-1",
        SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY: "signed-user-context",
    }
    config = {
        "configurable": {
            "thread_id": "spoofed-thread",
            "__pregel_runtime": Runtime(context=runtime_context, store=None),
        },
    }
    if legacy_context is not None:
        config["context"] = legacy_context

    resolved = module._runtime_config(config)

    assert resolved["run_id"] == "run-1"
    assert resolved["thread_id"] == "thread-1"
    assert resolved[SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY] == "signed-user-context"
    assert "__pregel_runtime" not in resolved
    assert "context" not in resolved


@pytest.mark.asyncio
async def test_saas_query_graph_injects_runnable_config_into_node(monkeypatch):
    from deerflow.agents.saas_query import agent as module

    captured = {}

    async def coverage(_question, runtime):
        captured.update(runtime)
        return {"objects": [], "metrics": [], "actions": []}

    runtime_context = {
        "run_id": "run-1",
        "thread_id": "thread-1",
        SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY: "signed-user-context",
        "app_config": _app_config(),
    }
    config = {
        "context": runtime_context,
        "configurable": {
            "thread_id": "thread-1",
            "__pregel_runtime": Runtime(context=runtime_context, store=None),
        },
    }
    monkeypatch.setattr(module, "_resolve_semantic_coverage", coverage)
    monkeypatch.setenv("DEER_FLOW_SAAS_QUERY_SQL_FALLBACK_MODE", "disabled")

    graph = module.make_saas_query_agent(config, app_config=_app_config())
    await graph.ainvoke({"messages": [HumanMessage(content="query sites")]}, config=config)

    assert captured["run_id"] == "run-1"
    assert captured["thread_id"] == "thread-1"
    assert captured[SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY] == "signed-user-context"


@pytest.mark.asyncio
async def test_saas_query_covered_question_uses_only_semantic_tools(monkeypatch):
    from deerflow.agents.saas_query import agent as module

    calls = []
    recorded_tool_messages = []

    class Journal:
        def record_external_tool_messages(self, messages, *, caller):
            recorded_tool_messages.append((messages, caller))

    class DummyExecutor:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.config = kwargs["config"]

        async def _aexecute(self, prompt):
            calls[-1]["prompt"] = prompt
            return FakeResult(
                result=f"{self.config.name} semantic result",
                ai_messages=[{"type": "tool", "name": "query_metrics"}],
            )

    async def coverage(_question, _runtime):
        return {
            "ontology_version": "1",
            "objects": [{"id": "Site"}],
            "metrics": [{"id": "site.count"}],
            "actions": [],
            "authorization_scope_hash": "scope-1",
        }

    async def summary(**_kwargs):
        return "semantic final"

    async def no_fallback(*_args, **_kwargs):
        raise AssertionError("covered questions must not use SQL fallback")

    monkeypatch.setattr(module, "SubagentExecutor", DummyExecutor)
    monkeypatch.setattr(module, "SubagentStatus", FakeStatus)
    monkeypatch.setattr(module, "get_subagent_config", lambda name, *, app_config: _config(name))
    monkeypatch.setattr(module, "_resolve_semantic_coverage", coverage)
    monkeypatch.setattr(module, "_summarize", summary)
    monkeypatch.setattr(module, "_run_sql_fallback", no_fallback)
    monkeypatch.setenv("DEER_FLOW_SAAS_QUERY_SHADOW_SQL", "false")

    result = await module._saas_query_node(
        {"messages": [HumanMessage(content="查询场地数量")]},
        {
            "context": {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "app_config": _app_config(),
                "__run_journal": Journal(),
            }
        },
    )

    assert result["messages"][0].content == "semantic final"
    assert [call["config"].name for call in calls] == ["mysql-query", "mysql-validator"]
    assert {tool.name for tool in calls[0]["tools"]} == {tool.name for tool in module.SEMANTIC_READ_TOOLS}
    assert {tool.name for tool in calls[1]["tools"]} == {tool.name for tool in module.SEMANTIC_VALIDATION_TOOLS}
    assert all(call["config"].skills == [] for call in calls)
    assert [caller for _, caller in recorded_tool_messages] == [
        "subagent:mysql-query",
        "subagent:mysql-validator",
    ]
    assert "不允许生成或执行 SQL" in calls[0]["prompt"]
    assert "不生成第二条 SQL" in calls[1]["prompt"]


def test_saas_query_exposes_action_tools_only_for_authorized_action_coverage():
    from deerflow.agents.saas_query import agent as module

    read_names = {tool.name for tool in module._primary_tools({"metrics": [{"id": "site.count"}]})}
    action_names = {tool.name for tool in module._primary_tools({"actions": [{"id": "site.update_display_name"}]})}

    assert read_names == {tool.name for tool in module.SEMANTIC_READ_TOOLS}
    assert action_names == {tool.name for tool in module.SEMANTIC_TOOLS}


@pytest.mark.asyncio
async def test_saas_query_uncovered_question_uses_scoped_sql_fallback(monkeypatch):
    from deerflow.agents.saas_query import agent as module

    async def coverage(_question, _runtime):
        return {"objects": [], "metrics": [], "actions": []}

    async def fallback(_state, _config):
        return {"messages": [AIMessage(content="scoped SQL fallback result")]}

    monkeypatch.setattr(module, "_resolve_semantic_coverage", coverage)
    monkeypatch.setattr(module, "_run_sql_fallback", fallback)
    monkeypatch.setenv("DEER_FLOW_SAAS_QUERY_SQL_FALLBACK_MODE", "scoped")

    result = await module._saas_query_node(
        {"messages": [HumanMessage(content="未建模问题")]},
        {
            "context": {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "app_config": _app_config(),
            }
        },
    )

    assert result["messages"][0].content == "scoped SQL fallback result"


@pytest.mark.asyncio
async def test_saas_query_semantic_outage_fails_closed_without_sql(monkeypatch):
    from deerflow.agents.saas_query import agent as module

    async def unavailable(_question, _runtime):
        raise SemanticClientError("POLICY_UNAVAILABLE", "offline")

    async def no_fallback(*_args, **_kwargs):
        raise AssertionError("outage must not downgrade to SQL")

    monkeypatch.setattr(module, "_resolve_semantic_coverage", unavailable)
    monkeypatch.setattr(module, "_run_sql_fallback", no_fallback)

    result = await module._saas_query_node(
        {"messages": [HumanMessage(content="查询场地数量")]},
        {
            "context": {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "app_config": _app_config(),
            }
        },
    )

    assert "fail-closed" in result["messages"][0].content
    assert "未降级到自由 SQL" in result["messages"][0].content


@pytest.mark.asyncio
async def test_saas_query_can_disable_uncovered_sql_fallback(monkeypatch):
    from deerflow.agents.saas_query import agent as module

    async def coverage(_question, _runtime):
        return {"objects": [], "metrics": [], "actions": []}

    monkeypatch.setattr(module, "_resolve_semantic_coverage", coverage)
    monkeypatch.setenv("DEER_FLOW_SAAS_QUERY_SQL_FALLBACK_MODE", "disabled")

    result = await module._saas_query_node(
        {"messages": [HumanMessage(content="未建模问题")]},
        {
            "context": {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "app_config": _app_config(),
            }
        },
    )

    assert "scoped SQL fallback 已关闭" in result["messages"][0].content
