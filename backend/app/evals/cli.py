"""Command-line entry point for one-shot DeerFlow evaluation suites."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from app.evals.collector import ObservationCollector
from app.evals.fixture_client import FixtureClient
from app.evals.gateway_client import GatewayClient
from app.evals.loader import DatasetLoadError, load_suite
from app.evals.runner import EvalRunner, EvalRunnerSettings
from app.evals.semantic_client import SemanticEvidenceClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.evals.cli")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="execute one versioned Eval Suite")
    run.add_argument("--suite", required=True, type=Path)
    run.add_argument("--gateway-url", default="http://localhost:8001")
    run.add_argument("--output-dir", type=Path)
    return parser


async def _run(args: argparse.Namespace) -> int:
    loaded = load_suite(args.suite)
    settings = EvalRunnerSettings.from_env(
        gateway_url=args.gateway_url,
        suite_output_root=args.output_dir,
    )
    gateway = GatewayClient(settings)
    semantic = SemanticEvidenceClient(settings)
    fixture = FixtureClient(settings)
    collector = ObservationCollector(semantic=semantic, fixture=fixture)
    result = await EvalRunner(settings=settings, gateway=gateway, collector=collector).run(loaded)
    print(f"Eval run: {result.eval_run_id}")
    print(f"Safety gate: {result.gate.hard_gate_status.upper()}")
    print(f"Quality score: {result.gate.quality_score if result.gate.quality_score is not None else 'n/a'}/10")
    print(f"Release recommendation: {result.gate.release_recommendation.upper()}")
    print(f"Report: {result.output_dir.resolve()}")
    if result.gate.status == "passed":
        return 0
    if result.gate.status == "incomplete":
        return 2
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (DatasetLoadError, ValidationError, ValueError) as exc:
        print(f"Evals configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
