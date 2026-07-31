from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.evals import cli


def test_evals_make_targets_load_root_env_file():
    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text(encoding="utf-8")

    assert "uv run --env-file ../.env uvicorn app.evals.fixture_api:create_fixture_app" in makefile
    assert "uv run --env-file ../.env python -m app.evals.cli run" in makefile


@pytest.mark.parametrize(
    ("gate_status", "exit_code"),
    [("passed", 0), ("failed", 1), ("incomplete", 2)],
)
def test_cli_run_wires_suite_clients_runner_and_gate_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    gate_status: str,
    exit_code: int,
):
    suite_path = tmp_path / "suite.yaml"
    output_dir = tmp_path / "reports"
    loaded = object()
    settings = SimpleNamespace(name="eval-settings")
    constructed: dict[str, object] = {}

    monkeypatch.setattr(cli, "load_suite", lambda path: loaded if path == suite_path else None)
    monkeypatch.setattr(
        cli.EvalRunnerSettings,
        "from_env",
        lambda *, gateway_url, suite_output_root: settings if gateway_url == "http://gateway.internal" and suite_output_root == output_dir else None,
    )

    class FakeGatewayClient:
        def __init__(self, configured):
            constructed["gateway"] = configured

    class FakeSemanticClient:
        def __init__(self, configured):
            constructed["semantic"] = configured

    class FakeFixtureClient:
        def __init__(self, configured):
            constructed["fixture"] = configured

    class FakeCollector:
        def __init__(self, *, semantic, fixture):
            constructed["collector"] = (semantic, fixture)

    class FakeRunner:
        def __init__(self, *, settings, gateway, collector):
            constructed["runner"] = (settings, gateway, collector)

        async def run(self, suite):
            assert suite is loaded
            return SimpleNamespace(
                eval_run_id="eval-cli-1",
                gate=SimpleNamespace(
                    status=gate_status,
                    hard_gate_status=gate_status,
                    quality_score=10.0 if gate_status == "passed" else 6.0,
                    release_recommendation="release" if gate_status == "passed" else "hold",
                ),
                output_dir=output_dir / "eval-cli-1",
            )

    monkeypatch.setattr(cli, "GatewayClient", FakeGatewayClient)
    monkeypatch.setattr(cli, "SemanticEvidenceClient", FakeSemanticClient)
    monkeypatch.setattr(cli, "FixtureClient", FakeFixtureClient)
    monkeypatch.setattr(cli, "ObservationCollector", FakeCollector)
    monkeypatch.setattr(cli, "EvalRunner", FakeRunner)

    result = cli.main(
        [
            "run",
            "--suite",
            str(suite_path),
            "--gateway-url",
            "http://gateway.internal",
            "--output-dir",
            str(output_dir),
        ]
    )

    assert result == exit_code
    output = capsys.readouterr().out
    assert "Safety gate:" in output
    assert "Quality score:" in output
    assert "Release recommendation:" in output
    assert constructed["gateway"] is settings
    assert constructed["semantic"] is settings
    assert constructed["fixture"] is settings
    assert "Eval run: eval-cli-1" in output
    assert f"Safety gate: {gate_status.upper()}" in output
