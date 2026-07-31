from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from deerflow.subagents.config import SubagentConfig


class FakeSubagentStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class FakeSubagentResult:
    def __init__(
        self,
        *,
        task_id: str,
        trace_id: str,
        status: str,
        result: str | None = None,
        error: str | None = None,
        token_usage_records: list[dict] | None = None,
    ):
        self.task_id = task_id
        self.trace_id = trace_id
        self.status = SimpleNamespace(value=status)
        self.result = result
        self.error = error
        self.token_usage_records = token_usage_records or []


def _app_config() -> SimpleNamespace:
    return SimpleNamespace(
        subagents=SimpleNamespace(
            agents={},
            custom_agents={},
            timeout_seconds=300,
            max_turns=None,
            get_model_for=lambda _name: None,
            get_skills_for=lambda _name: None,
        ),
        tool_search=SimpleNamespace(enabled=False),
    )


def _config(name: str) -> SubagentConfig:
    return SubagentConfig(
        name=name,
        description=f"{name} test config",
        system_prompt="system",
        tools=None,
        model="inherit",
        max_turns=5,
        timeout_seconds=30,
    )


@pytest.fixture()
def sql_module(monkeypatch):
    from deerflow.agents.sql_cross_validation import agent as module

    monkeypatch.setattr(module, "SubagentResult", FakeSubagentResult)
    monkeypatch.setattr(module, "SubagentStatus", FakeSubagentStatus)
    return module


@pytest.mark.asyncio
async def test_sql_cross_validation_graph_injects_runnable_config_into_node(monkeypatch, sql_module):
    module = sql_module
    captured = []

    async def run_subagent(**kwargs):
        captured.append(kwargs["runtime_context"])
        return module._SqlAgentRun(
            role=kwargs["role"],
            subagent_type=kwargs["subagent_type"],
            result=FakeSubagentResult(
                task_id=kwargs["subagent_type"],
                trace_id="trace",
                status=FakeSubagentStatus.COMPLETED,
                result="ok",
            ),
        )

    async def summarize(**_kwargs):
        return "ok"

    runtime_context = {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "tenant_code": "tenant-safe",
        "app_config": _app_config(),
    }
    config = {
        "context": runtime_context,
        "configurable": {
            "thread_id": "thread-1",
            "__pregel_runtime": Runtime(context=runtime_context, store=None),
        },
    }
    monkeypatch.setattr(module, "_run_subagent", run_subagent)
    monkeypatch.setattr(module, "_summarize_final_answer", summarize)

    graph = module.make_sql_cross_validation_agent(config, app_config=_app_config())
    await graph.ainvoke({"messages": [HumanMessage(content="query data")]}, config=config)

    assert len(captured) == 2
    assert all(context["run_id"] == "run-1" for context in captured)
    assert all(context["thread_id"] == "thread-1" for context in captured)
    assert all(context["tenant_code"] == "tenant-safe" for context in captured)
    assert all("__pregel_runtime" not in context for context in captured)
    assert all("context" not in context for context in captured)


@pytest.mark.asyncio
async def test_sql_cross_validation_runs_exactly_two_subagents(monkeypatch, sql_module):
    module = sql_module
    calls = []

    class DummyExecutor:
        def __init__(self, **kwargs):
            calls.append({"kwargs": kwargs})
            self.config = kwargs["config"]

        async def _aexecute(self, prompt):
            calls[-1]["prompt"] = prompt
            await asyncio.sleep(0)
            return FakeSubagentResult(
                task_id=f"task-{self.config.name}",
                trace_id="trace",
                status=FakeSubagentStatus.COMPLETED,
                result=f"{self.config.name} result",
                token_usage_records=[
                    {
                        "source_run_id": f"usage-{self.config.name}",
                        "caller": f"subagent:{self.config.name}",
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "total_tokens": 3,
                    }
                ],
            )

    journal = SimpleNamespace(records=[])
    journal.record_external_llm_usage_records = lambda records: journal.records.extend(records)

    monkeypatch.setattr(module, "SubagentExecutor", DummyExecutor)
    monkeypatch.setattr(module, "get_subagent_config", lambda name, *, app_config: _config(name))

    async def fake_summarize_final_answer(**kwargs):
        return module._final_answer(kwargs["question"], kwargs["primary"], kwargs["verifier"])

    monkeypatch.setattr(module, "_summarize_final_answer", fake_summarize_final_answer)

    state = {"messages": [HumanMessage(content="查询本月用电量")]}
    config = {
        "context": {
            "thread_id": "thread-1",
            "run_id": "run-abcdef",
            "tenant_code": "tenant-safe",
            "system_code": "efficiency",
            "app_config": _app_config(),
            "__run_journal": journal,
        },
        "metadata": {"model_name": "model-a"},
    }

    result = await module._cross_validate_node(state, config)

    assert len(calls) == 2
    assert [call["kwargs"]["config"].name for call in calls] == ["mysql-query", "mysql-validator"]
    assert all([tool.name for tool in call["kwargs"]["tools"]] == [tool.name for tool in module.SQL_TOOLS] for call in calls)
    assert all(call["kwargs"]["parent_model"] == "model-a" for call in calls)
    assert all(call["kwargs"]["thread_id"] == "thread-1" for call in calls)
    assert all(call["kwargs"]["runtime_context"]["tenant_code"] == "tenant-safe" for call in calls)
    assert all(call["kwargs"]["runtime_context"]["system_code"] == "efficiency" for call in calls)
    assert "查询本月用电量" in calls[0]["prompt"]
    assert "查询本月用电量" in calls[1]["prompt"]
    assert "独立" in calls[1]["prompt"]
    assert len(journal.records) == 2
    final = result["messages"][0].content
    assert "SQL 问数交叉验证结果" in final
    assert "mysql-query result" in final
    assert "mysql-validator result" in final


def test_sql_cross_validation_final_answer_marks_single_side_failure(sql_module):
    _SqlAgentRun = sql_module._SqlAgentRun
    _final_answer = sql_module._final_answer

    primary = _SqlAgentRun(
        role="主查询",
        subagent_type="mysql-query",
        result=FakeSubagentResult(
            task_id="p",
            trace_id="t",
            status=FakeSubagentStatus.COMPLETED,
            result="primary ok",
        ),
    )
    verifier = _SqlAgentRun(
        role="交叉验证",
        subagent_type="mysql-validator",
        result=FakeSubagentResult(
            task_id="v",
            trace_id="t",
            status=FakeSubagentStatus.FAILED,
            error="validation failed",
        ),
    )

    answer = _final_answer("问题", primary, verifier)

    assert "未完成完整验证" in answer
    assert "primary ok" in answer
    assert "validation failed" in answer


def test_sql_cross_validation_final_answer_marks_double_failure(sql_module):
    _SqlAgentRun = sql_module._SqlAgentRun
    _final_answer = sql_module._final_answer

    primary = _SqlAgentRun(
        role="主查询",
        subagent_type="mysql-query",
        result=FakeSubagentResult(task_id="p", trace_id="t", status=FakeSubagentStatus.FAILED, error="primary failed"),
    )
    verifier = _SqlAgentRun(
        role="交叉验证",
        subagent_type="mysql-validator",
        result=FakeSubagentResult(task_id="v", trace_id="t", status=FakeSubagentStatus.FAILED, error="verifier failed"),
    )

    answer = _final_answer("问题", primary, verifier)

    assert "两个子 Agent 都未成功" in answer
    assert "primary failed" in answer
    assert "verifier failed" in answer


@pytest.mark.asyncio
async def test_sql_cross_validation_uses_summary_model_when_available(monkeypatch, sql_module):
    module = sql_module
    prompts = []

    class DummyModel:
        async def ainvoke(self, messages, config=None):
            prompts.append(messages[0].content)
            return SimpleNamespace(content="已通过交叉验证\n最终答案：ok")

    primary = module._SqlAgentRun(
        role="主查询",
        subagent_type="mysql-query",
        result=FakeSubagentResult(task_id="p", trace_id="t", status=FakeSubagentStatus.COMPLETED, result="primary sql result"),
    )
    verifier = module._SqlAgentRun(
        role="交叉验证",
        subagent_type="mysql-validator",
        result=FakeSubagentResult(task_id="v", trace_id="t", status=FakeSubagentStatus.COMPLETED, result="verifier sql result"),
    )
    monkeypatch.setattr(module, "create_chat_model", lambda **_kwargs: DummyModel())

    answer = await module._summarize_final_answer(
        question="查询本月用电量",
        primary=primary,
        verifier=verifier,
        model_name="model-a",
        app_config=_app_config(),
        config={"metadata": {"run_profile": "sql-cross-validation"}},
    )

    assert answer == "已通过交叉验证\n最终答案：ok"
    assert "不要生成或执行新的 SQL" in prompts[0]
    assert "primary sql result" in prompts[0]
    assert "verifier sql result" in prompts[0]
