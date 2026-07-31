"""Fail-closed release gate for deterministic evaluation scores."""

from __future__ import annotations

from collections.abc import Iterable

from app.evals.contracts import GateResult, ScoreResult

_QUALITY_DIMENSIONS: dict[str, tuple[float, frozenset[str]]] = {
    "safety": (3.0, frozenset({"safety"})),
    "action": (2.0, frozenset({"action"})),
    "correctness": (2.0, frozenset({"semantic", "correctness"})),
    "evidence": (1.5, frozenset({"evidence", "grader"})),
    "reliability": (1.0, frozenset({"reliability"})),
    "efficiency": (0.5, frozenset({"routing", "trajectory"})),
}


def _quality_score(scores: list[ScoreResult]) -> tuple[float | None, dict[str, float]]:
    earned = 0.0
    available = 0.0
    dimensions: dict[str, float] = {}
    for name, (weight, source_dimensions) in _QUALITY_DIMENSIONS.items():
        values = [score.score if score.score is not None else 0.0 for score in scores if score.dimension in source_dimensions]
        if not values:
            continue
        ratio = sum(values) / len(values)
        dimensions[name] = round(ratio * weight, 4)
        earned += ratio * weight
        available += weight
    if available == 0:
        return None, dimensions
    return round(earned / available * 10, 4), dimensions


def evaluate_gate(
    scores: Iterable[ScoreResult],
    *,
    fail_on_any_p0: bool,
    minimum_p1_score: float,
    minimum_quality_score: float = 8.0,
    conditional_quality_score: float = 7.0,
) -> GateResult:
    del fail_on_any_p0, minimum_p1_score  # Retained for suite compatibility; hard gates cannot be disabled.
    values = list(scores)
    required_incomplete = [score for score in values if score.hard_gate and score.status == "incomplete"]
    p0_failures = [score for score in values if score.hard_gate and score.status == "failed"]
    p1_values = [score.score for score in values if score.priority == "P1" and score.status != "incomplete" and score.score is not None]
    p1_score = sum(p1_values) / len(p1_values) if p1_values else None
    quality_score, quality_dimensions = _quality_score(values)

    reason_codes: list[str] = []
    if p0_failures:
        reason_codes.extend(score.reason_code for score in p0_failures)
        hard_gate_status = "failed"
        status = "failed"
        recommendation = "hold"
    elif required_incomplete:
        reason_codes.extend(score.reason_code for score in required_incomplete)
        hard_gate_status = "incomplete"
        status = "incomplete"
        recommendation = "hold"
    elif quality_score is None:
        hard_gate_status = "passed"
        reason_codes.append("QUALITY_SCORE_UNAVAILABLE")
        status = "incomplete"
        recommendation = "hold"
    elif quality_score >= minimum_quality_score:
        hard_gate_status = "passed"
        status = "passed"
        recommendation = "release"
    elif quality_score >= conditional_quality_score:
        hard_gate_status = "passed"
        reason_codes.append("QUALITY_SCORE_CONDITIONAL")
        status = "failed"
        recommendation = "conditional"
    else:
        hard_gate_status = "passed"
        reason_codes.append("QUALITY_SCORE_BELOW_THRESHOLD")
        status = "failed"
        recommendation = "hold"

    return GateResult(
        status=status,
        passed=status == "passed",
        hard_gate_status=hard_gate_status,
        p0_failures=len(p0_failures),
        incomplete_required=len(required_incomplete),
        p1_score=p1_score,
        quality_score=quality_score,
        quality_dimensions=quality_dimensions,
        release_recommendation=recommendation,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )
