"""Load versioned Evals suites from YAML and JSONL files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.evals.contracts import EvalCase, EvalSuite, LoadedSuite


class DatasetLoadError(ValueError):
    """Raised when an evaluation suite cannot be loaded safely."""


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DatasetLoadError(f"Could not load suite {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DatasetLoadError(f"Suite {path} must contain a YAML mapping")
    return payload


def _read_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetLoadError(f"Could not read case file {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetLoadError(f"Invalid JSON in {path} line {line_number}: {exc.msg}") from exc
        try:
            cases.append(EvalCase.model_validate(payload))
        except ValidationError as exc:
            raise DatasetLoadError(f"Invalid EvalCase in {path} line {line_number}: {exc}") from exc
    return cases


def _dataset_hash(suite: EvalSuite, cases: list[EvalCase]) -> str:
    canonical = {
        "suite": suite.model_dump(mode="json"),
        "cases": [case.model_dump(mode="json") for case in sorted(cases, key=lambda item: item.case_id)],
    }
    encoded = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_suite(path: str | Path) -> LoadedSuite:
    suite_path = Path(path).resolve()
    try:
        suite = EvalSuite.model_validate(_read_yaml(suite_path))
    except ValidationError as exc:
        raise DatasetLoadError(f"Invalid EvalSuite in {suite_path}: {exc}") from exc

    cases: list[EvalCase] = []
    for relative in suite.case_files:
        case_path = (suite_path.parent / relative).resolve()
        cases.extend(_read_cases(case_path))
    if not cases:
        raise DatasetLoadError(f"Suite {suite.suite_id} contains no cases")

    case_ids = [case.case_id for case in cases]
    duplicates = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
    if duplicates:
        raise DatasetLoadError(f"Suite contains duplicate case_id values: {', '.join(duplicates)}")

    sorted_cases = sorted(cases, key=lambda item: item.case_id)
    return LoadedSuite(
        suite=suite,
        cases=tuple(sorted_cases),
        dataset_hash=_dataset_hash(suite, sorted_cases),
    )
