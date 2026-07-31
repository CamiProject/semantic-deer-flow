from __future__ import annotations

import json

from app.evals.contracts import ScoreResult, TrialObservation
from app.evals.redaction import redact_report_value
from app.evals.report import write_report


def test_report_writes_manifest_trials_scores_json_and_markdown(tmp_path):
    observation = TrialObservation.model_validate(
        {
            "eval_run_id": "eval-run-1",
            "case_id": "case-1",
            "trial_index": 0,
            "thread_id": "thread-1",
            "run_id": "run-1",
            "expected_scope_hash": "scope-1",
            "run": {"status": "success", "metadata": {}},
            "final_response": "ok",
            "evidence_quality": {"status": "complete", "missing": []},
        }
    )
    score = ScoreResult.model_validate(
        {
            "case_id": "case-1",
            "trial_index": 0,
            "grader_id": "run_completed",
            "grader_version": "1",
            "dimension": "reliability",
            "priority": "P0",
            "status": "passed",
            "score": 1.0,
            "passed": True,
            "hard_gate": True,
            "reason_code": "RUN_COMPLETED",
            "summary": "Run completed",
            "evidence_refs": ["run:run-1"],
        }
    )

    output = write_report(
        output_root=tmp_path,
        eval_run_id="eval-run-1",
        manifest={"suite_id": "smoke", "dataset_hash": "abc"},
        observations=[observation],
        scores=[score],
        fail_on_any_p0=True,
        minimum_p1_score=0.8,
    )

    assert {path.name for path in output.iterdir()} == {
        "manifest.json",
        "trials.jsonl",
        "scores.jsonl",
        "report.json",
        "report.md",
    }
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["gate"]["status"] == "passed"
    assert report["gate"]["hard_gate_status"] == "passed"
    assert report["gate"]["quality_score"] == 10.0
    assert report["gate"]["release_recommendation"] == "release"
    assert report["summary"]["trial_count"] == 1
    assert report["aggregate"]["cases"]["case-1"]["pass_at_1"] is True
    assert report["aggregate"]["cases"]["case-1"]["pass_at_k"] is True
    assert report["aggregate"]["cases"]["case-1"]["pass_power_k"] is True
    assert "P0 hard gate: PASSED" in (output / "report.md").read_text(encoding="utf-8")
    assert "Quality score: 10.0/10" in (output / "report.md").read_text(encoding="utf-8")
    assert "Release recommendation: RELEASE" in (output / "report.md").read_text(encoding="utf-8")


def test_report_classifies_task_grader_evidence_and_fixture_failures(tmp_path):
    observations = [
        TrialObservation.model_validate(
            {
                "eval_run_id": "eval-run-1",
                "case_id": "case-task",
                "trial_index": 0,
                "thread_id": "thread-task",
                "run_id": "run-task",
                "expected_scope_hash": "scope-1",
                "run": {"status": "error", "metadata": {}, "error": "failed"},
                "evidence_quality": {"status": "complete"},
            }
        ),
        TrialObservation.model_validate(
            {
                "eval_run_id": "eval-run-1",
                "case_id": "case-evidence",
                "trial_index": 0,
                "thread_id": "thread-evidence",
                "run_id": "run-evidence",
                "expected_scope_hash": "scope-1",
                "run": {"status": "success", "metadata": {}},
                "evidence_quality": {
                    "status": "incomplete",
                    "missing": ["semantic_audit"],
                },
            }
        ),
        TrialObservation.model_validate(
            {
                "eval_run_id": "eval-run-1",
                "case_id": "case-fixture",
                "trial_index": 0,
                "thread_id": "thread-fixture",
                "run_id": "run-fixture",
                "expected_scope_hash": "scope-1",
                "run": {"status": "success", "metadata": {}},
                "evidence_quality": {
                    "status": "fixture_failed",
                    "missing": ["fixture_outcome"],
                },
            }
        ),
    ]
    scores = [
        ScoreResult.model_validate(
            {
                "case_id": "case-task",
                "trial_index": 0,
                "grader_id": "broken_grader",
                "dimension": "grader",
                "priority": "P0",
                "status": "incomplete",
                "passed": False,
                "hard_gate": True,
                "reason_code": "GRADER_FAILED",
                "summary": "Grader failed",
                "grader_error": "RuntimeError",
            }
        )
    ]

    output = write_report(
        output_root=tmp_path,
        eval_run_id="eval-run-1",
        manifest={"suite_id": "smoke", "dataset_hash": "abc"},
        observations=observations,
        scores=scores,
        fail_on_any_p0=True,
        minimum_p1_score=0.8,
    )

    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["failure_classification"]["counts"] == {
        "evidence_incomplete": 1,
        "fixture_failed": 1,
        "grader_failed": 1,
        "task_failed": 1,
    }
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "task_failed: 1" in markdown
    assert "fixture_failed: 1" in markdown


def test_report_labels_p1_only_failure_without_claiming_p0_failed(tmp_path):
    observation = TrialObservation.model_validate(
        {
            "eval_run_id": "eval-run-1",
            "case_id": "case-p1",
            "trial_index": 0,
            "thread_id": "thread-1",
            "run_id": "run-1",
            "expected_scope_hash": "scope-1",
            "run": {"status": "success", "metadata": {}},
        }
    )
    score = ScoreResult.model_validate(
        {
            "case_id": "case-p1",
            "trial_index": 0,
            "grader_id": "answer_exact_or_numeric",
            "dimension": "correctness",
            "priority": "P1",
            "status": "failed",
            "score": 0,
            "passed": False,
            "hard_gate": False,
            "reason_code": "ANSWER_MISMATCH",
            "summary": "Answer mismatch",
        }
    )

    output = write_report(
        output_root=tmp_path,
        eval_run_id="eval-run-1",
        manifest={},
        observations=[observation],
        scores=[score],
        fail_on_any_p0=True,
        minimum_p1_score=0.8,
    )

    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "P0 hard gate: PASSED" in markdown
    assert "Release gate: FAILED" in markdown


def test_report_redaction_recurses_through_nested_action_values():
    redacted = redact_report_value(
        {
            "action": {
                "execution": {
                    "result": {
                        "access_token": "top-secret",
                        "items": [{"password": "nested-secret"}],
                    }
                }
            }
        }
    )

    serialized = json.dumps(redacted)
    assert "top-secret" not in serialized
    assert "nested-secret" not in serialized
    assert serialized.count("[redacted]") == 2
