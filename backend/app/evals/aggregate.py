"""Deterministic Trial, Case and Suite aggregation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.evals.contracts import ScoreResult, TrialObservation


def aggregate_results(
    observations: list[TrialObservation],
    scores: list[ScoreResult],
) -> dict[str, Any]:
    scores_by_trial: dict[tuple[str, int], list[ScoreResult]] = defaultdict(list)
    for score in scores:
        scores_by_trial[(score.case_id, score.trial_index)].append(score)

    trials_by_case: dict[str, list[TrialObservation]] = defaultdict(list)
    for observation in observations:
        trials_by_case[observation.case_id].append(observation)

    cases: dict[str, Any] = {}
    for case_id, case_trials in sorted(trials_by_case.items()):
        ordered = sorted(case_trials, key=lambda item: item.trial_index)
        trial_passes: list[bool] = []
        reason_codes: list[str] = []
        for trial in ordered:
            trial_scores = scores_by_trial[(case_id, trial.trial_index)]
            passed = bool(trial_scores) and all(score.status == "passed" for score in trial_scores)
            trial_passes.append(passed)
            reason_codes.extend(score.reason_code for score in trial_scores if score.status != "passed")
        cases[case_id] = {
            "trial_count": len(ordered),
            "passed_trials": sum(trial_passes),
            "pass_at_1": trial_passes[0] if trial_passes else False,
            "pass_at_k": any(trial_passes),
            "pass_power_k": all(trial_passes) if trial_passes else False,
            "reason_codes": list(dict.fromkeys(reason_codes)),
        }

    return {
        "trial_count": len(observations),
        "case_count": len(cases),
        "passed_cases": sum(value["pass_power_k"] for value in cases.values()),
        "cases": cases,
    }
