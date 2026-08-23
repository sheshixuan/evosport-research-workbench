from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from evosport.cli import __main__ as cli


def _write_valid_cli_spec(tmp_path: Path) -> Path:
    strategy = tmp_path / "strategy.py"
    lock = tmp_path / "requirements.lock"
    manifest = tmp_path / "manifest.json"
    strategy.write_text("class Strategy: pass\n", encoding="utf-8")
    lock.write_text("pydantic==2.7.0\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    spec_path = tmp_path / "experiment.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "name": "cli-fixture",
                "family_id": "football-over25",
                "dataset_manifest_path": str(manifest),
                "strategy": {
                    "slug": "over25",
                    "source_path": str(strategy),
                    "dependency_lock_path": str(lock),
                    "config": {},
                },
                "window": {
                    "train_start": "2026-01-01T00:00:00Z",
                    "train_end": "2026-05-01T00:00:00Z",
                    "validation_start": "2026-05-01T00:00:00Z",
                    "validation_end": "2026-08-01T00:00:00Z",
                },
                "execution": {
                    "initial_capital_usd": 1000.0,
                    "submit_p50_ms": 50.0,
                    "submit_p95_ms": 100.0,
                    "cancel_p50_ms": 50.0,
                    "cancel_p95_ms": 100.0,
                },
                "split_method": "time",
                "max_trials": 1,
                "seed": 7,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return spec_path


def _write_documented_cli_spec(tmp_path: Path, manifest_id: str) -> Path:
    spec_path = tmp_path / "EXPERIMENT.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "name": "football-over25-noop",
                "family_id": "football-over25",
                "dataset_manifest_path": f"DATASETS/{manifest_id}/manifest.json",
                "strategy": {
                    "slug": "football-over25-noop",
                    "source_path": "strategy.py",
                    "dependency_lock_path": "evosport-requirements.lock",
                    "config": {},
                },
                "window": {
                    "train_start": "2026-01-01T00:00:00Z",
                    "train_end": "2026-05-01T00:00:00Z",
                    "validation_start": "2026-05-01T00:00:00Z",
                    "validation_end": "2026-08-01T00:00:00Z",
                },
                "execution": {
                    "initial_capital_usd": 1000.0,
                    "submit_p50_ms": 50.0,
                    "submit_p95_ms": 100.0,
                    "cancel_p50_ms": 50.0,
                    "cancel_p95_ms": 100.0,
                },
                "split_method": "time",
                "max_trials": 1,
                "seed": 7,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return spec_path


class _GitRepository:
    def __init__(self, root: Path, source: Path, head: str) -> None:
        self.root = root
        self.source = source
        self.head = head


@pytest.fixture
def git_repository(tmp_path: Path) -> _GitRepository:
    root = tmp_path / "repository"
    source = root / "backend" / "evosport" / "cli" / "__main__.py"
    source.parent.mkdir(parents=True)
    source.write_text("fixture\n", encoding="utf-8")
    for arguments in (["init"], ["config", "user.email", "test@example.com"], ["config", "user.name", "Test"]):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True, text=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    return _GitRepository(root, source, head)


def test_version_flag_prints_version(capsys):
    try:
        cli.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert capsys.readouterr().out.strip() == "EvoSport 0.1.0"


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "personal sports alpha research workbench" in capsys.readouterr().out


def test_dataset_freeze_pins_catalog_ids_and_football_settlement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_path = tmp_path / "football-binding.json"
    binding_path.write_text("{}", encoding="utf-8")
    captured = {}

    async def freeze_catalog(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(model_dump_json=lambda: '{"manifest_id":"catalog-manifest"}')

    monkeypatch.setattr(cli, "_freeze_catalog_dataset", freeze_catalog)

    assert (
        cli.main(
            [
                "dataset",
                "freeze",
                "--provider-dataset-id",
                "provider-b",
                "--provider-dataset-id",
                "provider-a",
                "--football-binding",
                str(binding_path),
                "--output-root",
                str(tmp_path / "datasets"),
                "--start",
                "2026-05-01T08:00:00+08:00",
                "--end",
                "2026-08-02T00:00:00Z",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["manifest_id"] == "catalog-manifest"
    assert captured == {
        "provider_dataset_ids": ["provider-b", "provider-a"],
        "football_binding_path": binding_path,
        "output_root": tmp_path / "datasets",
        "start": cli._parse_datetime("2026-05-01T08:00:00+08:00"),
        "end": cli._parse_datetime("2026-08-02T00:00:00Z"),
    }


def test_dataset_freeze_rejects_naive_datetime(tmp_path: Path) -> None:
    binding = tmp_path / "binding.json"
    binding.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        cli.main(
            [
                "dataset",
                "freeze",
                "--provider-dataset-id",
                "selected",
                "--football-binding",
                str(binding),
                "--output-root",
                str(tmp_path / "datasets"),
                "--start",
                "2026-05-01T00:00:00",
                "--end",
                "2026-08-02T00:00:00Z",
            ]
        )


def test_validate_is_schema_only_and_does_not_check_referenced_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = _write_valid_cli_spec(tmp_path)
    (tmp_path / "strategy.py").unlink()
    (tmp_path / "requirements.lock").unlink()
    (tmp_path / "manifest.json").unlink()

    assert cli.main(["experiment", "validate", str(spec_path)]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "name": "cli-fixture",
        "scope": "schema-only",
        "valid": True,
    }


def test_run_uses_runner_builder_and_outputs_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = _write_valid_cli_spec(tmp_path)
    artifact_root = tmp_path / "runs"
    expected_artifact_dir = artifact_root / "run-1"

    class FakeRunner:
        async def run(self, path: Path) -> SimpleNamespace:
            assert path == spec_path
            return SimpleNamespace(
                run_id="run-1",
                fingerprint="a" * 64,
                decision="NOT_EVALUATED",
                artifact_dir=expected_artifact_dir,
            )

    builder_calls: list[Path] = []

    def build_runner(path: Path) -> FakeRunner:
        builder_calls.append(path)
        return FakeRunner()

    monkeypatch.setattr(cli, "_build_runner", build_runner)

    assert cli.main(["experiment", "run", str(spec_path), "--artifact-root", str(artifact_root)]) == 0

    assert builder_calls == [artifact_root]
    assert json.loads(capsys.readouterr().out) == {
        "decision": "NOT_EVALUATED",
        "fingerprint": "a" * 64,
        "report_path": str(expected_artifact_dir / "report.html"),
        "run_id": "run-1",
    }


def test_documented_local_sequence_freezes_validates_and_dispatches_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = tmp_path / "FOOTBALL_BINDING.json"
    binding.write_text("{}", encoding="utf-8")
    dataset_root = tmp_path / "DATASETS"
    async def freeze_catalog(**kwargs):
        return SimpleNamespace(model_dump_json=lambda: '{"manifest_id":"catalog-manifest"}')

    monkeypatch.setattr(cli, "_freeze_catalog_dataset", freeze_catalog)
    assert (
        cli.main(
            [
                "dataset",
                "freeze",
                "--provider-dataset-id",
                "football-selected",
                "--football-binding",
                str(binding),
                "--output-root",
                str(dataset_root),
                "--start",
                "2026-05-01T00:00:00Z",
                "--end",
                "2026-08-02T00:00:00Z",
            ]
        )
        == 0
    )
    manifest_id = json.loads(capsys.readouterr().out)["manifest_id"]
    (tmp_path / "strategy.py").write_bytes((Path(__file__).parent / "fixtures" / "evosport" / "strategy.py").read_bytes())
    (tmp_path / "evosport-requirements.lock").write_text("pydantic==2.7.0\n", encoding="utf-8")
    spec_path = _write_documented_cli_spec(tmp_path, manifest_id)

    assert cli.main(["experiment", "validate", str(spec_path)]) == 0
    assert json.loads(capsys.readouterr().out)["scope"] == "schema-only"

    class FakeRunner:
        async def run(self, path: Path) -> SimpleNamespace:
            assert path == spec_path
            return SimpleNamespace(
                run_id="documented-run",
                fingerprint="d" * 64,
                decision="NOT_EVALUATED",
                artifact_dir=tmp_path / "RUNS" / "documented-run",
            )

    monkeypatch.setattr(cli, "_build_runner", lambda artifact_root: FakeRunner())

    assert cli.main(["experiment", "run", str(spec_path), "--artifact-root", str(tmp_path / "RUNS")]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "documented-run"


def test_validate_and_version_paths_do_not_import_database_or_backtest_runner(tmp_path: Path) -> None:
    spec_path = _write_valid_cli_spec(tmp_path)
    script = """
import builtins
import contextlib
import io
import json
import sys

forbidden = {
    \"models.database\",
    \"evosport.experiments.models\",
    \"evosport.experiments.registry\",
    \"evosport.experiments.runner\",
    \"services.backtest.unified_runner\",
}
original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name in forbidden:
        raise AssertionError(f\"forbidden import: {name}\")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import

def assert_clean(stage):
    loaded = forbidden.intersection(sys.modules)
    if loaded:
        raise AssertionError(f\"{stage} imported {sorted(loaded)}\")

from evosport.cli.__main__ import build_parser, main
assert_clean(\"import\")
build_parser()
assert_clean(\"parser\")
with contextlib.redirect_stdout(io.StringIO()):
    try:
        main([\"--version\"])
    except SystemExit as exc:
        assert exc.code == 0
assert_clean(\"version\")
with contextlib.redirect_stdout(io.StringIO()):
    main([\"experiment\", \"validate\", sys.argv[1]])
assert_clean(\"validate\")
print(json.dumps(sorted(forbidden)))
"""
    environment = os.environ | {"PYTHONPATH": str(Path(__file__).parents[1])}

    completed = subprocess.run(
        [sys.executable, "-c", script, str(spec_path)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert json.loads(completed.stdout.splitlines()[-1]) == [
        "evosport.experiments.models",
        "evosport.experiments.registry",
        "evosport.experiments.runner",
        "models.database",
        "services.backtest.unified_runner",
    ]


def test_git_commit_returns_exact_clean_head(git_repository: _GitRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "__file__", str(git_repository.source))

    assert cli._git_commit() == git_repository.head
    assert re.fullmatch(r"[0-9a-f]{40}", git_repository.head)


def test_git_commit_refuses_staged_tracked_changes(
    git_repository: _GitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_repository.source.write_text("staged\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", str(git_repository.source.relative_to(git_repository.root))],
        cwd=git_repository.root,
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setattr(cli, "__file__", str(git_repository.source))

    with pytest.raises(RuntimeError, match="commit or stash"):
        cli._git_commit()


def test_git_commit_refuses_unstaged_tracked_changes(
    git_repository: _GitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_repository.source.write_text("unstaged\n", encoding="utf-8")
    monkeypatch.setattr(cli, "__file__", str(git_repository.source))

    with pytest.raises(RuntimeError, match="commit or stash"):
        cli._git_commit()


def test_git_commit_accepts_untracked_inputs_without_changing_head(
    git_repository: _GitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    for filename in ("RAW.jsonl", "EXPERIMENT.yaml", "strategy.py"):
        (git_repository.root / filename).write_text("untracked\n", encoding="utf-8")
    monkeypatch.setattr(cli, "__file__", str(git_repository.source))

    assert cli._git_commit() == git_repository.head
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repository.root, check=True, capture_output=True, text=True
        ).stdout.strip()
        == git_repository.head
    )


def test_git_commit_propagates_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    error = subprocess.CalledProcessError(1, ["git", "status"])
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(subprocess.CalledProcessError) as raised:
        cli._git_commit()

    assert raised.value is error


def test_git_commit_propagates_non_repository_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "backend" / "evosport" / "cli" / "__main__.py"
    source.parent.mkdir(parents=True)
    source.write_text("fixture\n", encoding="utf-8")
    monkeypatch.setattr(cli, "__file__", str(source))

    with pytest.raises(subprocess.CalledProcessError):
        cli._git_commit()


def test_quickstart_contains_the_constructible_documented_sequence() -> None:
    quickstart = (Path(__file__).parents[2] / "docs" / "evosport" / "quickstart.md").read_text(encoding="utf-8")

    for required_text in (
        "manifest_id",
        "DATASETS/<manifest-id>/manifest.json",
        "--provider-dataset-id",
        "--football-binding",
        "FOOTBALL_BINDING.json",
        "class FootballOver25NoOp(BaseStrategy):",
        "def detect(self, events, markets, prices):",
        "venv/bin/python -m pip list --format=freeze > evosport-requirements.lock",
        "environment.json",
        "libomp",
        "name:",
        "family_id:",
        "dataset_manifest_path:",
        "source_path:",
        "dependency_lock_path:",
        "train_start:",
        "train_end:",
        "validation_start:",
        "validation_end:",
        "initial_capital_usd:",
        "submit_p50_ms:",
        "submit_p95_ms:",
        "cancel_p50_ms:",
        "cancel_p95_ms:",
        "split_method:",
        "max_trials:",
        "seed:",
        "ProviderDataset",
    ):
        assert required_text in quickstart
    assert "--source-file" not in quickstart
    assert "RAW.jsonl" not in quickstart


def test_oos_command_is_not_exposed() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["oos", "evaluate", "candidate"])
