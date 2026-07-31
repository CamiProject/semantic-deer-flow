from __future__ import annotations

import json

import pytest

from app.evals.loader import DatasetLoadError, load_suite


def _case(case_id: str) -> dict:
    return {
        "schema_version": "1",
        "case_id": case_id,
        "title": case_id,
        "category": "model_routing",
        "risk": "low",
        "target": {"assistant_id": "saas-query", "endpoint_mode": "wait"},
        "turns": [{"role": "user", "content": "hello"}],
        "fixture": {
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "principal_id": "public-user-001",
            "system_code": "demo",
            "role_codes": ["viewer"],
            "scope": {"mode": "tenant_all", "site_ids": [], "project_ids": []},
            "scenario": "empty",
        },
        "expect": {"routing": {"route_type": "simple", "source": "rules"}},
        "graders": ["run_completed", "routing_decision"],
    }


def _write_suite(tmp_path, *, case_lines: list[str]):
    cases_dir = tmp_path / "cases"
    suites_dir = tmp_path / "suites"
    cases_dir.mkdir()
    suites_dir.mkdir()
    case_path = cases_dir / "smoke.jsonl"
    case_path.write_text("\n".join(case_lines) + "\n", encoding="utf-8")
    suite_path = suites_dir / "smoke.yaml"
    suite_path.write_text(
        "\n".join(
            [
                'schema_version: "1"',
                "suite_id: smoke",
                'version: "1"',
                "case_files:",
                "  - ../cases/smoke.jsonl",
                "gate:",
                "  fail_on_any_p0: true",
            ]
        ),
        encoding="utf-8",
    )
    return suite_path, case_path


def test_loader_reads_yaml_jsonl_and_produces_stable_dataset_hash(tmp_path):
    lines = [json.dumps(_case("case-b")), json.dumps(_case("case-a"))]
    suite_path, case_path = _write_suite(tmp_path, case_lines=lines)

    first = load_suite(suite_path)
    case_path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
    second = load_suite(suite_path)

    assert [case.case_id for case in first.cases] == ["case-a", "case-b"]
    assert first.dataset_hash == second.dataset_hash
    assert len(first.dataset_hash) == 64


def test_loader_rejects_duplicate_case_ids(tmp_path):
    duplicate = json.dumps(_case("duplicate"))
    suite_path, _case_path = _write_suite(tmp_path, case_lines=[duplicate, duplicate])

    with pytest.raises(DatasetLoadError, match="duplicate"):
        load_suite(suite_path)


def test_loader_reports_invalid_jsonl_line(tmp_path):
    suite_path, _case_path = _write_suite(tmp_path, case_lines=["{not-json}"])

    with pytest.raises(DatasetLoadError, match="line 1"):
        load_suite(suite_path)
