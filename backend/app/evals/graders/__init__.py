"""Deterministic grader registry."""

from app.evals.graders.deterministic import GRADERS, grade_trial

__all__ = ["GRADERS", "grade_trial"]
