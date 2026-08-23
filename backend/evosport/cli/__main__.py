from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from datetime import datetime
import json
from pathlib import Path
import subprocess

from evosport import __version__
from evosport.experiments.spec import load_experiment_spec


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("datetime must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a UTC offset")
    return parsed


async def _freeze_catalog_dataset(
    *,
    provider_dataset_ids: list[str],
    football_binding_path: Path,
    output_root: Path,
    start: datetime,
    end: datetime,
):
    from evosport.data.freeze import freeze_provider_datasets
    from evosport.semantics.football_binding import FootballDatasetBinding, store_football_settlement

    binding = FootballDatasetBinding.model_validate_json(
        football_binding_path.read_text(encoding="utf-8")
    )
    manifest = await freeze_provider_datasets(
        provider_dataset_ids=provider_dataset_ids,
        output_root=output_root,
        start=start,
        end=end,
        football=binding,
    )
    await store_football_settlement(binding)
    return manifest


def _handle_dataset_freeze(args: argparse.Namespace) -> int:
    manifest = asyncio.run(
        _freeze_catalog_dataset(
            provider_dataset_ids=args.provider_dataset_id,
            football_binding_path=args.football_binding,
            output_root=args.output_root,
            start=args.start,
            end=args.end,
        )
    )
    print(manifest.model_dump_json())
    return 0


def _handle_experiment_validate(args: argparse.Namespace) -> int:
    spec = load_experiment_spec(args.spec_path)
    print(json.dumps({"valid": True, "name": spec.name, "scope": "schema-only"}, sort_keys=True))
    return 0


def _git_commit() -> str:
    repository_root = Path(__file__).resolve().parents[3]
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    if dirty.stdout:
        raise RuntimeError("tracked repository changes are present; commit or stash them before experiment run")
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _build_runner(artifact_root: Path):
    from models.database import BacktestAsyncSessionLocal

    from evosport.experiments.gateway import HomerunBacktestGateway
    from evosport.experiments.registry import SqlRunRegistry
    from evosport.experiments.runner import ExperimentRunner

    return ExperimentRunner(
        registry=SqlRunRegistry(BacktestAsyncSessionLocal),
        gateway=HomerunBacktestGateway(),
        artifact_root=artifact_root,
        homerun_commit=_git_commit(),
        evaluator_version="not-evaluated-v1",
    )


def _handle_experiment_run(args: argparse.Namespace) -> int:
    outcome = asyncio.run(_build_runner(args.artifact_root).run(args.spec_path))
    print(
        json.dumps(
            {
                "run_id": outcome.run_id,
                "fingerprint": outcome.fingerprint,
                "decision": outcome.decision,
                "report_path": str(outcome.artifact_dir / "report.html"),
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evosport",
        description="EvoSport personal sports alpha research workbench",
    )
    parser.add_argument("--version", action="version", version=f"EvoSport {__version__}")
    commands = parser.add_subparsers(dest="command")

    dataset = commands.add_parser("dataset")
    dataset_commands = dataset.add_subparsers(dest="dataset_command")
    freeze = dataset_commands.add_parser("freeze")
    freeze.add_argument("--provider-dataset-id", action="append", required=True)
    freeze.add_argument("--football-binding", required=True, type=Path)
    freeze.add_argument("--output-root", required=True, type=Path)
    freeze.add_argument("--start", required=True, type=_parse_datetime)
    freeze.add_argument("--end", required=True, type=_parse_datetime)
    freeze.set_defaults(handler=_handle_dataset_freeze)

    experiment = commands.add_parser("experiment")
    experiment_commands = experiment.add_subparsers(dest="experiment_command")
    validate = experiment_commands.add_parser("validate")
    validate.add_argument("spec_path", type=Path)
    validate.set_defaults(handler=_handle_experiment_validate)
    run = experiment_commands.add_parser("run")
    run.add_argument("spec_path", type=Path)
    run.add_argument("--artifact-root", required=True, type=Path)
    run.set_defaults(handler=_handle_experiment_run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
