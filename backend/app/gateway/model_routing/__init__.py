"""Gateway-local two-stage model routing."""

from app.gateway.model_routing.contracts import (
    RouteSource,
    RouteType,
    RoutingDecision,
    RoutingInput,
    RoutingSignals,
)
from app.gateway.model_routing.faiss_search import FaissConfigurationError
from app.gateway.model_routing.router import ModelRoutingConfigError, build_routing_input, extract_latest_user_text, route_model

__all__ = [
    "ModelRoutingConfigError",
    "FaissConfigurationError",
    "RouteSource",
    "RouteType",
    "RoutingDecision",
    "RoutingInput",
    "RoutingSignals",
    "build_routing_input",
    "extract_latest_user_text",
    "route_model",
]
