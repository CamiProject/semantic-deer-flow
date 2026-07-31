"""Deterministic SQL question-answering with cross-validation."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from deerflow.agents.thread_state import ThreadState
from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.models import create_chat_model
from deerflow.runtime.secret_context import SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY
from deerflow.subagents import get_subagent_config
from deerflow.subagents.executor import SubagentExecutor, SubagentResult, SubagentStatus
from deerflow.tools.builtins.sql_tools import SQL_TOOLS

logger = logging.getLogger(__name__)

_PRIMARY_AGENT = "mysql-query"
_VERIFIER_AGENT = "mysql-validator"
_RUN_PROFILE = "sql-cross-validation"


@dataclass(frozen=True)
class _SqlAgentRun:
    role: str
    subagent_type: str
    result: SubagentResult


def _runtime_config(config: RunnableConfig | dict | None) -> dict[str, Any]:
    if not config:
        return {}
    configurable = config.get("configurable", {}) or {}
    cfg = {key: value for key, value in configurable.items() if key not in {"__pregel_runtime", "context"}} if isinstance(configurable, Mapping) else {}
    configurable_context = configurable.get("context") if isinstance(configurable, Mapping) else None
    if isinstance(configurable_context, Mapping):
        cfg.update(configurable_context)
    context = config.get("context", {}) or {}
    if isinstance(context, Mapping):
        cfg.update(context)
    parent_runtime = configurable.get("__pregel_runtime") if isinstance(configurable, Mapping) else None
    runtime_context = getattr(parent_runtime, "context", None)
    if isinstance(runtime_context, Mapping):
        cfg.update(runtime_context)
    metadata = config.get("metadata", {}) or {}
    if isinstance(metadata, dict):
        for key in ("model_name",):
            if key in metadata and key not in cfg:
                cfg[key] = metadata[key]
    return cfg


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(message, dict):
        raw = message.get("content")
        if isinstance(raw, str):
            return raw
    return str(content or "")


def _extract_question(state: dict[str, Any]) -> str:
    messages = state.get("messages") or []
    for message in reversed(messages):
        msg_type = getattr(message, "type", None)
        if msg_type is None and isinstance(message, dict):
            msg_type = message.get("type") or message.get("role")
        if msg_type in {"human", "user"}:
            text = _message_text(message).strip()
            if text:
                return text
    return ""


def _format_agent_result(run: _SqlAgentRun) -> str:
    result = run.result
    if result.status == SubagentStatus.COMPLETED:
        body = result.result or "No result returned."
    else:
        body = result.error or result.result or "No result returned."
    return f"### {run.role} ({run.subagent_type})\nStatus: {result.status.value}\n\n{body}".strip()


def _classify(primary: SubagentResult, verifier: SubagentResult) -> str:
    if primary.status == SubagentStatus.COMPLETED and verifier.status == SubagentStatus.COMPLETED:
        return "两个子 Agent 均已返回结果；自动汇总模型不可用时，无法可靠判定两侧结果是否一致。"
    if primary.status == SubagentStatus.COMPLETED or verifier.status == SubagentStatus.COMPLETED:
        return "未完成完整验证：只有一个子 Agent 成功返回。请展示成功结果，并明确说明另一侧失败原因。"
    return "未完成完整验证：两个子 Agent 都未成功返回业务结果。请展示失败原因，不要编造查询结果。"


def _final_answer(question: str, primary: _SqlAgentRun, verifier: _SqlAgentRun) -> str:
    return f"""# SQL 问数交叉验证结果

用户问题：{question or "未能识别用户问题"}

{_classify(primary.result, verifier.result)}

{_format_agent_result(primary)}

{_format_agent_result(verifier)}
"""


def _summary_prompt(question: str, primary: _SqlAgentRun, verifier: _SqlAgentRun) -> str:
    return f"""你是 SQL 问数交叉验证的最终汇总器。你只能基于下面两个子 Agent 的最终输出做比对和总结，不要生成或执行新的 SQL。

角色约定：
- subAgent 名称表达能力域，不表达它在本次 run 中的最终运行角色。
- `mysql-query` 在本 profile 中是 primary execution / 主查询角色。
- `mysql-validator` 在本 profile 中是 domain validation / 领域验证角色。

用户问题：
{question or "未能识别用户问题"}

{_format_agent_result(primary)}

{_format_agent_result(verifier)}

汇总规则：
1. 如果两侧都成功，并且 SQL 口径、关键筛选条件、数值结果或业务结论一致，输出“已通过交叉验证”，然后给出最终答案、主 SQL、验证 SQL、结果摘要。
2. 如果两侧都成功但结果或口径不一致，输出“交叉验证不一致”，展示主结果、验证结果、差异点，并将结论标注为低置信度。
3. 如果只有一侧成功，输出“未完成完整验证”，展示成功侧结果和失败侧原因。
4. 如果两侧都失败，输出“未完成完整验证”，展示失败原因，不要编造查询结果。
5. 不要泄露数据库连接串、密码、内部 token 或租户认证信息。
"""


async def _summarize_final_answer(
    *,
    question: str,
    primary: _SqlAgentRun,
    verifier: _SqlAgentRun,
    model_name: str | None,
    app_config: AppConfig,
    config: RunnableConfig | dict | None,
) -> str:
    fallback = _final_answer(question, primary, verifier)
    try:
        model = create_chat_model(name=model_name, thinking_enabled=False, app_config=app_config, attach_tracing=False)
        runnable_config: dict[str, Any] = {
            "run_name": "sql_cross_validation_summary",
            "tags": ["sql-cross-validation:summary"],
        }
        if isinstance(config, dict):
            callbacks = config.get("callbacks")
            if callbacks:
                runnable_config["callbacks"] = callbacks
            metadata = config.get("metadata")
            if isinstance(metadata, dict):
                runnable_config["metadata"] = metadata
        response = await model.ainvoke([HumanMessage(content=_summary_prompt(question, primary, verifier))], config=runnable_config)
        content = _message_text(response).strip()
        return content or fallback
    except Exception:
        logger.warning("SQL cross-validation summary model failed; returning fallback answer", exc_info=True)
        return fallback


def _primary_prompt(question: str) -> str:
    return f"""你正在 sql-cross-validation run profile 中执行 primary execution / 主查询角色。
你的底层 subAgent 类型是 mysql-query；该名称表示 MySQL 查询能力域，不表示你可以使用非 SQL 工具。
在本 profile 中，你被限制为 SQL 主查询 Agent。请独立完成用户问题，不要依赖任何验证 Agent。

用户问题：
{question}

要求：
1. 只使用 SQL 只读工具完成查询。
2. 必要时先用 sql_show_databases / sql_list_tables / sql_schema 探索库表。
3. 复杂 SQL 先用 sql_query_checker 校验，再用 sql_query 执行。
4. 返回必须包含：使用的 SQL、关键筛选条件、结果表或原始结果、简短业务结论。
5. 不要泄露数据库连接串、密码、内部 token 或租户认证信息。
6. 如果无法完成，返回已经确认的信息和明确失败原因。
"""


def _verifier_prompt(question: str) -> str:
    return f"""你正在 sql-cross-validation run profile 中执行 domain validation / 领域验证角色。
你的底层 subAgent 类型是 mysql-validator；该名称表示 MySQL 验证能力域，不表示普通主查询角色。
在本 profile 中，你被指定为 SQL 交叉验证 Agent。请独立理解并验证用户问题，不要假设主查询 Agent 的结论正确。

用户问题：
{question}

要求：
1. 独立选择库表、独立生成 SQL、独立执行查询。
2. 只使用 SQL 只读工具；不要请求切换租户或数据库权限。
3. 必要时先用 sql_show_databases / sql_list_tables / sql_schema 探索库表。
4. 复杂 SQL 先用 sql_query_checker 校验，再用 sql_query 执行。
5. 返回必须包含：验证 SQL、关键筛选条件、验证结果、你认为结果是否可用于交叉验证。
6. 不要泄露数据库连接串、密码、内部 token 或租户认证信息。
7. 如果无法完成，返回明确失败原因。
"""


def _publish(writer: Any, event: dict[str, Any]) -> None:
    if writer is None:
        return
    try:
        writer(event)
    except Exception:
        logger.debug("Failed to publish SQL cross-validation progress", exc_info=True)


async def _run_subagent(
    *,
    role: str,
    subagent_type: str,
    prompt: str,
    model_name: str | None,
    thread_id: str | None,
    trace_id: str | None,
    runtime_context: dict[str, Any],
    app_config: AppConfig,
) -> _SqlAgentRun:
    config = get_subagent_config(subagent_type, app_config=app_config)
    if config is None:
        raise RuntimeError(f"Unknown subagent type {subagent_type!r}")
    executor = SubagentExecutor(
        config=config,
        tools=list(SQL_TOOLS),
        app_config=app_config,
        parent_model=model_name,
        thread_id=thread_id,
        trace_id=trace_id,
        runtime_context=runtime_context,
    )
    result = await executor._aexecute(prompt)
    return _SqlAgentRun(role=role, subagent_type=subagent_type, result=result)


def _report_usage(runtime_context: dict[str, Any], runs: list[_SqlAgentRun]) -> None:
    journal = runtime_context.get("__run_journal")
    if journal is None or not hasattr(journal, "record_external_llm_usage_records"):
        return
    records: list[dict[str, int | str]] = []
    for run in runs:
        records.extend(run.result.token_usage_records or [])
    if records:
        journal.record_external_llm_usage_records(records)


async def _cross_validate_node(state: ThreadState, config: RunnableConfig) -> dict[str, Any]:
    runtime = _runtime_config(config)
    app_config = runtime.get("app_config") or get_app_config()
    thread_id = runtime.get("thread_id")
    run_id = runtime.get("run_id")
    model_name = runtime.get("model_name")
    trace_id = str(run_id)[:8] if run_id else None
    question = _extract_question(state)
    try:
        writer = get_stream_writer()
    except Exception:
        writer = None

    runtime_context = {
        key: value
        for key, value in runtime.items()
        if key
        not in {
            "configurable",
            "callbacks",
            SAAS_AUTHORIZATION_TOKEN_CONTEXT_KEY,
        }
    }
    runtime_context["app_config"] = app_config

    _publish(
        writer,
        {
            "type": "sql_cross_validation_started",
            "roles": [_PRIMARY_AGENT, _VERIFIER_AGENT],
            "description": "SQL cross-validation started",
        },
    )

    primary_task = asyncio.create_task(
        _run_subagent(
            role="主查询",
            subagent_type=_PRIMARY_AGENT,
            prompt=_primary_prompt(question),
            model_name=model_name,
            thread_id=thread_id,
            trace_id=trace_id,
            runtime_context=runtime_context,
            app_config=app_config,
        )
    )
    verifier_task = asyncio.create_task(
        _run_subagent(
            role="交叉验证",
            subagent_type=_VERIFIER_AGENT,
            prompt=_verifier_prompt(question),
            model_name=model_name,
            thread_id=thread_id,
            trace_id=trace_id,
            runtime_context=runtime_context,
            app_config=app_config,
        )
    )

    raw_results = await asyncio.gather(primary_task, verifier_task, return_exceptions=True)
    runs: list[_SqlAgentRun] = []
    for role, subagent_type, raw in (
        ("主查询", _PRIMARY_AGENT, raw_results[0]),
        ("交叉验证", _VERIFIER_AGENT, raw_results[1]),
    ):
        if isinstance(raw, Exception):
            result = SubagentResult(
                task_id=f"{_RUN_PROFILE}-{subagent_type}",
                trace_id=trace_id or "",
                status=SubagentStatus.FAILED,
                error=str(raw),
            )
            runs.append(_SqlAgentRun(role=role, subagent_type=subagent_type, result=result))
        else:
            runs.append(raw)

    _report_usage(runtime_context, runs)

    for run in runs:
        _publish(
            writer,
            {
                "type": "sql_cross_validation_subagent_completed",
                "role": run.role,
                "subagent_type": run.subagent_type,
                "status": run.result.status.value,
            },
        )

    content = await _summarize_final_answer(
        question=question,
        primary=runs[0],
        verifier=runs[1],
        model_name=model_name,
        app_config=app_config,
        config=config,
    )
    return {"messages": [AIMessage(content=content)]}


def make_sql_cross_validation_agent(config: RunnableConfig, *, app_config: AppConfig | None = None):
    """Build a graph that deterministically runs two SQL subagents."""
    if app_config is None:
        app_config = get_app_config()
    if "metadata" not in config:
        config["metadata"] = {}
    config["metadata"].update(
        {
            "agent_name": _RUN_PROFILE,
            "run_profile": _RUN_PROFILE,
            "subagent_enabled": True,
            "max_concurrent_subagents": 2,
        }
    )

    builder = StateGraph(ThreadState)
    node = _cross_validate_node
    if "config" not in inspect.signature(node).parameters:
        raise RuntimeError("SQL cross-validation node must accept RunnableConfig")
    builder.add_node("sql_cross_validate", node)
    builder.add_edge(START, "sql_cross_validate")
    builder.add_edge("sql_cross_validate", END)
    return builder.compile()
