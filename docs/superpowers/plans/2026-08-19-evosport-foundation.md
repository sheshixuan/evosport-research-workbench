# EvoSport Foundation and Reproducible Experiment Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the P0–P2 vertical slice that pins a Homerun baseline and turns a frozen football O/U 2.5 dataset plus a Python strategy into one reproducible Homerun backtest and a content-addressed evidence report.

**Architecture:** Use Homerun commit `c8e647f` as the pinned upstream base and add all product-specific code under `backend/evosport/`. EvoSport owns temporal sports contracts, immutable dataset manifests, experiment specifications, fingerprints, registry metadata, CLI orchestration, and reports; a narrow gateway calls Homerun's existing `run_unified_backtest` without modifying the engine.

**Tech Stack:** Python 3.10–3.13, Pydantic 2, PyYAML, SQLAlchemy 2 async, Alembic, PostgreSQL, PyArrow/Parquet, Jinja2, pytest, pytest-asyncio, Ruff, Homerun unified backtest.

**Spec:** `docs/superpowers/specs/2026-08-19-evosport-research-workbench-design.md`

## Global Constraints

- The project is a personal research tool; do not add billing, multi-tenancy, public SaaS APIs, or commercial audit features.
- The first research scope is pre-match football O/U 2.5; do not add in-play trading, additional sports, or multi-venue arbitrage.
- Keep EvoSport code under `backend/evosport/`; P0–P2 must not modify Homerun frontend, live execution, wallet, shadow, or autoresearch code.
- Call Homerun through one gateway around `services.backtest.unified_runner.run_unified_backtest`.
- No new workflow framework, Strategy DSL, Redis use, MLflow, or frontend.
- Every datetime accepted by EvoSport must be timezone-aware and normalized to UTC.
- Dataset snapshots and run artifacts are immutable and content-addressed; never overwrite a completed snapshot or run.
- P0–P2 may return `NOT_EVALUATED`; it must never emit `PASS`, `REJECT`, or claim G1–G4 completion.
- All feature code follows TDD. Run targeted tests after every red/green cycle and Homerun compatibility tests before final handoff.
- Do not enable or place live orders anywhere in this plan.

---

## Planned File Map

```text
backend/evosport/
  __init__.py                         # package version
  cli/__init__.py
  cli/__main__.py                     # argparse entry point
  domain/time.py                      # UTC temporal envelope
  domain/sports.py                    # event, contract, settlement models
  semantics/football_totals.py        # O/U settlement logic
  data/manifest.py                    # dataset manifest models and hashing
  data/freeze.py                      # atomic content-addressed snapshot writer
  experiments/spec.py                # YAML/Pydantic experiment contract
  experiments/fingerprint.py         # deterministic run identity
  experiments/gateway.py             # narrow Homerun backtest interface
  experiments/models.py              # SQLAlchemy registry tables
  experiments/registry.py            # registry protocol and implementations
  experiments/runner.py              # P0–P2 orchestration
  reports/render.py                   # JSON/HTML evidence report
  reports/templates/experiment.html.j2
backend/alembic/versions/
  202608190001_create_evosport_registry.py
backend/tests/
  fixtures/evosport/football_total_cases.json
  fixtures/evosport/strategy.py
  test_evosport_cli.py
  test_evosport_domain.py
  test_evosport_football_totals.py
  test_evosport_dataset_freeze.py
  test_evosport_experiment_spec.py
  test_evosport_gateway.py
  test_evosport_registry.py
  test_evosport_runner.py
  test_evosport_vertical_slice.py
docs/evosport/quickstart.md
```

---

### Task 1: Pin Homerun and Create the EvoSport Package Seam

**Files:**
- Create: `backend/evosport/__init__.py`
- Create: `backend/evosport/cli/__init__.py`
- Create: `backend/evosport/cli/__main__.py`
- Create: `backend/tests/test_evosport_cli.py`
- Preserve: `docs/superpowers/specs/2026-08-19-evosport-research-workbench-design.md`
- Preserve: `docs/superpowers/plans/2026-08-19-evosport-foundation.md`

**Interfaces:**
- Produces: `evosport.__version__: str`
- Produces: `evosport.cli.__main__.build_parser() -> argparse.ArgumentParser`
- Produces: `evosport.cli.__main__.main(argv: Sequence[str] | None = None) -> int`

- [ ] **Step 1: Initialize the workspace from the inspected Homerun baseline**

Run from the workspace root:

```bash
git init -b evosport-main
git remote add upstream https://github.com/braedonsaunders/homerun.git
git fetch upstream main
git cat-file -e 'c8e647f^{commit}'
git switch -c evosport/foundation c8e647f
git add docs/superpowers/specs/2026-08-19-evosport-research-workbench-design.md docs/superpowers/plans/2026-08-19-evosport-foundation.md
git commit -m "docs: add evosport research workbench design"
```

Expected: `git rev-parse HEAD` initially descends from `c8e647f`; no remote push occurs.

- [ ] **Step 2: Establish the backend environment and baseline**

Run:

```bash
make install-backend
backend/venv/bin/python -m pip install ruff
cd backend
venv/bin/python -m pytest tests/test_backtest_engine.py tests/test_backtest_settlement.py -q
```

Expected: the two pinned Homerun compatibility files pass before EvoSport code is added.

- [ ] **Step 3: Write the failing CLI test**

```python
from evosport.cli.__main__ import main


def test_version_flag_prints_version(capsys):
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert capsys.readouterr().out.strip() == "EvoSport 0.1.0"


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "personal sports alpha research workbench" in capsys.readouterr().out
```

- [ ] **Step 4: Run the test and verify red**

Run: `cd backend && venv/bin/python -m pytest tests/test_evosport_cli.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'evosport'`.

- [ ] **Step 5: Add the minimal package and CLI**

`backend/evosport/__init__.py`:

```python
__version__ = "0.1.0"
```

`backend/evosport/cli/__init__.py`:

```python
"""EvoSport command-line interface."""
```

`backend/evosport/cli/__main__.py`:

```python
from __future__ import annotations

import argparse
from collections.abc import Sequence

from evosport import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evosport",
        description="EvoSport personal sports alpha research workbench",
    )
    parser.add_argument("--version", action="version", version=f"EvoSport {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run tests and lint**

Run:

```bash
cd backend
venv/bin/python -m pytest tests/test_evosport_cli.py -v
venv/bin/python -m ruff check evosport tests/test_evosport_cli.py
```

Expected: PASS and no Ruff findings.

- [ ] **Step 7: Commit the package seam**

```bash
git add backend/evosport backend/tests/test_evosport_cli.py
git commit -m "feat: add evosport package and cli seam"
```

---

### Task 2: Add Time-Safe Sports Domain Models

**Files:**
- Create: `backend/evosport/domain/__init__.py`
- Create: `backend/evosport/domain/time.py`
- Create: `backend/evosport/domain/sports.py`
- Create: `backend/tests/test_evosport_domain.py`

**Interfaces:**
- Produces: `TemporalEnvelope`
- Produces: `CanonicalSportsEvent`
- Produces: `SettlementPolicy`
- Produces: `CanonicalSportsContract`
- Produces: enums `EventStatus`, `MarketType`, `PostponedAction`, `SettlementOutcome`

- [ ] **Step 1: Write failing timezone and invariant tests**

```python
from datetime import timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from evosport.domain.sports import CanonicalSportsContract, MarketType, SettlementPolicy
from evosport.domain.time import TemporalEnvelope


def test_temporal_envelope_rejects_naive_datetime():
    with pytest.raises(ValidationError):
        TemporalEnvelope(
            event_time=datetime(2026, 8, 1, 12),
            observed_at=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
            ingested_at=datetime(2026, 8, 1, 11, 1, tzinfo=timezone.utc),
        )


def test_temporal_envelope_rejects_observation_after_ingestion():
    with pytest.raises(ValidationError, match="observed_at cannot be after ingested_at"):
        TemporalEnvelope(
            event_time=datetime(2026, 8, 2, tzinfo=timezone.utc),
            observed_at=datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
            ingested_at=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
        )


def test_contract_preserves_venue_rule_semantics():
    contract = CanonicalSportsContract(
        contract_id="pm:epl-ars-che-over-2-5",
        event_id="football:epl:2026:ars-che",
        venue="polymarket",
        venue_market_id="market-1",
        yes_token_id="yes-1",
        no_token_id="no-1",
        market_type=MarketType.TOTAL_GOALS_OVER_UNDER,
        threshold=Decimal("2.5"),
        side="over",
        opens_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        closes_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        rule_version="sha256:rules-v1",
        settlement=SettlementPolicy(includes_extra_time=False),
    )
    assert contract.threshold == Decimal("2.5")
    assert contract.settlement.includes_extra_time is False
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd backend && venv/bin/python -m pytest tests/test_evosport_domain.py -v`

Expected: FAIL because `evosport.domain` does not exist.

- [ ] **Step 3: Implement UTC normalization and domain models**

`backend/evosport/domain/time.py`:

```python
from datetime import datetime, timezone

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator


class TemporalEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_time: AwareDatetime
    observed_at: AwareDatetime
    ingested_at: AwareDatetime
    effective_from: AwareDatetime | None = None
    effective_to: AwareDatetime | None = None
    source_revision: str | None = None
    raw_payload_hash: str | None = None

    @model_validator(mode="after")
    def validate_ordering(self) -> "TemporalEnvelope":
        for name in ("event_time", "observed_at", "ingested_at", "effective_from", "effective_to"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.observed_at > self.ingested_at:
            raise ValueError("observed_at cannot be after ingested_at")
        if self.effective_from and self.effective_to and self.effective_from >= self.effective_to:
            raise ValueError("effective_from must be before effective_to")
        return self
```

`backend/evosport/domain/sports.py`:

```python
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class EventStatus(StrEnum):
    SCHEDULED = "scheduled"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class MarketType(StrEnum):
    TOTAL_GOALS_OVER_UNDER = "total_goals_over_under"


class PostponedAction(StrEnum):
    VOID = "void"
    WAIT_FOR_RESCHEDULE = "wait_for_reschedule"


class SettlementOutcome(StrEnum):
    YES = "yes"
    NO = "no"
    VOID = "void"
    UNRESOLVED = "unresolved"


class SettlementPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    includes_extra_time: bool = False
    postponed_action: PostponedAction = PostponedAction.VOID
    cancelled_action: PostponedAction = PostponedAction.VOID
    abandoned_action: PostponedAction = PostponedAction.VOID


class CanonicalSportsEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_id: str
    sport: str = "football"
    competition: str
    season: str
    home_team: str
    away_team: str
    scheduled_start: AwareDatetime
    actual_start: AwareDatetime | None = None
    status: EventStatus = EventStatus.SCHEDULED
    source_ids: dict[str, str] = Field(default_factory=dict)


class CanonicalSportsContract(BaseModel):
    model_config = ConfigDict(frozen=True)
    contract_id: str
    event_id: str
    venue: str
    venue_market_id: str
    yes_token_id: str
    no_token_id: str
    market_type: MarketType
    threshold: Decimal
    side: str
    opens_at: AwareDatetime
    closes_at: AwareDatetime
    rule_version: str
    settlement: SettlementPolicy

    @model_validator(mode="after")
    def validate_contract(self) -> "CanonicalSportsContract":
        if self.opens_at >= self.closes_at:
            raise ValueError("opens_at must be before closes_at")
        if self.side not in {"over", "under"}:
            raise ValueError("side must be over or under")
        if self.threshold <= 0:
            raise ValueError("threshold must be positive")
        return self
```

- [ ] **Step 4: Run domain tests and lint**

Run:

```bash
cd backend
venv/bin/python -m pytest tests/test_evosport_domain.py -v
venv/bin/python -m ruff check evosport/domain tests/test_evosport_domain.py
```

Expected: PASS.

- [ ] **Step 5: Commit domain contracts**

```bash
git add backend/evosport/domain backend/tests/test_evosport_domain.py
git commit -m "feat: add time-safe sports domain contracts"
```

---

### Task 3: Implement Football O/U 2.5 Settlement Semantics

**Files:**
- Create: `backend/evosport/semantics/__init__.py`
- Create: `backend/evosport/semantics/football_totals.py`
- Create: `backend/tests/fixtures/evosport/football_total_cases.json`
- Create: `backend/tests/test_evosport_football_totals.py`

**Interfaces:**
- Consumes: `CanonicalSportsContract`, `EventStatus`, `SettlementOutcome`
- Produces: `FootballMatchResult`
- Produces: `settle_total_goals(contract, result) -> SettlementOutcome`

- [ ] **Step 1: Add fixed golden cases**

`backend/tests/fixtures/evosport/football_total_cases.json`:

```json
[
  {"name":"regulation_over","home":2,"away":1,"extra_home":0,"extra_away":0,"status":"finished","includes_extra_time":false,"expected":"yes"},
  {"name":"regulation_under","home":1,"away":1,"extra_home":0,"extra_away":0,"status":"finished","includes_extra_time":false,"expected":"no"},
  {"name":"extra_time_excluded","home":1,"away":1,"extra_home":1,"extra_away":0,"status":"finished","includes_extra_time":false,"expected":"no"},
  {"name":"extra_time_included","home":1,"away":1,"extra_home":1,"extra_away":0,"status":"finished","includes_extra_time":true,"expected":"yes"},
  {"name":"cancelled_void","home":0,"away":0,"extra_home":0,"extra_away":0,"status":"cancelled","includes_extra_time":false,"expected":"void"}
]
```

- [ ] **Step 2: Write the failing golden-case test**

```python
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from evosport.domain.sports import CanonicalSportsContract, EventStatus, MarketType, SettlementPolicy
from evosport.semantics.football_totals import FootballMatchResult, settle_total_goals


CASES = json.loads((Path(__file__).parent / "fixtures/evosport/football_total_cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_football_total_golden_cases(case):
    opens = datetime(2026, 8, 1, tzinfo=timezone.utc)
    contract = CanonicalSportsContract(
        contract_id="case", event_id="event", venue="fixture", venue_market_id="market",
        yes_token_id="yes", no_token_id="no", market_type=MarketType.TOTAL_GOALS_OVER_UNDER,
        threshold=Decimal("2.5"), side="over", opens_at=opens, closes_at=opens + timedelta(days=1),
        rule_version="fixture-v1", settlement=SettlementPolicy(includes_extra_time=case["includes_extra_time"]),
    )
    result = FootballMatchResult(
        regulation_home=case["home"], regulation_away=case["away"],
        extra_time_home=case["extra_home"], extra_time_away=case["extra_away"],
        status=EventStatus(case["status"]),
    )
    assert settle_total_goals(contract, result).value == case["expected"]
```

- [ ] **Step 3: Run the test and verify red**

Run: `cd backend && venv/bin/python -m pytest tests/test_evosport_football_totals.py -v`

Expected: FAIL because the semantics module is missing.

- [ ] **Step 4: Implement deterministic settlement**

```python
from pydantic import BaseModel, ConfigDict, Field

from evosport.domain.sports import (
    CanonicalSportsContract,
    EventStatus,
    PostponedAction,
    SettlementOutcome,
)


class FootballMatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    regulation_home: int = Field(ge=0)
    regulation_away: int = Field(ge=0)
    extra_time_home: int = Field(default=0, ge=0)
    extra_time_away: int = Field(default=0, ge=0)
    status: EventStatus


def settle_total_goals(
    contract: CanonicalSportsContract,
    result: FootballMatchResult,
) -> SettlementOutcome:
    if result.status == EventStatus.SCHEDULED or result.status == EventStatus.POSTPONED:
        action = contract.settlement.postponed_action
        return SettlementOutcome.VOID if action == PostponedAction.VOID else SettlementOutcome.UNRESOLVED
    if result.status == EventStatus.CANCELLED:
        action = contract.settlement.cancelled_action
        return SettlementOutcome.VOID if action == PostponedAction.VOID else SettlementOutcome.UNRESOLVED
    if result.status == EventStatus.ABANDONED:
        action = contract.settlement.abandoned_action
        return SettlementOutcome.VOID if action == PostponedAction.VOID else SettlementOutcome.UNRESOLVED
    total = result.regulation_home + result.regulation_away
    if contract.settlement.includes_extra_time:
        total += result.extra_time_home + result.extra_time_away
    is_yes = total > contract.threshold if contract.side == "over" else total < contract.threshold
    return SettlementOutcome.YES if is_yes else SettlementOutcome.NO
```

- [ ] **Step 5: Run tests and commit**

```bash
cd backend
venv/bin/python -m pytest tests/test_evosport_football_totals.py -v
venv/bin/python -m ruff check evosport/semantics tests/test_evosport_football_totals.py
cd ..
git add backend/evosport/semantics backend/tests/fixtures/evosport backend/tests/test_evosport_football_totals.py
git commit -m "feat: add football totals settlement semantics"
```

Expected: PASS.

---

### Task 4: Freeze Immutable Content-Addressed Datasets

**Files:**
- Create: `backend/evosport/data/__init__.py`
- Create: `backend/evosport/data/manifest.py`
- Create: `backend/evosport/data/freeze.py`
- Create: `backend/tests/test_evosport_dataset_freeze.py`

**Interfaces:**
- Produces: `DatasetFile`, `DatasetManifest`
- Produces: `sha256_file(path: Path) -> str`
- Produces: `freeze_dataset(source_files, output_root, source, token_ids, start, end) -> DatasetManifest`
- Produces: `load_manifest(path: Path) -> DatasetManifest`

- [ ] **Step 1: Write failing snapshot tests**

```python
from datetime import datetime, timezone

from evosport.data.freeze import freeze_dataset, load_manifest


def test_freeze_is_content_addressed_and_idempotent(tmp_path):
    source = tmp_path / "raw.jsonl"
    source.write_text('{"event":"a"}\n', encoding="utf-8")
    root = tmp_path / "datasets"
    kwargs = dict(
        source_files=[source], output_root=root, source="fixture",
        token_ids=["yes-1", "no-1"],
        start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        end=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )
    first = freeze_dataset(**kwargs)
    second = freeze_dataset(**kwargs)
    assert first.manifest_id == second.manifest_id
    frozen = root / first.manifest_id / "files/raw.jsonl"
    assert frozen.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert load_manifest(root / first.manifest_id / "manifest.json") == first


def test_changed_bytes_create_a_new_snapshot(tmp_path):
    source = tmp_path / "raw.jsonl"
    source.write_text("one", encoding="utf-8")
    root = tmp_path / "datasets"
    common = dict(source_files=[source], output_root=root, source="fixture", token_ids=["t"],
                  start=datetime(2026, 8, 1, tzinfo=timezone.utc),
                  end=datetime(2026, 8, 2, tzinfo=timezone.utc))
    first = freeze_dataset(**common)
    source.write_text("two", encoding="utf-8")
    second = freeze_dataset(**common)
    assert first.manifest_id != second.manifest_id
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd backend && venv/bin/python -m pytest tests/test_evosport_dataset_freeze.py -v`

Expected: FAIL because `evosport.data` is missing.

- [ ] **Step 3: Implement manifest models and canonical hashing**

```python
import hashlib
import json
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict


class DatasetFile(BaseModel):
    model_config = ConfigDict(frozen=True)
    relative_path: str
    sha256: str
    size_bytes: int


class DatasetManifest(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: str = "evosport.dataset.v1"
    manifest_id: str
    source: str
    token_ids: tuple[str, ...]
    start: AwareDatetime
    end: AwareDatetime
    files: tuple[DatasetFile, ...]


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
```

- [ ] **Step 4: Implement atomic freeze and manifest load**

```python
import hashlib
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from collections.abc import Sequence

from evosport.data.manifest import DatasetFile, DatasetManifest, canonical_json, sha256_file


def freeze_dataset(*, source_files: Sequence[Path], output_root: Path, source: str,
                   token_ids: Sequence[str], start: datetime, end: datetime) -> DatasetManifest:
    if not source_files:
        raise ValueError("source_files cannot be empty")
    if start >= end:
        raise ValueError("start must be before end")
    names = [path.name for path in source_files]
    if len(names) != len(set(names)):
        raise ValueError("source file basenames must be unique")
    files = tuple(
        DatasetFile(relative_path=f"files/{path.name}", sha256=sha256_file(path), size_bytes=path.stat().st_size)
        for path in sorted(source_files, key=lambda item: item.name)
    )
    identity = {
        "schema_version": "evosport.dataset.v1", "source": source,
        "token_ids": sorted(set(token_ids)), "start": start.isoformat(), "end": end.isoformat(),
        "files": [item.model_dump(mode="json") for item in files],
    }
    manifest_id = hashlib.sha256(canonical_json(identity)).hexdigest()
    manifest = DatasetManifest(manifest_id=manifest_id, source=source, token_ids=tuple(identity["token_ids"]),
                               start=start, end=end, files=files)
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / manifest_id
    if target.exists():
        existing = load_manifest(target / "manifest.json")
        if existing != manifest:
            raise RuntimeError(f"snapshot collision at {target}")
        return existing
    staging = Path(tempfile.mkdtemp(prefix=f".{manifest_id}.", dir=output_root))
    try:
        (staging / "files").mkdir()
        for source_path, file_record in zip(sorted(source_files, key=lambda item: item.name), files, strict=True):
            destination = staging / file_record.relative_path
            shutil.copyfile(source_path, destination)
            os.chmod(destination, 0o444)
        (staging / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def load_manifest(path: Path) -> DatasetManifest:
    return DatasetManifest.model_validate_json(path.read_text(encoding="utf-8"))
```

- [ ] **Step 5: Run tests, lint, and commit**

```bash
cd backend
venv/bin/python -m pytest tests/test_evosport_dataset_freeze.py -v
venv/bin/python -m ruff check evosport/data tests/test_evosport_dataset_freeze.py
cd ..
git add backend/evosport/data backend/tests/test_evosport_dataset_freeze.py
git commit -m "feat: add immutable dataset snapshots"
```

Expected: PASS.

---

### Task 5: Define Experiment Specs and Deterministic Fingerprints

**Files:**
- Create: `backend/evosport/experiments/__init__.py`
- Create: `backend/evosport/experiments/spec.py`
- Create: `backend/evosport/experiments/fingerprint.py`
- Create: `backend/tests/test_evosport_experiment_spec.py`

**Interfaces:**
- Produces: `TimeWindow`, `StrategyPackageSpec`, `ExecutionModelSpec`, `ExperimentSpec`
- Produces: `load_experiment_spec(path: Path) -> ExperimentSpec`
- Produces: `compute_run_fingerprint(...) -> str`

- [ ] **Step 1: Write failing spec and fingerprint tests**

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from evosport.experiments.fingerprint import compute_run_fingerprint
from evosport.experiments.spec import ExperimentSpec, load_experiment_spec


def test_spec_rejects_non_time_split(valid_spec_dict):
    valid_spec_dict["split_method"] = "random"
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(valid_spec_dict)


def test_yaml_round_trip_and_stable_fingerprint(tmp_path, valid_spec_dict):
    path = tmp_path / "experiment.yaml"
    path.write_text(__import__("yaml").safe_dump(valid_spec_dict), encoding="utf-8")
    spec = load_experiment_spec(path)
    first = compute_run_fingerprint(spec=spec, strategy_bytes=b"strategy", lock_bytes=b"lock",
                                    manifest_bytes=b"manifest", evaluator_version="none",
                                    homerun_commit="c8e647f")
    second = compute_run_fingerprint(spec=spec, strategy_bytes=b"strategy", lock_bytes=b"lock",
                                     manifest_bytes=b"manifest", evaluator_version="none",
                                     homerun_commit="c8e647f")
    assert first == second
    assert len(first) == 64
```

Add this fixture above the tests:

```python
@pytest.fixture
def valid_spec_dict(tmp_path):
    strategy = tmp_path / "strategy.py"
    lock = tmp_path / "requirements.lock"
    manifest = tmp_path / "manifest.json"
    strategy.write_text("class Strategy: pass\n", encoding="utf-8")
    lock.write_text("pydantic==2.7.0\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    return {
        "name": "football-over25-v001",
        "family_id": "football-over25",
        "dataset_manifest_path": str(manifest),
        "strategy": {
            "slug": "football_over25_v001",
            "source_path": str(strategy),
            "dependency_lock_path": str(lock),
            "config": {"minimum_edge": 0.03},
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
    }
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd backend && venv/bin/python -m pytest tests/test_evosport_experiment_spec.py -v`

Expected: FAIL because experiment modules are missing.

- [ ] **Step 3: Implement the pre-registered spec**

```python
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class TimeWindow(BaseModel):
    model_config = ConfigDict(frozen=True)
    train_start: AwareDatetime
    train_end: AwareDatetime
    validation_start: AwareDatetime
    validation_end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> "TimeWindow":
        if not self.train_start < self.train_end <= self.validation_start < self.validation_end:
            raise ValueError("time windows must be ordered and non-overlapping")
        return self


class StrategyPackageSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    slug: str
    source_path: Path
    dependency_lock_path: Path
    config: dict = Field(default_factory=dict)


class ExecutionModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    initial_capital_usd: float = Field(gt=0)
    submit_p50_ms: float = Field(ge=0)
    submit_p95_ms: float = Field(ge=0)
    cancel_p50_ms: float = Field(ge=0)
    cancel_p95_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def percentiles_are_ordered(self) -> "ExecutionModelSpec":
        if self.submit_p95_ms < self.submit_p50_ms:
            raise ValueError("submit_p95_ms cannot be below submit_p50_ms")
        if self.cancel_p95_ms < self.cancel_p50_ms:
            raise ValueError("cancel_p95_ms cannot be below cancel_p50_ms")
        return self


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    family_id: str
    dataset_manifest_path: Path
    strategy: StrategyPackageSpec
    window: TimeWindow
    execution: ExecutionModelSpec
    split_method: Literal["time"] = "time"
    max_trials: int = Field(ge=1)
    seed: int
    hidden_oos_manifest_id: str | None = None


def load_experiment_spec(path: Path) -> ExperimentSpec:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("experiment YAML must contain a mapping")
    return ExperimentSpec.model_validate(payload)
```

- [ ] **Step 4: Implement canonical fingerprinting**

```python
import hashlib

from evosport.data.manifest import canonical_json
from evosport.experiments.spec import ExperimentSpec


def compute_run_fingerprint(*, spec: ExperimentSpec, strategy_bytes: bytes, lock_bytes: bytes,
                            manifest_bytes: bytes, evaluator_version: str, homerun_commit: str) -> str:
    spec_payload = spec.model_dump(mode="json")
    spec_payload.pop("dataset_manifest_path")
    spec_payload["strategy"].pop("source_path")
    spec_payload["strategy"].pop("dependency_lock_path")
    payload = {
        "spec": spec_payload,
        "strategy_sha256": hashlib.sha256(strategy_bytes).hexdigest(),
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "evaluator_version": evaluator_version,
        "homerun_commit": homerun_commit,
        "seed": spec.seed,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()
```

- [ ] **Step 5: Run tests and commit**

```bash
cd backend
venv/bin/python -m pytest tests/test_evosport_experiment_spec.py -v
venv/bin/python -m ruff check evosport/experiments tests/test_evosport_experiment_spec.py
cd ..
git add backend/evosport/experiments backend/tests/test_evosport_experiment_spec.py
git commit -m "feat: add reproducible experiment specifications"
```

Expected: PASS.

---

### Task 6: Add the Narrow Homerun Backtest Gateway

**Files:**
- Create: `backend/evosport/experiments/gateway.py`
- Create: `backend/tests/test_evosport_gateway.py`

**Interfaces:**
- Consumes: `ExperimentSpec`, `DatasetManifest`, strategy source bytes
- Produces: `BacktestRequest`
- Produces: protocol `BacktestGateway.run(request: BacktestRequest) -> dict[str, Any]`
- Produces: `HomerunBacktestGateway`

- [ ] **Step 1: Write a failing adapter contract test**

```python
from unittest.mock import AsyncMock

import pytest

from evosport.experiments.gateway import BacktestRequest, HomerunBacktestGateway


@pytest.mark.asyncio
async def test_gateway_maps_only_unified_backtest_fields():
    runner = AsyncMock(return_value={"run_id": "hr-1", "execution": {"trade_count": 2}})
    request = BacktestRequest(
        source_code="class Strategy: pass", slug="over25", config={"edge": 0.03},
        token_ids=("yes", "no"), start="2026-08-01T00:00:00+00:00",
        end="2026-08-02T00:00:00+00:00", initial_capital_usd=1000.0,
        submit_p50_ms=50.0, submit_p95_ms=100.0, cancel_p50_ms=50.0,
        cancel_p95_ms=100.0, seed=7, n_trials=3,
    )
    result = await HomerunBacktestGateway(runner=runner).run(request)
    assert result["run_id"] == "hr-1"
    runner.assert_awaited_once_with(
        source_code=request.source_code, slug="over25", config={"edge": 0.03},
        token_ids=["yes", "no"], start=request.start_datetime, end=request.end_datetime,
        initial_capital_usd=1000.0, submit_p50_ms=50.0, submit_p95_ms=100.0,
        cancel_p50_ms=50.0, cancel_p95_ms=100.0, seed=7, n_trials=3,
    )
```

- [ ] **Step 2: Run the test and verify red**

Run: `cd backend && venv/bin/python -m pytest tests/test_evosport_gateway.py -v`

Expected: FAIL because `gateway.py` does not exist.

- [ ] **Step 3: Implement the gateway with lazy Homerun import**

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol


@dataclass(frozen=True)
class BacktestRequest:
    source_code: str
    slug: str
    config: dict[str, Any]
    token_ids: tuple[str, ...]
    start: str
    end: str
    initial_capital_usd: float
    submit_p50_ms: float
    submit_p95_ms: float
    cancel_p50_ms: float
    cancel_p95_ms: float
    seed: int
    n_trials: int

    @property
    def start_datetime(self) -> datetime:
        return datetime.fromisoformat(self.start)

    @property
    def end_datetime(self) -> datetime:
        return datetime.fromisoformat(self.end)


class BacktestGateway(Protocol):
    async def run(self, request: BacktestRequest) -> dict[str, Any]: ...


Runner = Callable[..., Awaitable[dict[str, Any]]]


class HomerunBacktestGateway:
    def __init__(self, runner: Runner | None = None) -> None:
        if runner is None:
            from services.backtest.unified_runner import run_unified_backtest
            runner = run_unified_backtest
        self._runner = runner

    async def run(self, request: BacktestRequest) -> dict[str, Any]:
        return await self._runner(
            source_code=request.source_code, slug=request.slug, config=request.config,
            token_ids=list(request.token_ids), start=request.start_datetime, end=request.end_datetime,
            initial_capital_usd=request.initial_capital_usd,
            submit_p50_ms=request.submit_p50_ms, submit_p95_ms=request.submit_p95_ms,
            cancel_p50_ms=request.cancel_p50_ms, cancel_p95_ms=request.cancel_p95_ms,
            seed=request.seed, n_trials=request.n_trials,
        )
```

- [ ] **Step 4: Run the gateway and compatibility tests**

```bash
cd backend
venv/bin/python -m pytest tests/test_evosport_gateway.py tests/test_backtest_engine.py -v
venv/bin/python -m ruff check evosport/experiments/gateway.py tests/test_evosport_gateway.py
```

Expected: PASS; no Homerun engine file is modified.

- [ ] **Step 5: Commit the gateway**

```bash
git add backend/evosport/experiments/gateway.py backend/tests/test_evosport_gateway.py
git commit -m "feat: add homerun backtest gateway"
```

---

### Task 7: Persist EvoSport Run and Trial Metadata

**Files:**
- Create: `backend/evosport/experiments/models.py`
- Create: `backend/evosport/experiments/registry.py`
- Create: `backend/alembic/versions/202608190001_create_evosport_registry.py`
- Create: `backend/tests/test_evosport_registry.py`

**Interfaces:**
- Produces: SQLAlchemy models `ExperimentRun`, `TrialRecord`
- Produces: `RunRecord`
- Produces: protocol `RunRegistry`
- Produces: `InMemoryRunRegistry`, `SqlRunRegistry`
- Methods: `get_by_fingerprint`, `create`, `mark_running`, `mark_succeeded`, `mark_failed`

- [ ] **Step 1: Write failing schema and registry-state tests**

```python
import pytest

from evosport.experiments.models import ExperimentRun, TrialRecord
from evosport.experiments.registry import InMemoryRunRegistry


def test_registry_tables_are_isolated_in_evosport_schema():
    assert ExperimentRun.__table__.schema == "evosport"
    assert TrialRecord.__table__.schema == "evosport"
    assert {"fingerprint", "status", "spec_json", "dataset_manifest_id"} <= set(ExperimentRun.__table__.columns.keys())


@pytest.mark.asyncio
async def test_in_memory_registry_preserves_terminal_run():
    registry = InMemoryRunRegistry()
    row = await registry.create(fingerprint="abc", spec_json={"name": "x"}, dataset_manifest_id="dataset")
    await registry.mark_running(row.id)
    await registry.mark_succeeded(row.id, homerun_run_id="hr-1", result_json={"ok": True})
    stored = await registry.get_by_fingerprint("abc")
    assert stored is not None
    assert stored.status == "SUCCEEDED"
    assert stored.homerun_run_id == "hr-1"
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd backend && venv/bin/python -m pytest tests/test_evosport_registry.py -v`

Expected: FAIL because models and registry are missing.

- [ ] **Step 3: Add registry models**

Use Homerun's `models.database.Base`; do not add classes to the 6,000-line `models/database.py`.

```python
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint

from models.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_evosport_run_fingerprint"), {"schema": "evosport"})
    id = Column(String, primary_key=True)
    fingerprint = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False)
    spec_json = Column(JSON, nullable=False)
    dataset_manifest_id = Column(String(64), nullable=False)
    homerun_run_id = Column(String, nullable=True)
    result_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class TrialRecord(Base):
    __tablename__ = "trial_records"
    __table_args__ = (
        UniqueConstraint("run_id", "trial_number", name="uq_evosport_trial_number"),
        {"schema": "evosport"},
    )
    id = Column(String, primary_key=True)
    run_id = Column(String, ForeignKey("evosport.experiment_runs.id", ondelete="CASCADE"), nullable=False)
    family_id = Column(String, nullable=False)
    trial_number = Column(Integer, nullable=False)
    strategy_hash = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False)
    metrics_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
```

- [ ] **Step 4: Implement registry protocol and in-memory/SQL implementations**

Start `registry.py` with these exact public types:

```python
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select

from evosport.experiments.models import ExperimentRun


@dataclass(frozen=True)
class RunRecord:
    id: str
    fingerprint: str
    status: str
    spec_json: dict[str, Any]
    dataset_manifest_id: str
    homerun_run_id: str | None = None
    result_json: dict[str, Any] | None = None
    error: str | None = None


class RunRegistry(Protocol):
    async def get_by_fingerprint(self, fingerprint: str) -> RunRecord | None: ...
    async def create(self, *, fingerprint: str, spec_json: dict[str, Any], dataset_manifest_id: str) -> RunRecord: ...
    async def mark_running(self, run_id: str) -> RunRecord: ...
    async def mark_succeeded(self, run_id: str, *, homerun_run_id: str, result_json: dict[str, Any]) -> RunRecord: ...
    async def mark_failed(self, run_id: str, *, error: str) -> RunRecord: ...


_ALLOWED = {
    "CREATED": {"RUNNING", "FAILED"},
    "RUNNING": {"SUCCEEDED", "FAILED"},
    "SUCCEEDED": set(),
    "FAILED": set(),
}


def _transition(record: RunRecord, status: str, **updates: Any) -> RunRecord:
    if status not in _ALLOWED[record.status]:
        raise ValueError(f"invalid run transition {record.status} -> {status}")
    return replace(record, status=status, **updates)
```

Implement the deterministic test registry as follows:

```python
class InMemoryRunRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, RunRecord] = {}
        self._id_by_fingerprint: dict[str, str] = {}

    async def get_by_fingerprint(self, fingerprint: str) -> RunRecord | None:
        run_id = self._id_by_fingerprint.get(fingerprint)
        return self._by_id.get(run_id) if run_id else None

    async def create(self, *, fingerprint: str, spec_json: dict[str, Any], dataset_manifest_id: str) -> RunRecord:
        if fingerprint in self._id_by_fingerprint:
            raise ValueError("fingerprint already registered")
        row = RunRecord(
            id=uuid.uuid4().hex, fingerprint=fingerprint, status="CREATED",
            spec_json=spec_json, dataset_manifest_id=dataset_manifest_id,
        )
        self._by_id[row.id] = row
        self._id_by_fingerprint[fingerprint] = row.id
        return row

    async def mark_running(self, run_id: str) -> RunRecord:
        return self._store(_transition(self._by_id[run_id], "RUNNING"))

    async def mark_succeeded(self, run_id: str, *, homerun_run_id: str, result_json: dict[str, Any]) -> RunRecord:
        return self._store(_transition(
            self._by_id[run_id], "SUCCEEDED",
            homerun_run_id=homerun_run_id, result_json=result_json,
        ))

    async def mark_failed(self, run_id: str, *, error: str) -> RunRecord:
        return self._store(_transition(self._by_id[run_id], "FAILED", error=error))

    def _store(self, row: RunRecord) -> RunRecord:
        self._by_id[row.id] = row
        return row
```

`SqlRunRegistry` receives a Homerun async `session_factory` and uses this implementation:

```python
def _to_record(row: ExperimentRun) -> RunRecord:
    return RunRecord(
        id=row.id, fingerprint=row.fingerprint, status=row.status,
        spec_json=dict(row.spec_json or {}), dataset_manifest_id=row.dataset_manifest_id,
        homerun_run_id=row.homerun_run_id,
        result_json=dict(row.result_json) if row.result_json is not None else None,
        error=row.error,
    )

class SqlRunRegistry:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def get_by_fingerprint(self, fingerprint: str) -> RunRecord | None:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(ExperimentRun).where(ExperimentRun.fingerprint == fingerprint)
            )).scalar_one_or_none()
            return _to_record(row) if row is not None else None

    async def create(self, *, fingerprint: str, spec_json: dict[str, Any], dataset_manifest_id: str) -> RunRecord:
        async with self._session_factory() as session:
            now = datetime.now(timezone.utc)
            row = ExperimentRun(
                id=uuid.uuid4().hex, fingerprint=fingerprint, status="CREATED",
                spec_json=spec_json, dataset_manifest_id=dataset_manifest_id,
                created_at=now, updated_at=now,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_record(row)

    async def mark_running(self, run_id: str) -> RunRecord:
        return await self._set_status(run_id, "RUNNING")

    async def mark_succeeded(self, run_id: str, *, homerun_run_id: str, result_json: dict[str, Any]) -> RunRecord:
        return await self._set_status(
            run_id, "SUCCEEDED", homerun_run_id=homerun_run_id, result_json=result_json,
        )

    async def mark_failed(self, run_id: str, *, error: str) -> RunRecord:
        return await self._set_status(run_id, "FAILED", error=error)

    async def _set_status(self, run_id: str, status: str, **updates: Any) -> RunRecord:
        async with self._session_factory() as session:
            row = await session.get(ExperimentRun, run_id)
            if row is None:
                raise KeyError(run_id)
            next_record = _transition(_to_record(row), status, **updates)
            row.status = next_record.status
            row.homerun_run_id = next_record.homerun_run_id
            row.result_json = next_record.result_json
            row.error = next_record.error
            row.updated_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(row)
            return _to_record(row)
```

State transitions are enforced:

```python
_ALLOWED = {
    "CREATED": {"RUNNING", "FAILED"},
    "RUNNING": {"SUCCEEDED", "FAILED"},
    "SUCCEEDED": set(),
    "FAILED": set(),
}
```

Invalid transitions raise `ValueError` and never update storage.

- [ ] **Step 5: Add the explicit Alembic migration**

Use this complete migration shape, with the standard Alembic imports and revision metadata:

```python
from alembic import op
import sqlalchemy as sa


revision = "202608190001"
down_revision = "202606160003"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS evosport")
    op.create_table(
        "experiment_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("dataset_manifest_id", sa.String(length=64), nullable=False),
        sa.Column("homerun_run_id", sa.String(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="evosport",
    )
    op.create_unique_constraint("uq_evosport_run_fingerprint", "experiment_runs", ["fingerprint"], schema="evosport")
    op.create_table(
        "trial_records",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("family_id", sa.String(), nullable=False),
        sa.Column("trial_number", sa.Integer(), nullable=False),
        sa.Column("strategy_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["evosport.experiment_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "trial_number", name="uq_evosport_trial_number"),
        schema="evosport",
    )


def downgrade() -> None:
    op.drop_table("trial_records", schema="evosport")
    op.drop_table("experiment_runs", schema="evosport")
    op.execute("DROP SCHEMA IF EXISTS evosport")
```

- [ ] **Step 6: Run unit tests, migration-head check, and lint**

```bash
cd backend
venv/bin/python -m pytest tests/test_evosport_registry.py -v
venv/bin/alembic heads
venv/bin/python -m ruff check evosport/experiments/models.py evosport/experiments/registry.py tests/test_evosport_registry.py
```

Expected: tests pass and Alembic reports only `202608190001 (head)`.

- [ ] **Step 7: Commit registry storage**

```bash
git add backend/evosport/experiments/models.py backend/evosport/experiments/registry.py backend/alembic/versions/202608190001_create_evosport_registry.py backend/tests/test_evosport_registry.py
git commit -m "feat: add evosport experiment registry"
```

---

### Task 8: Orchestrate a Reproducible Run and Render Evidence Artifacts

**Files:**
- Create: `backend/evosport/experiments/runner.py`
- Create: `backend/evosport/reports/__init__.py`
- Create: `backend/evosport/reports/render.py`
- Create: `backend/evosport/reports/templates/experiment.html.j2`
- Create: `backend/tests/test_evosport_runner.py`

**Interfaces:**
- Consumes: `ExperimentSpec`, `DatasetManifest`, `RunRegistry`, `BacktestGateway`
- Produces: `ExperimentOutcome`
- Produces: `ExperimentRunner.run(spec_path: Path) -> ExperimentOutcome`
- Produces: `render_report(outcome, artifact_dir) -> Path`

- [ ] **Step 1: Write the failing orchestration test**

```python
from unittest.mock import AsyncMock
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from evosport.data.freeze import freeze_dataset
from evosport.experiments.registry import InMemoryRunRegistry
from evosport.experiments.runner import ExperimentRunner


@dataclass(frozen=True)
class VerticalSliceFixture:
    spec_path: Path
    artifact_root: Path


@pytest.fixture
def vertical_slice_fixture(tmp_path):
    raw = tmp_path / "raw.jsonl"
    raw.write_text('{"event":"match-1"}\n', encoding="utf-8")
    manifest = freeze_dataset(
        source_files=[raw], output_root=tmp_path / "datasets", source="fixture",
        token_ids=["yes", "no"], start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )
    manifest_path = tmp_path / "datasets" / manifest.manifest_id / "manifest.json"
    strategy = tmp_path / "strategy.py"
    lock = tmp_path / "requirements.lock"
    strategy.write_text("class Strategy: pass\n", encoding="utf-8")
    lock.write_text("pydantic==2.7.0\n", encoding="utf-8")
    spec_path = tmp_path / "experiment.yaml"
    spec_path.write_text(yaml.safe_dump({
        "name": "fixture", "family_id": "football-over25",
        "dataset_manifest_path": str(manifest_path),
        "strategy": {"slug": "over25", "source_path": str(strategy),
                     "dependency_lock_path": str(lock), "config": {}},
        "window": {"train_start": "2026-01-01T00:00:00Z", "train_end": "2026-05-01T00:00:00Z",
                   "validation_start": "2026-05-01T00:00:00Z", "validation_end": "2026-08-01T00:00:00Z"},
        "execution": {"initial_capital_usd": 1000.0, "submit_p50_ms": 50.0,
                      "submit_p95_ms": 100.0, "cancel_p50_ms": 50.0, "cancel_p95_ms": 100.0},
        "split_method": "time", "max_trials": 1, "seed": 7,
    }), encoding="utf-8")
    return VerticalSliceFixture(spec_path=spec_path, artifact_root=tmp_path / "runs")


@pytest.mark.asyncio
async def test_runner_caches_same_fingerprint_and_emits_not_evaluated_artifacts(vertical_slice_fixture):
    gateway = AsyncMock()
    gateway.run.return_value = {"run_id": "hr-1", "execution": {"trade_count": 2, "total_return_pct": 1.5}}
    runner = ExperimentRunner(
        registry=InMemoryRunRegistry(), gateway=gateway,
        artifact_root=vertical_slice_fixture.artifact_root,
        homerun_commit="c8e647f", evaluator_version="not-evaluated-v1",
    )
    first = await runner.run(vertical_slice_fixture.spec_path)
    second = await runner.run(vertical_slice_fixture.spec_path)
    assert first.run_id == second.run_id
    assert first.decision == "NOT_EVALUATED"
    assert (first.artifact_dir / "result.json").exists()
    assert (first.artifact_dir / "decision.json").read_text().find("NOT_EVALUATED") >= 0
    assert (first.artifact_dir / "report.html").exists()
    gateway.run.assert_awaited_once()
```

- [ ] **Step 2: Run the test and verify red**

Run: `cd backend && venv/bin/python -m pytest tests/test_evosport_runner.py -v`

Expected: FAIL because `runner.py` is missing.

- [ ] **Step 3: Implement the outcome and runner**

Implement these types and the orchestration body. Split private helpers (`_resolve`, `_verify_snapshot`, `_write_json`) below the class so `run` remains readable.

```python
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evosport.data.freeze import load_manifest
from evosport.data.manifest import sha256_file
from evosport.experiments.fingerprint import compute_run_fingerprint
from evosport.experiments.gateway import BacktestGateway, BacktestRequest
from evosport.experiments.registry import RunRecord, RunRegistry
from evosport.experiments.spec import load_experiment_spec
from evosport.reports.render import render_report


_DECISION = {
    "decision": "NOT_EVALUATED",
    "reason": "P0-P2 records reproducible backtests; Evaluation Gate G1-G4 is not installed",
}


@dataclass(frozen=True)
class ExperimentOutcome:
    run_id: str
    fingerprint: str
    status: str
    decision: str
    dataset_manifest_id: str
    homerun_run_id: str
    artifact_dir: Path
    result: dict[str, Any]


class ExperimentRunner:
    def __init__(self, *, registry: RunRegistry, gateway: BacktestGateway, artifact_root: Path,
                 homerun_commit: str, evaluator_version: str) -> None:
        self._registry = registry
        self._gateway = gateway
        self._artifact_root = artifact_root
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        self._homerun_commit = homerun_commit
        self._evaluator_version = evaluator_version

    async def run(self, spec_path: Path) -> ExperimentOutcome:
        spec_path = spec_path.resolve()
        spec = load_experiment_spec(spec_path)
        manifest_path = _resolve(spec_path.parent, spec.dataset_manifest_path)
        source_path = _resolve(spec_path.parent, spec.strategy.source_path)
        lock_path = _resolve(spec_path.parent, spec.strategy.dependency_lock_path)
        manifest = load_manifest(manifest_path)
        _verify_snapshot(manifest_path.parent, manifest)
        strategy_bytes = source_path.read_bytes()
        lock_bytes = lock_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        fingerprint = compute_run_fingerprint(
            spec=spec, strategy_bytes=strategy_bytes, lock_bytes=lock_bytes,
            manifest_bytes=manifest_bytes, evaluator_version=self._evaluator_version,
            homerun_commit=self._homerun_commit,
        )
        cached = await self._registry.get_by_fingerprint(fingerprint)
        if cached is not None:
            if cached.status != "SUCCEEDED" or cached.result_json is None or cached.homerun_run_id is None:
                raise RuntimeError(f"fingerprint {fingerprint} already has non-terminal run {cached.id}")
            return _outcome(cached, self._artifact_root / cached.id)

        row = await self._registry.create(
            fingerprint=fingerprint, spec_json=spec.model_dump(mode="json"),
            dataset_manifest_id=manifest.manifest_id,
        )
        row = await self._registry.mark_running(row.id)
        try:
            request = BacktestRequest(
                source_code=strategy_bytes.decode("utf-8"), slug=spec.strategy.slug,
                config=spec.strategy.config, token_ids=manifest.token_ids,
                start=spec.window.validation_start.isoformat(), end=spec.window.validation_end.isoformat(),
                initial_capital_usd=spec.execution.initial_capital_usd,
                submit_p50_ms=spec.execution.submit_p50_ms,
                submit_p95_ms=spec.execution.submit_p95_ms,
                cancel_p50_ms=spec.execution.cancel_p50_ms,
                cancel_p95_ms=spec.execution.cancel_p95_ms,
                seed=spec.seed, n_trials=spec.max_trials,
            )
            result = await self._gateway.run(request)
            homerun_run_id = str(result["run_id"])
            artifact_dir = self._artifact_root / row.id
            staging = Path(tempfile.mkdtemp(prefix=f".{row.id}.", dir=self._artifact_root))
            try:
                (staging / "strategy-package").mkdir()
                shutil.copyfile(spec_path, staging / "experiment.yaml")
                shutil.copyfile(manifest_path, staging / "dataset-manifest.json")
                shutil.copyfile(source_path, staging / "strategy-package" / source_path.name)
                shutil.copyfile(lock_path, staging / "environment.lock")
                _write_json(staging / "result.json", result)
                _write_json(staging / "decision.json", _DECISION)
                render_report(
                    run_id=row.id, fingerprint=fingerprint, manifest=manifest,
                    homerun_run_id=homerun_run_id, result=result,
                    decision=_DECISION, artifact_dir=staging,
                )
                if artifact_dir.exists():
                    raise RuntimeError(f"artifact directory already exists: {artifact_dir}")
                os.replace(staging, artifact_dir)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            row = await self._registry.mark_succeeded(
                row.id, homerun_run_id=homerun_run_id, result_json=result,
            )
            return _outcome(row, artifact_dir)
        except Exception as exc:
            await self._registry.mark_failed(row.id, error=str(exc))
            raise


def _resolve(base: Path, path: Path) -> Path:
    return path if path.is_absolute() else (base / path).resolve()


def _verify_snapshot(root: Path, manifest) -> None:
    for item in manifest.files:
        path = root / item.relative_path
        if not path.is_file() or sha256_file(path) != item.sha256:
            raise ValueError(f"dataset snapshot hash mismatch: {item.relative_path}")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str), encoding="utf-8")


def _outcome(row: RunRecord, artifact_dir: Path) -> ExperimentOutcome:
    assert row.result_json is not None and row.homerun_run_id is not None
    return ExperimentOutcome(
        run_id=row.id, fingerprint=row.fingerprint, status=row.status,
        decision="NOT_EVALUATED", dataset_manifest_id=row.dataset_manifest_id,
        homerun_run_id=row.homerun_run_id, artifact_dir=artifact_dir,
        result=row.result_json,
    )
```

A completed `runs/<run-id>` is never overwritten.

- [ ] **Step 4: Implement the HTML renderer**

Use this renderer contract:

```python
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from evosport.data.manifest import DatasetManifest


def render_report(*, run_id: str, fingerprint: str, manifest: DatasetManifest,
                  homerun_run_id: str, result: dict[str, Any], decision: dict[str, str],
                  artifact_dir: Path) -> Path:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html"]),
    )
    execution = dict(result.get("execution") or {})
    html = env.get_template("experiment.html.j2").render(
        run_id=run_id, fingerprint=fingerprint, manifest=manifest,
        homerun_run_id=homerun_run_id, result=result, execution=execution,
        decision=decision,
    )
    output = artifact_dir / "report.html"
    output.write_text(html, encoding="utf-8")
    return output
```

Create the template with this minimum body; styling may be limited to readable system fonts and a warning color:

```html
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>EvoSport {{ run_id }}</title></head>
<body>
  <h1>{{ decision.decision }}</h1>
  <p>{{ decision.reason }}</p>
  <dl>
    <dt>Run</dt><dd>{{ run_id }}</dd>
    <dt>Fingerprint</dt><dd>{{ fingerprint }}</dd>
    <dt>Dataset</dt><dd>{{ manifest.manifest_id }} · {{ manifest.start }} – {{ manifest.end }}</dd>
    <dt>Homerun run</dt><dd>{{ homerun_run_id }}</dd>
    <dt>Trades</dt><dd>{{ execution.get('trade_count', 0) }}</dd>
    <dt>Total return</dt><dd>{{ execution.get('total_return_pct', 0) }}</dd>
  </dl>
  <h2>Data coverage</h2><pre>{{ result.get('data_coverage', {}) }}</pre>
  <h2>Validation warnings</h2><pre>{{ execution.get('validation_warnings', []) }}</pre>
  <strong>Statistical promotion is unavailable in P0-P2.</strong>
</body>
</html>
```

- [ ] **Step 5: Run tests and commit**

```bash
cd backend
venv/bin/python -m pytest tests/test_evosport_runner.py -v
venv/bin/python -m ruff check evosport/experiments/runner.py evosport/reports tests/test_evosport_runner.py
cd ..
git add backend/evosport/experiments/runner.py backend/evosport/reports backend/tests/test_evosport_runner.py
git commit -m "feat: orchestrate reproducible evosport runs"
```

Expected: PASS and the fake gateway is called once across duplicate runs.

---

### Task 9: Wire CLI Commands and Prove the Vertical Slice

**Files:**
- Modify: `backend/evosport/cli/__main__.py`
- Create: `backend/tests/fixtures/evosport/strategy.py`
- Create: `backend/tests/test_evosport_vertical_slice.py`
- Create: `docs/evosport/quickstart.md`

**Interfaces:**
- Produces CLI: `evosport dataset freeze`
- Produces CLI: `evosport experiment validate`
- Produces CLI: `evosport experiment run`
- Does not produce `pipeline --through walk-forward`, OOS, shadow, or live commands in this phase.

- [ ] **Step 1: Write failing CLI command tests**

```python
import pytest
import yaml


def _write_valid_cli_spec(tmp_path):
    strategy = tmp_path / "strategy.py"
    lock = tmp_path / "requirements.lock"
    manifest = tmp_path / "manifest.json"
    strategy.write_text("class Strategy: pass\n", encoding="utf-8")
    lock.write_text("pydantic==2.7.0\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump({
        "name": "cli-fixture", "family_id": "football-over25",
        "dataset_manifest_path": str(manifest),
        "strategy": {"slug": "over25", "source_path": str(strategy),
                     "dependency_lock_path": str(lock), "config": {}},
        "window": {"train_start": "2026-01-01T00:00:00Z", "train_end": "2026-05-01T00:00:00Z",
                   "validation_start": "2026-05-01T00:00:00Z", "validation_end": "2026-08-01T00:00:00Z"},
        "execution": {"initial_capital_usd": 1000.0, "submit_p50_ms": 50.0,
                      "submit_p95_ms": 100.0, "cancel_p50_ms": 50.0, "cancel_p95_ms": 100.0},
        "split_method": "time", "max_trials": 1, "seed": 7,
    }), encoding="utf-8")
    return path


def test_validate_command_returns_zero(tmp_path, capsys):
    spec_path = _write_valid_cli_spec(tmp_path)
    assert main(["experiment", "validate", str(spec_path)]) == 0
    assert "valid" in capsys.readouterr().out.lower()


def test_oos_command_is_not_exposed():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["oos", "evaluate", "candidate"])
```

- [ ] **Step 2: Run the CLI tests and verify red**

Run: `cd backend && venv/bin/python -m pytest tests/test_evosport_cli.py -v`

Expected: FAIL because subcommands are not registered.

- [ ] **Step 3: Add argparse subcommands**

Extend `build_parser` with this parser shape and set each leaf parser's `handler`:

```python
import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path

from evosport.data.freeze import freeze_dataset
from evosport.experiments.gateway import HomerunBacktestGateway
from evosport.experiments.registry import SqlRunRegistry
from evosport.experiments.runner import ExperimentRunner
from evosport.experiments.spec import load_experiment_spec


subcommands = parser.add_subparsers(dest="command")
dataset = subcommands.add_parser("dataset")
dataset_commands = dataset.add_subparsers(dest="dataset_command")
freeze = dataset_commands.add_parser("freeze")
freeze.add_argument("--source-file", action="append", required=True, type=Path)
freeze.add_argument("--output-root", required=True, type=Path)
freeze.add_argument("--source", required=True)
freeze.add_argument("--token-id", action="append", required=True)
freeze.add_argument("--start", required=True, type=_parse_datetime)
freeze.add_argument("--end", required=True, type=_parse_datetime)
freeze.set_defaults(handler=_handle_dataset_freeze)

experiment = subcommands.add_parser("experiment")
experiment_commands = experiment.add_subparsers(dest="experiment_command")
validate = experiment_commands.add_parser("validate")
validate.add_argument("spec_path", type=Path)
validate.set_defaults(handler=_handle_experiment_validate)
run = experiment_commands.add_parser("run")
run.add_argument("spec_path", type=Path)
run.add_argument("--artifact-root", required=True, type=Path)
run.set_defaults(handler=_handle_experiment_run)
```

Implement the leaf handlers exactly around public APIs:

```python
def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a UTC offset")
    return parsed


def _handle_dataset_freeze(args: argparse.Namespace) -> int:
    manifest = freeze_dataset(
        source_files=args.source_file, output_root=args.output_root,
        source=args.source, token_ids=args.token_id, start=args.start, end=args.end,
    )
    print(manifest.model_dump_json())
    return 0


def _handle_experiment_validate(args: argparse.Namespace) -> int:
    spec = load_experiment_spec(args.spec_path)
    print(json.dumps({"valid": True, "name": spec.name}, sort_keys=True))
    return 0


def _build_runner(artifact_root: Path) -> ExperimentRunner:
    from models.database import BacktestAsyncSessionLocal
    return ExperimentRunner(
        registry=SqlRunRegistry(BacktestAsyncSessionLocal),
        gateway=HomerunBacktestGateway(), artifact_root=artifact_root,
        homerun_commit=_git_commit(), evaluator_version="not-evaluated-v1",
    )


def _handle_experiment_run(args: argparse.Namespace) -> int:
    outcome = asyncio.run(_build_runner(args.artifact_root).run(args.spec_path))
    print(json.dumps({
        "run_id": outcome.run_id, "fingerprint": outcome.fingerprint,
        "decision": outcome.decision,
        "report_path": str(outcome.artifact_dir / "report.html"),
    }, sort_keys=True))
    return 0
```

Use this implementation for the commit pin and dispatch:

```python
def _git_commit() -> str:
    repository_root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository_root,
        check=True, capture_output=True, text=True,
    )
    return completed.stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return int(handler(args))
```

Tests monkeypatch `_build_runner` rather than connecting to Postgres.

- [ ] **Step 4: Add a complete fake-gateway vertical-slice test**

Reuse the concrete fixture construction from Task 8 in this separate end-to-end test, run `ExperimentRunner` with `AsyncMock`, and assert:

```python
assert outcome.decision == "NOT_EVALUATED"
assert outcome.dataset_manifest_id == frozen.manifest_id
assert json.loads((outcome.artifact_dir / "decision.json").read_text())["decision"] == "NOT_EVALUATED"
assert "hr-fixture" in (outcome.artifact_dir / "report.html").read_text()
```

It then runs the same spec again and asserts the gateway call count remains one.

- [ ] **Step 5: Document the exact P0–P2 workflow**

Write `docs/evosport/quickstart.md` with these exact sections and commands:

```markdown
# EvoSport P0-P2 Quick Start

This slice freezes input data, runs one Homerun backtest, and records immutable artifacts. `NOT_EVALUATED` means no statistical promotion decision has been made.

## Environment

Run `make install-backend`, configure Homerun PostgreSQL, then run `cd backend && venv/bin/alembic upgrade head`.

## Freeze data

Run `venv/bin/python -m evosport.cli dataset freeze --source-file RAW.jsonl --output-root DATASETS --source fixture --token-id YES --token-id NO --start 2026-05-01T00:00:00Z --end 2026-08-02T00:00:00Z`.

## Validate and run

Run `venv/bin/python -m evosport.cli experiment validate EXPERIMENT.yaml`, then `venv/bin/python -m evosport.cli experiment run EXPERIMENT.yaml --artifact-root RUNS`.

## Artifacts

Each `RUNS/<run-id>/` contains the experiment, dataset manifest, strategy package, dependency lock, Homerun result, `NOT_EVALUATED` decision, and HTML report.

## Safety boundary

P0-P2 has no OOS consumption, shadow promotion, live-order, or statistical PASS path.

## Tests

Run the focused pytest and Ruff commands from Task 9 of the implementation plan.
```

- [ ] **Step 6: Run focused and compatibility verification**

```bash
cd backend
venv/bin/python -m pytest \
  tests/test_evosport_cli.py \
  tests/test_evosport_domain.py \
  tests/test_evosport_football_totals.py \
  tests/test_evosport_dataset_freeze.py \
  tests/test_evosport_experiment_spec.py \
  tests/test_evosport_gateway.py \
  tests/test_evosport_registry.py \
  tests/test_evosport_runner.py \
  tests/test_evosport_vertical_slice.py -v
venv/bin/python -m pytest tests/test_backtest_engine.py tests/test_backtest_settlement.py -q
venv/bin/python -m ruff check evosport tests/test_evosport_*.py
venv/bin/alembic heads
```

Expected: all tests pass, Ruff is clean, and `202608190001` is the only Alembic head.

- [ ] **Step 7: Inspect the thin-fork boundary**

Run:

```bash
git diff --stat c8e647f...HEAD
git diff --name-only c8e647f...HEAD | sort
```

Expected: application changes are confined to `backend/evosport/`, one Alembic migration, EvoSport tests, and EvoSport docs. No frontend, live execution, wallet, shadow, autoresearch, or Homerun backtest engine file is modified.

- [ ] **Step 8: Commit the P0–P2 vertical slice**

```bash
git add backend/evosport/cli/__main__.py backend/tests/fixtures/evosport/strategy.py backend/tests/test_evosport_cli.py backend/tests/test_evosport_vertical_slice.py docs/evosport/quickstart.md
git commit -m "feat: complete evosport reproducible experiment slice"
```

---

## P0–P2 Completion Gate

The slice is complete only when all of the following are evidenced by command output:

- The repository descends from pinned Homerun commit `c8e647f`.
- The two Homerun compatibility test files still pass.
- All EvoSport unit and vertical-slice tests pass.
- Identical inputs reuse one fingerprint and one gateway invocation.
- Changed source bytes produce a different dataset ID or run fingerprint.
- The report always says `NOT_EVALUATED`; no G1–G4 decision exists.
- The Alembic chain has exactly one head.
- The diff boundary contains no Homerun engine, frontend, shadow, live execution, wallet, or autoresearch modification.
- No command can consume OOS or place an order.

After this gate, create the next independent plan for P3–P4 Evaluation Gate and simple baselines. Do not add those features opportunistically to this slice.
