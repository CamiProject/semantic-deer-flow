"""Two-stage model routing orchestration."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from app.gateway.model_routing.contracts import RouteType, RoutingDecision, RoutingInput
from app.gateway.model_routing.faiss_search import FaissConfigurationError, FaissSearcher, FaissSearchError, FaissSearchResult, get_faiss_searcher
from app.gateway.model_routing.rules import RuleResult, classify_rules
from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)


class ModelRoutingConfigError(ValueError):
    """Raised when an enabled router cannot resolve its configured models."""


async def route_model(
    routing_input: RoutingInput,
    *,
    app_config: AppConfig,
    searcher: FaissSearcher | None = None,
) -> RoutingDecision | None:
    """Route one authenticated request without invoking an LLM.

    Rules handle high-confidence requests first. Only ``undecided`` requests
    reach the local FAISS adapter. Any unavailable or ambiguous second-stage
    result fails closed to the configured complex model.
    """
    config = app_config.model_routing
    if config.mode == "disabled":
        return None
    _validate_target_models(app_config)
    started_at = time.perf_counter()

    rule_result = classify_rules(routing_input)
    if rule_result.route_type is not None:
        return _with_latency(_decision_from_rule(rule_result, app_config=app_config), started_at)

    searcher = searcher or get_faiss_searcher(config.faiss)
    try:
        result = await searcher.search(routing_input.question)
    except FaissConfigurationError as exc:
        if config.mode == "enforce":
            raise ModelRoutingConfigError(f"Invalid model_routing FAISS assets: {exc}") from exc
        logger.warning("Model routing FAISS assets unavailable; falling back to complex: %s", exc)
        return _with_latency(_fallback_decision(app_config), started_at)
    except FaissSearchError as exc:
        logger.warning("Model routing FAISS stage unavailable; falling back to complex: %s", exc)
        return _with_latency(_fallback_decision(app_config), started_at)
    except Exception:
        logger.exception("Model routing FAISS stage failed; falling back to complex")
        return _with_latency(_fallback_decision(app_config), started_at)
    if result is None:
        return _with_latency(_fallback_decision(app_config), started_at)
    return _with_latency(_decision_from_faiss(result, app_config=app_config), started_at)


def build_routing_input(
    raw_input: Mapping[str, Any] | None,
    *,
    endpoint: str | None = None,
    assistant_id: str | None = None,
    action_allowed: bool = False,
) -> RoutingInput:
    """Extract only the latest user text before routing."""
    return RoutingInput(
        question=extract_latest_user_text(raw_input),
        endpoint=endpoint,
        assistant_id=assistant_id,
        action_allowed=action_allowed,
    )


def extract_latest_user_text(raw_input: Mapping[str, Any] | None) -> str:
    """Extract text from the newest user/human message without retaining history."""
    if not isinstance(raw_input, Mapping):
        return ""
    messages = raw_input.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, Mapping):
            role = str(message.get("role") or message.get("type") or "").lower()
            if role not in {"user", "human"}:
                continue
            return _content_to_text(message.get("content"))
        role = str(getattr(message, "type", "") or getattr(message, "role", "")).lower()
        if role in {"user", "human"}:
            return _content_to_text(getattr(message, "content", ""))
    return ""


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return " ".join(parts)
    return ""


def _validate_target_models(app_config: AppConfig) -> None:
    config = app_config.model_routing
    for route_type, model_name in (("simple", config.simple_model), ("complex", config.complex_model)):
        if not model_name:
            raise ModelRoutingConfigError(f"model_routing.{route_type}_model is required when routing is enabled")
        if app_config.get_model_config(model_name) is None:
            raise ModelRoutingConfigError(f"model_routing.{route_type}_model {model_name!r} is not in the configured model allowlist")


def _decision_from_rule(result: RuleResult, *, app_config: AppConfig) -> RoutingDecision:
    assert result.route_type is not None
    return RoutingDecision(
        route_type=result.route_type,
        model_name=_model_for(result.route_type, app_config),
        source="rules",
        confidence=result.confidence,
        reason_codes=result.reason_codes,
        signals=result.signals,
        rules_version=app_config.model_routing.rules_version,
        index_version=app_config.model_routing.faiss.index_version,
    )


def _decision_from_faiss(result: FaissSearchResult, *, app_config: AppConfig) -> RoutingDecision:
    return RoutingDecision(
        route_type=result.route_type,
        model_name=_model_for(result.route_type, app_config),
        source="faiss",
        confidence=result.confidence,
        reason_codes=result.reason_codes,
        signals=result.signals,
        rules_version=app_config.model_routing.rules_version,
        index_version=result.index_version,
    )


def _fallback_decision(app_config: AppConfig) -> RoutingDecision:
    config = app_config.model_routing
    return RoutingDecision(
        route_type="complex",
        model_name=_model_for("complex", app_config),
        source="fallback",
        confidence=0.0,
        reason_codes=("faiss_unavailable_or_undecided",),
        rules_version=config.rules_version,
        index_version=config.faiss.index_version,
    )


def _model_for(route_type: RouteType, app_config: AppConfig) -> str:
    model_name = app_config.model_routing.simple_model if route_type == "simple" else app_config.model_routing.complex_model
    assert model_name is not None
    return model_name


def _with_latency(decision: RoutingDecision, started_at: float) -> RoutingDecision:
    return replace(decision, decision_latency_ms=round((time.perf_counter() - started_at) * 1000, 3))
