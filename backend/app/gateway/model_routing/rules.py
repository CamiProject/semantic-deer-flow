"""Deterministic first-stage routing rules."""

from __future__ import annotations

import re

from app.gateway.model_routing.contracts import RouteType, RoutingInput, RoutingSignals


class RuleResult:
    """Internal rule result; ``route_type=None`` means FAISS is required."""

    __slots__ = ("route_type", "confidence", "reason_codes", "signals")

    def __init__(
        self,
        route_type: RouteType | None,
        *,
        confidence: float,
        reason_codes: tuple[str, ...],
        signals: RoutingSignals,
    ) -> None:
        self.route_type = route_type
        self.confidence = confidence
        self.reason_codes = reason_codes
        self.signals = signals


_HIGH_RISK_RE = re.compile(r"修改|更新|删除|删掉|写回|写入|发布|审批|批量|覆盖|新增|创建动作|执行动作|撤回|上线|停用|启用|\b(?:update|delete|write|publish|approve|batch)\b", re.IGNORECASE)
_COMPLEX_RE = re.compile(r"分析|对比|比较|原因|为什么|优化|研究|诊断|趋势|报告|验证|交叉|多个|各个|所有|分别|排名|预测|方案|计划|analy[sz]e|compare|research|diagnos|report|validate", re.IGNORECASE)
_READ_RE = re.compile(r"^(查询|查看|统计|计算|获取|列出|显示|多少|有哪些|查一下|看一下|count|list|show|query)", re.IGNORECASE)
_TIME_RE = re.compile(r"本月|上月|本周|上周|今年|去年|今天|昨天|最近|当前|过去|this month|last month|today|yesterday", re.IGNORECASE)
_CONVERSATION_RE = re.compile(r"^(?:你好|您好|嗨|hello|hi|解释|说明|什么是|请问|谢谢|为什么叫|help)", re.IGNORECASE)


def classify_rules(routing_input: RoutingInput) -> RuleResult:
    """Classify only high-confidence requests and leave the rest to FAISS."""
    question = " ".join((routing_input.question or "").split())
    if not question:
        return RuleResult(
            None,
            confidence=0.0,
            reason_codes=("empty_question",),
            signals=RoutingSignals(),
        )

    if len(question) > 800:
        return _complex("long_question", risk="unknown", difficulty="high")

    if _HIGH_RISK_RE.search(question):
        return _complex("write_or_high_risk", risk="high", difficulty="medium")

    if _COMPLEX_RE.search(question):
        return _complex("multi_step_or_analysis", risk="read", difficulty="high")

    if _CONVERSATION_RE.search(question) and len(question) <= 160:
        return RuleResult(
            "simple",
            confidence=0.96,
            reason_codes=("short_conversation",),
            signals=RoutingSignals(risk_level="none", difficulty_level="low", delivery_level="direct"),
        )

    if _READ_RE.search(question) and (_TIME_RE.search(question) or len(question) <= 160):
        return RuleResult(
            "simple",
            confidence=0.94,
            reason_codes=("single_resource_read",),
            signals=RoutingSignals(risk_level="read", difficulty_level="low", scale_level="single", delivery_level="direct"),
        )

    # A short direct question with no risk or multi-step signal is safe enough
    # for the sample retriever to decide, but not safe enough for silent simple.
    return RuleResult(
        None,
        confidence=0.0,
        reason_codes=("rule_undecided",),
        signals=RoutingSignals(),
    )


def _complex(reason_code: str, *, risk: str, difficulty: str) -> RuleResult:
    return RuleResult(
        "complex",
        confidence=0.98,
        reason_codes=(reason_code,),
        signals=RoutingSignals(risk_level=risk, difficulty_level=difficulty, delivery_level="recommendation" if difficulty == "high" else "unknown"),
    )
