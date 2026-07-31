"""Semantic-first SaaS business query and Action run profile."""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from deerflow.agents.sql_cross_validation.agent import _cross_validate_node
from deerflow.agents.thread_state import ThreadState
from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.models import create_chat_model
from deerflow.semantic.client import SemanticClientError, SemanticPlatformClient
from deerflow.subagents import get_subagent_config
from deerflow.subagents.executor import SubagentExecutor, SubagentResult, SubagentStatus
from deerflow.tools.builtins.semantic_tools import (
    SEMANTIC_READ_TOOLS,
    SEMANTIC_TOOLS,
    SEMANTIC_VALIDATION_TOOLS,
)

logger = logging.getLogger(__name__)

_PRIMARY_AGENT = "mysql-query"
_VALIDATOR_AGENT = "mysql-validator"
_RUN_PROFILE = "saas-query"
_SQL_FALLBACK_MODE_ENV = "DEER_FLOW_SAAS_QUERY_SQL_FALLBACK_MODE"
_SQL_FALLBACK_ROLES_ENV = "DEER_FLOW_SAAS_QUERY_SQL_FALLBACK_ROLES"
_SHADOW_SQL_ENV = "DEER_FLOW_SAAS_QUERY_SHADOW_SQL"


@dataclass(frozen=True)
class _SemanticAgentRun:
    role: str
    subagent_type: str
    result: SubagentResult


def _runtime_config(config: RunnableConfig | dict | None) -> dict[str, Any]:
    if not config:
        return {}
    configurable = config.get("configurable", {}) or {}
    resolved = {key: value for key, value in configurable.items() if key not in {"__pregel_runtime", "context"}} if isinstance(configurable, Mapping) else {}
    configurable_context = configurable.get("context") if isinstance(configurable, Mapping) else None
    if isinstance(configurable_context, Mapping):
        resolved.update(configurable_context)
    context = config.get("context", {}) or {}
    if isinstance(context, Mapping):
        resolved.update(context)
    parent_runtime = configurable.get("__pregel_runtime") if isinstance(configurable, Mapping) else None
    runtime_context = getattr(parent_runtime, "context", None)
    if isinstance(runtime_context, Mapping):
        resolved.update(runtime_context)
    metadata = config.get("metadata", {}) or {}
    if isinstance(metadata, dict) and "model_name" in metadata and "model_name" not in resolved:
        resolved["model_name"] = metadata["model_name"]
    return resolved


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block if isinstance(block, str) else str(block.get("text") or block.get("content") or "") for block in content if isinstance(block, (str, dict)))
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return str(content or "")


def _extract_question(state: dict[str, Any]) -> str:
    for message in reversed(state.get("messages") or []):
        msg_type = getattr(message, "type", None)
        if msg_type is None and isinstance(message, dict):
            msg_type = message.get("type") or message.get("role")
        if msg_type in {"human", "user"}:
            text = _message_text(message).strip()
            if text:
                return text
    return ""


def _publish(writer: Any, event: dict[str, Any]) -> None:
    if writer is None:
        return
    try:
        writer(event)
    except Exception:
        logger.debug("Failed to publish SaaS semantic progress", exc_info=True)


def _coverage_summary(coverage: dict[str, Any]) -> str:
    def ids(name: str) -> list[str]:
        return [str(item.get("id")) for item in coverage.get(name, []) if isinstance(item, dict) and item.get("id")]

    return f"ontology_version={coverage.get('ontology_version')}; objects={ids('objects')}; metrics={ids('metrics')}; actions={ids('actions')}; scope_hash={coverage.get('authorization_scope_hash') or coverage.get('scope_hash')}"


def _is_semantically_covered(coverage: dict[str, Any]) -> bool:
    return any(coverage.get(name) for name in ("objects", "metrics", "actions"))


def _primary_tools(coverage: dict[str, Any]) -> list[Any]:
    if coverage.get("actions"):
        return list(SEMANTIC_TOOLS)
    return list(SEMANTIC_READ_TOOLS)


def _sql_fallback_allowed(runtime: dict[str, Any]) -> bool:
    mode = os.environ.get(_SQL_FALLBACK_MODE_ENV, "scoped").strip().lower()
    if mode == "scoped":
        return True
    if mode == "disabled":
        return False
    if mode != "role_allowlist":
        return False
    allowed = {
        role.strip()
        for role in os.environ.get(
            _SQL_FALLBACK_ROLES_ENV,
            "tenant_admin,data_engineer",
        ).split(",")
        if role.strip()
    }
    authorization = runtime.get("authorization_context")
    roles = set(authorization.get("role_codes") or ()) if isinstance(authorization, dict) else set()
    return bool(allowed.intersection(roles))


async def _resolve_semantic_coverage(
    question: str,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(runtime.get("run_id") or "unknown")
    client = SemanticPlatformClient.from_runtime(
        SimpleNamespace(context=runtime, tool_call_id=f"semantic-route:{run_id}"),
    )
    return await client.resolve_business_context(
        question=question,
        include_facts=False,
        fact_limit=1,
    )


def _primary_prompt(question: str, coverage: dict[str, Any]) -> str:
    return f"""你正在 saas-query run profile 中执行业务语义主查询或受控 Action 规划。
你的能力名称是 mysql-query，但本次运行不允许生成或执行 SQL；只允许调用 Semantic Query / Action 工具。

用户问题：
{question}

已由服务器完成的授权语义覆盖判定：
{_coverage_summary(coverage)}

要求：
1. 先基于已识别的 ontology object、metric 或 action 选择语义工具；必要时再次调用 resolve_business_context 获取授权事实。
2. 查询只能使用 search_objects、get_object、query_metrics、explain_metric，参数只能是 ontology 标识、业务维度和结构化过滤。
3. 写回只能使用 list_available_actions、propose_action、preview_action、execute_action、get_action_status；不得绕过 proposal、preview、审批或 Worker。
4. 不得提交数据库名、表名、字段列名、JOIN、SQL、JDBC URL、密码、token 或 scope 条件。
5. 返回所用 object/metric/action 及版本、时间范围、授权 scope hash、lineage、结果和限制。
6. 若 Action 仍需审批，只说明 proposal_id 和审批状态，不得声称已经执行。
"""


def _validator_prompt(
    question: str,
    coverage: dict[str, Any],
    primary: _SemanticAgentRun,
) -> str:
    primary_text = primary.result.result or primary.result.error or "No primary result"
    return f"""你正在 saas-query run profile 中执行轻量业务语义验证。
你的能力名称是 mysql-validator；本次不生成第二条 SQL，也不执行任何 Action。

用户问题：
{question}

授权语义覆盖：
{_coverage_summary(coverage)}

主查询输出：
{primary_text}

验证要求：
1. 核对 object/metric/action 标识是否来自 ontology，指标版本、允许维度、过滤、时间口径和单位是否一致。
2. 核对结果声明的 authorization_scope_hash、source_refs 和 as_of 是否完整；不得扩大 scope。
3. 只在必要时调用 explain_metric 核对已选指标定义；复用主查询结果，不重新执行数据查询。
4. 不得生成 SQL，不得调用 Action 变更工具。
5. 返回“通过语义验证”或“语义验证不一致”，并列出明确原因。
"""


async def _run_semantic_subagent(
    *,
    role: str,
    subagent_type: str,
    prompt: str,
    tools: list[Any],
    system_prompt: str,
    model_name: str | None,
    thread_id: str | None,
    trace_id: str | None,
    runtime_context: dict[str, Any],
    app_config: AppConfig,
) -> _SemanticAgentRun:
    base_config = get_subagent_config(subagent_type, app_config=app_config)
    if base_config is None:
        raise RuntimeError(f"Unknown subagent type {subagent_type!r}")
    config = replace(
        base_config,
        system_prompt=system_prompt,
        tools=[tool.name for tool in tools],
        skills=[],
        disallowed_tools=["task", "ask_clarification", "present_files"],
    )
    executor = SubagentExecutor(
        config=config,
        tools=list(tools),
        app_config=app_config,
        parent_model=model_name,
        thread_id=thread_id,
        trace_id=trace_id,
        user_id=runtime_context.get("user_id"),
        user_role=runtime_context.get("user_role"),
        oauth_provider=runtime_context.get("oauth_provider"),
        oauth_id=runtime_context.get("oauth_id"),
        run_id=runtime_context.get("run_id"),
        channel_user_id=runtime_context.get("channel_user_id"),
        deerflow_trace_id=runtime_context.get("deerflow_trace_id"),
        runtime_context=runtime_context,
    )
    result = await executor._aexecute(prompt)
    journal = runtime_context.get("__run_journal")
    if journal is not None and hasattr(journal, "record_external_tool_messages"):
        journal.record_external_tool_messages(
            getattr(result, "ai_messages", None) or [],
            caller=f"subagent:{config.name}",
        )
    return _SemanticAgentRun(role=role, subagent_type=subagent_type, result=result)


def _format_result(run: _SemanticAgentRun) -> str:
    body = run.result.result if run.result.status == SubagentStatus.COMPLETED else run.result.error
    return f"### {run.role} ({run.subagent_type})\nStatus: {run.result.status.value}\n\n{body or 'No result returned.'}"


async def _summarize(
    *,
    question: str,
    coverage: dict[str, Any],
    primary: _SemanticAgentRun,
    validator: _SemanticAgentRun,
    model_name: str | None,
    app_config: AppConfig,
    config: RunnableConfig | dict | None,
) -> str:
    fallback = f"# SaaS 业务语义结果\n\n用户问题：{question}\n\n{_format_result(primary)}\n\n{_format_result(validator)}"
    prompt = f"""你是 SaaS 业务语义查询的最终汇总器。只能基于语义主结果和轻量验证结果总结，不得生成 SQL 或发起 Action。

用户问题：{question}
覆盖判定：{_coverage_summary(coverage)}

{_format_result(primary)}

{_format_result(validator)}

规则：
1. 两侧一致时输出最终答案，并说明 metric/object/action、版本、scope、时间、lineage 和限制。
2. 不一致时明确标注“语义验证不一致”，分别列出差异，不得拼接或猜测结果。
3. Action 未达到 SUCCEEDED 时不得描述为已完成。
4. 不得泄露数据库、连接串、密码、service token、Authorization token 或内部执行器信息。
"""
    try:
        model = create_chat_model(
            name=model_name,
            thinking_enabled=False,
            app_config=app_config,
            attach_tracing=False,
        )
        runnable_config: dict[str, Any] = {
            "run_name": "saas_query_summary",
            "tags": ["saas-query:summary"],
        }
        if isinstance(config, dict):
            if config.get("callbacks"):
                runnable_config["callbacks"] = config["callbacks"]
            if isinstance(config.get("metadata"), dict):
                runnable_config["metadata"] = config["metadata"]
        response = await model.ainvoke([HumanMessage(content=prompt)], config=runnable_config)
        return _message_text(response).strip() or fallback
    except Exception:
        logger.warning("SaaS semantic summary failed; returning deterministic fallback", exc_info=True)
        return fallback


def _report_usage(runtime: dict[str, Any], runs: list[_SemanticAgentRun]) -> None:
    journal = runtime.get("__run_journal")
    if journal is None or not hasattr(journal, "record_external_llm_usage_records"):
        return
    records: list[dict[str, Any]] = []
    for run in runs:
        records.extend(run.result.token_usage_records or [])
    if records:
        journal.record_external_llm_usage_records(records)


async def _run_sql_fallback(state: ThreadState, config: RunnableConfig | None) -> dict[str, Any]:
    return await _cross_validate_node(state, config)


async def _record_shadow_sql(
    *,
    state: ThreadState,
    config: RunnableConfig | None,
    runtime: dict[str, Any],
    semantic_content: str,
) -> None:
    if os.environ.get(_SHADOW_SQL_ENV, "false").strip().lower() != "true":
        return
    try:
        result = await _run_sql_fallback(state, config)
        messages = result.get("messages") or []
        sql_content = _message_text(messages[-1]) if messages else ""
        journal = runtime.get("__run_journal")
        if journal is not None and hasattr(journal, "record_middleware"):
            journal.record_middleware(
                tag="semantic_shadow",
                name="SaasQueryMigrationRouter",
                hook="route",
                action="compare",
                changes={
                    "semantic_output_hash": hashlib.sha256(semantic_content.encode("utf-8")).hexdigest(),
                    "sql_output_hash": hashlib.sha256(sql_content.encode("utf-8")).hexdigest(),
                    "exact_match": semantic_content == sql_content,
                },
            )
    except Exception:
        logger.warning("Shadow SQL execution failed", exc_info=True)


async def _saas_query_node(
    state: ThreadState,
    config: RunnableConfig,
) -> dict[str, Any]:
    runtime = _runtime_config(config)
    app_config = runtime.get("app_config") or get_app_config()
    question = _extract_question(state)
    run_id = runtime.get("run_id")
    thread_id = runtime.get("thread_id")
    model_name = runtime.get("model_name")
    trace_id = str(run_id)[:8] if run_id else None
    try:
        writer = get_stream_writer()
    except Exception:
        writer = None

    _publish(writer, {"type": "saas_query_routing_started"})
    try:
        coverage = await _resolve_semantic_coverage(question, runtime)
    except SemanticClientError as exc:
        _publish(
            writer,
            {"type": "saas_query_routing_failed", "code": exc.code},
        )
        return {"messages": [AIMessage(content=(f"SaaS 业务语义服务当前不可用，已按 fail-closed 策略停止；未降级到自由 SQL。错误类别：{exc.code}。"))]}

    action_authorization = coverage.get("action_authorization")
    if isinstance(action_authorization, dict) and action_authorization.get("status") == "denied":
        code = str(action_authorization.get("code") or "AUTHORIZATION_DENIED")
        semantic_trace_id = str(coverage.get("semantic_trace_id") or "")
        event = {
            "type": "saas_query_action_preflight_denied",
            "code": code,
            "semantic_trace_id": semantic_trace_id,
        }
        _publish(writer, event)
        journal = runtime.get("__run_journal")
        if journal is not None and hasattr(journal, "record_semantic_preflight_denial"):
            journal.record_semantic_preflight_denial(
                code=code,
                semantic_trace_id=semantic_trace_id,
            )
        return {"messages": [AIMessage(content="当前操作未获得授权，已在 Semantic Platform 预检阶段拒绝。未创建 Action 提议，也未执行任何写回。")]}

    if not _is_semantically_covered(coverage):
        if _sql_fallback_allowed(runtime):
            _publish(writer, {"type": "saas_query_routed", "route": "scoped_sql_fallback"})
            return await _run_sql_fallback(state, config)
        _publish(writer, {"type": "saas_query_routed", "route": "uncovered_denied"})
        return {"messages": [AIMessage(content=("当前问题尚未被已发布的 Ontology/Metric 覆盖，且此角色的 scoped SQL fallback 已关闭。请先发布对应业务语义定义，或使用独立的 break-glass 数据工程入口。"))]}

    _publish(writer, {"type": "saas_query_routed", "route": "semantic"})
    runtime_context = {key: value for key, value in runtime.items() if key not in {"configurable", "callbacks"}}
    runtime_context["app_config"] = app_config
    primary = await _run_semantic_subagent(
        role="业务语义主查询",
        subagent_type=_PRIMARY_AGENT,
        prompt=_primary_prompt(question, coverage),
        tools=_primary_tools(coverage),
        system_prompt=("You are a SaaS business semantic specialist. Use only the provided Semantic Platform tools. Never generate SQL, inspect files, run commands, or accept tenant/scope/database values from the user."),
        model_name=model_name,
        thread_id=thread_id,
        trace_id=trace_id,
        runtime_context=runtime_context,
        app_config=app_config,
    )
    validator = await _run_semantic_subagent(
        role="业务语义验证",
        subagent_type=_VALIDATOR_AGENT,
        prompt=_validator_prompt(question, coverage, primary),
        tools=list(SEMANTIC_VALIDATION_TOOLS),
        system_prompt=("You validate controlled business semantics. Use only metric-definition tools. Do not generate SQL and do not propose or execute Actions."),
        model_name=model_name,
        thread_id=thread_id,
        trace_id=trace_id,
        runtime_context=runtime_context,
        app_config=app_config,
    )
    runs = [primary, validator]
    _report_usage(runtime_context, runs)
    for run in runs:
        _publish(
            writer,
            {
                "type": "saas_query_subagent_completed",
                "role": run.role,
                "subagent_type": run.subagent_type,
                "status": run.result.status.value,
            },
        )
    content = await _summarize(
        question=question,
        coverage=coverage,
        primary=primary,
        validator=validator,
        model_name=model_name,
        app_config=app_config,
        config=config,
    )
    await _record_shadow_sql(
        state=state,
        config=config,
        runtime=runtime_context,
        semantic_content=content,
    )
    return {"messages": [AIMessage(content=content)]}


def make_saas_query_agent(
    config: RunnableConfig,
    *,
    app_config: AppConfig | None = None,
):
    """Build the dedicated semantic-first SaaS graph without Lead/general tools."""
    if app_config is None:
        app_config = get_app_config()
    config.setdefault("metadata", {}).update(
        {
            "agent_name": _RUN_PROFILE,
            "run_profile": _RUN_PROFILE,
            "subagent_enabled": True,
            "max_concurrent_subagents": 1,
        }
    )
    builder = StateGraph(ThreadState)
    node = _saas_query_node
    if "config" not in inspect.signature(node).parameters:
        raise RuntimeError("SaaS query node must accept RunnableConfig")
    builder.add_node("saas_query", node)
    builder.add_edge(START, "saas_query")
    builder.add_edge("saas_query", END)
    return builder.compile()
