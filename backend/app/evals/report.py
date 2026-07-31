"""Machine-readable and human-readable Evals report generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evals.aggregate import aggregate_results
from app.evals.contracts import ScoreResult, TrialObservation
from app.evals.gate import evaluate_gate
from app.evals.redaction import redact_report_value

_FAILURE_KIND_ORDER = (
    "task_failed",
    "grader_failed",
    "evidence_incomplete",
    "fixture_failed",
)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(redact_report_value(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    content = "".join(json.dumps(redact_report_value(value), ensure_ascii=False, sort_keys=True) + "\n" for value in values)
    path.write_text(content, encoding="utf-8")


def _classify_failures(
    observations: list[TrialObservation],
    scores: list[ScoreResult],
) -> dict[str, Any]:
    by_trial: dict[tuple[str, int], set[str]] = {}
    for observation in observations:
        kinds = by_trial.setdefault((observation.case_id, observation.trial_index), set())
        if observation.run.status.lower() not in {"success", "completed"} or observation.run.error:
            kinds.add("task_failed")
        evidence_status = observation.evidence_quality.status
        if evidence_status == "fixture_failed":
            kinds.add("fixture_failed")
        elif evidence_status != "complete":
            kinds.add("evidence_incomplete")
    for score in scores:
        if score.grader_error or score.reason_code in {"GRADER_FAILED", "GRADER_NOT_REGISTERED"}:
            by_trial.setdefault((score.case_id, score.trial_index), set()).add("grader_failed")

    counts = {kind: sum(kind in kinds for kinds in by_trial.values()) for kind in _FAILURE_KIND_ORDER}
    trials = [
        {
            "case_id": case_id,
            "trial_index": trial_index,
            "kinds": [kind for kind in _FAILURE_KIND_ORDER if kind in kinds],
        }
        for (case_id, trial_index), kinds in sorted(by_trial.items())
        if kinds
    ]
    return {"counts": counts, "trials": trials}


def write_report(
    *,
    output_root: str | Path,
    eval_run_id: str,
    manifest: dict[str, Any],
    observations: list[TrialObservation],
    scores: list[ScoreResult],
    fail_on_any_p0: bool,
    minimum_p1_score: float,
    minimum_quality_score: float = 8.0,
    conditional_quality_score: float = 7.0,
) -> Path:
    output = Path(output_root) / eval_run_id
    output.mkdir(parents=True, exist_ok=False)
    gate = evaluate_gate(
        scores,
        fail_on_any_p0=fail_on_any_p0,
        minimum_p1_score=minimum_p1_score,
        minimum_quality_score=minimum_quality_score,
        conditional_quality_score=conditional_quality_score,
    )
    aggregate = aggregate_results(observations, scores)
    failure_classification = _classify_failures(observations, scores)

    manifest_payload = {**manifest, "eval_run_id": eval_run_id}
    trial_payloads = [item.model_dump(mode="json") for item in observations]
    score_payloads = [item.model_dump(mode="json") for item in scores]
    report = {
        "schema_version": "1",
        "eval_run_id": eval_run_id,
        "summary": {
            "trial_count": len(observations),
            "score_count": len(scores),
            "passed_scores": sum(score.status == "passed" for score in scores),
            "failed_scores": sum(score.status == "failed" for score in scores),
            "incomplete_scores": sum(score.status == "incomplete" for score in scores),
        },
        "aggregate": aggregate,
        "gate": gate.model_dump(mode="json"),
        "failure_classification": failure_classification,
        "failures": [payload for payload in score_payloads if payload["status"] != "passed"],
    }

    _write_json(output / "manifest.json", manifest_payload)
    _write_jsonl(output / "trials.jsonl", trial_payloads)
    _write_jsonl(output / "scores.jsonl", score_payloads)
    _write_json(output / "report.json", report)

    lines = [
        f"# Eval Report: {eval_run_id}",
        "",
        f"- P0 hard gate: {gate.hard_gate_status.upper()}",
        f"- Release gate: {gate.status.upper()}",
        f"- Quality score: {gate.quality_score if gate.quality_score is not None else 'n/a'}/10",
        f"- Release recommendation: {gate.release_recommendation.upper()}",
        f"- Trials: {len(observations)}",
        f"- Scores: {len(scores)}",
        f"- P0 failures: {gate.p0_failures}",
        f"- Required evidence incomplete: {gate.incomplete_required}",
        f"- P1 score: {gate.p1_score if gate.p1_score is not None else 'n/a'}",
        "",
        "## Failure Classification",
        "",
        *(f"- {kind}: {failure_classification['counts'][kind]}" for kind in _FAILURE_KIND_ORDER),
        "",
        "## Findings",
        "",
    ]
    failures = [score for score in scores if score.status != "passed"]
    if failures:
        lines.extend(f"- `{score.case_id}` / `{score.grader_id}`: {score.reason_code} - {score.summary}" for score in failures)
    else:
        lines.append("No failed or incomplete scores.")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output
