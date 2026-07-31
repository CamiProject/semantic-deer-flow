from pathlib import Path

from app.evals.graders import GRADERS
from app.evals.loader import load_suite


def test_committed_smoke_suite_has_required_mvp_coverage():
    root = Path(__file__).resolve().parents[2]
    loaded = load_suite(root / "evals" / "suites" / "saas-agent-smoke.yaml")

    assert 10 <= len(loaded.cases) <= 15
    assert {case.category for case in loaded.cases}.issuperset({"semantic_read", "action", "model_routing"})
    assert any("scope" in case.tags for case in loaded.cases)
    assert any(case.expect.action and case.expect.action.outcome == "success" for case in loaded.cases)
    assert any(case.expect.action and case.expect.action.outcome == "rejected" for case in loaded.cases)
    assert {case.expect.routing.route_type for case in loaded.cases if case.expect.routing is not None} == {"simple", "complex"}
    assert all(grader in GRADERS for case in loaded.cases for grader in case.graders)
