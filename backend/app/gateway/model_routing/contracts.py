"""Stable contracts for the Gateway model router."""

from dataclasses import dataclass, field
from typing import Literal

RouteType = Literal["simple", "complex"]
RouteSource = Literal["rules", "faiss", "fallback"]


@dataclass(frozen=True, slots=True)
class RoutingSignals:
    """Optional dimensions kept separate from the binary route type."""

    risk_level: str = "unknown"
    difficulty_level: str = "unknown"
    scale_level: str = "unknown"
    delivery_level: str = "unknown"

    def as_dict(self) -> dict[str, str]:
        return {
            "risk_level": self.risk_level,
            "difficulty_level": self.difficulty_level,
            "scale_level": self.scale_level,
            "delivery_level": self.delivery_level,
        }


@dataclass(frozen=True, slots=True)
class RoutingInput:
    """Minimal, already-authenticated input supplied by the Gateway."""

    question: str
    endpoint: str | None = None
    assistant_id: str | None = None
    action_allowed: bool = False


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """One auditable route decision without sensitive request data."""

    route_type: RouteType
    model_name: str | None
    source: RouteSource
    confidence: float
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    signals: RoutingSignals = field(default_factory=RoutingSignals)
    rules_version: str = "1"
    index_version: str | None = None
    router_version: str = "1"
    decision_latency_ms: float | None = None

    def as_metadata(self) -> dict[str, object]:
        """Return the non-sensitive subset suitable for run metadata."""
        return {
            "route_type": self.route_type,
            "model_name": self.model_name,
            "source": self.source,
            "confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
            "signals": self.signals.as_dict(),
            "rules_version": self.rules_version,
            "index_version": self.index_version,
            "router_version": self.router_version,
            "decision_latency_ms": self.decision_latency_ms,
        }
