from __future__ import annotations

import hashlib
import json
import os
import ctypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from stat import S_IMODE, S_ISDIR
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from evosport.data.manifest import (
    CATALOG_SCHEMA_VERSION,
    DatasetFile,
    DatasetManifest,
    manifest_id_for,
    sha256_file,
)
from evosport.domain.sports import (
    CanonicalSportsContract,
    CanonicalSportsEvent,
    EventStatus,
    MarketType,
    SettlementPolicy,
)
from evosport.domain.time import TemporalEnvelope
from evosport.experiments.environment import active_distribution_pins
from evosport.experiments.registry import InMemoryRunRegistry
from evosport.experiments.runner import ExperimentRunner, _manifest_effective_content_hash
from evosport.semantics.football_binding import FootballDatasetBinding
from evosport.semantics.football_binding import expected_football_settlement
from evosport.semantics.football_totals import FootballMatchResult
from services.marketdata.manifest import SnapshotEntry, compute_content_hash


@dataclass(frozen=True)
class RunnerFixture:
    spec_path: Path
    manifest_path: Path
    source_path: Path
    lock_path: Path
    artifact_root: Path
    manifest: DatasetManifest


def _track_open_directory_descriptors(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    opened: list[int] = []
    original_open = os.open

    def track_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if S_ISDIR(os.fstat(descriptor).st_mode):
            opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", track_open)
    return opened


def _assert_descriptors_closed(descriptors: list[int]) -> None:
    if not descriptors:
        return
    assert len(descriptors) >= 2
    for descriptor in set(descriptors):
        with pytest.raises(OSError):
            os.fstat(descriptor)


def _require_darwin_materialization() -> None:
    import evosport.data.freeze as freeze

    if freeze._resolve_cleanup_backend() != "darwin":
        pytest.skip("named-object race contract is specific to the Darwin backend")


def _current_football_identity(binding: FootballDatasetBinding, dataset_ids: tuple[str, ...]) -> dict[str, object]:
    record = expected_football_settlement(binding)
    return {
        "provider_dataset_ids": list(dataset_ids),
        "provider_datasets": [],
        "settlement": {
            "condition_id": record.condition_id,
            "slug": None,
            "winning_token_id": record.winning_token_id,
            "winning_outcome": record.winning_outcome,
            "token_ids": sorted(record.token_ids),
            "resolution_time": record.resolution_time.isoformat(),
            "coin_price_start": None,
            "coin_price_end": None,
            "resolved": record.resolved,
            "source": record.source,
        },
    }


@pytest.fixture(autouse=True)
def _stub_current_football_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    import evosport.experiments.runner as runner_module

    async def load_identity(
        binding: FootballDatasetBinding,
        dataset_ids: tuple[str, ...],
    ) -> dict[str, object]:
        return _current_football_identity(binding, dataset_ids)

    async def resolve_coverage_identity(manifest: DatasetManifest) -> dict[str, object]:
        return {
            "paths": [item.path for item in manifest.files],
            "dataset_ids_by_path": {
                item.path: list(item.provider_dataset_ids) for item in manifest.files
            },
            "paths_by_token": {
                token_id: [item.path for item in manifest.files if token_id in item.token_ids]
                for token_id in manifest.token_ids
            },
        }

    monkeypatch.setattr(runner_module, "load_current_football_identity", load_identity)
    monkeypatch.setattr(
        runner_module,
        "_resolve_manifest_coverage_identity",
        resolve_coverage_identity,
    )


def _football_binding(start: datetime, kickoff: datetime) -> FootballDatasetBinding:
    return FootballDatasetBinding(
        event=CanonicalSportsEvent(
            event_id="event-1",
            competition="fixture-league",
            season="2026",
            home_team="Home",
            away_team="Away",
            scheduled_start=kickoff,
            actual_start=kickoff,
            status=EventStatus.FINISHED,
        ),
        contract=CanonicalSportsContract(
            contract_id="contract-1",
            event_id="event-1",
            venue="fixture-provider",
            venue_market_id="market-1",
            yes_token_id="YES",
            no_token_id="NO",
            market_type=MarketType.TOTAL_GOALS_OVER_UNDER,
            threshold=Decimal("2.5"),
            side="over",
            opens_at=start,
            closes_at=kickoff,
            rule_version="fixture-v1",
            settlement=SettlementPolicy(),
        ),
        result=FootballMatchResult(
            regulation_home=2,
            regulation_away=1,
            status=EventStatus.FINISHED,
        ),
        result_time=TemporalEnvelope(
            event_time=kickoff + timedelta(hours=2),
            observed_at=kickoff + timedelta(hours=2, minutes=1),
            ingested_at=kickoff + timedelta(hours=2, minutes=2),
        ),
    )


@pytest.fixture
def runner_fixture(tmp_path: Path) -> RunnerFixture:
    start = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    kickoff = start + timedelta(hours=1)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    yes_path = canonical / "snapshots__YES.parquet"
    no_path = canonical / "snapshots__NO.parquet"
    yes_path.write_bytes(b"canonical-selected-yes")
    no_path.write_bytes(b"canonical-selected-no")
    files = tuple(
        DatasetFile(
            path=str(path.resolve()),
            sha256=sha256_file(path),
            size_bytes=path.stat().st_size,
            provider_dataset_ids=("dataset-selected",),
            token_ids=(token_id,),
        )
        for path, token_id in sorted(((yes_path, "YES"), (no_path, "NO")))
    )
    binding = _football_binding(start, kickoff)
    manifest = DatasetManifest(
        schema_version=CATALOG_SCHEMA_VERSION,
        manifest_id=manifest_id_for(
            schema_version=CATALOG_SCHEMA_VERSION,
            source="homerun_catalog",
            token_ids=("NO", "YES"),
            start=start,
            end=kickoff,
            files=files,
            provider_dataset_ids=("dataset-selected",),
            football=binding,
        ),
        source="homerun_catalog",
        token_ids=("NO", "YES"),
        start=start,
        end=kickoff,
        files=files,
        provider_dataset_ids=("dataset-selected",),
        football=binding,
    )
    manifest_dir = tmp_path / "manifests" / manifest.manifest_id
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    source_path = tmp_path / "strategy.py"
    source_path.write_text("class Strategy: pass\n", encoding="utf-8")
    lock_path = tmp_path / "environment.lock"
    lock_path.write_text("\n".join(active_distribution_pins()) + "\n", encoding="utf-8")
    spec_path = tmp_path / "experiment.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "name": "fixture",
                "family_id": "football-over25",
                "dataset_manifest_path": str(manifest_path),
                "strategy": {
                    "slug": "over25",
                    "source_path": str(source_path),
                    "dependency_lock_path": str(lock_path),
                    "config": {},
                },
                "window": {
                    "train_start": "2026-01-01T00:00:00Z",
                    "train_end": start.isoformat(),
                    "validation_start": start.isoformat(),
                    "validation_end": kickoff.isoformat(),
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
    return RunnerFixture(
        spec_path=spec_path,
        manifest_path=manifest_path,
        source_path=source_path,
        lock_path=lock_path,
        artifact_root=tmp_path / "runs",
        manifest=manifest,
    )


def _runner(registry: InMemoryRunRegistry, gateway: AsyncMock, artifact_root: Path) -> ExperimentRunner:
    return ExperimentRunner(
        registry=registry,
        gateway=gateway,
        artifact_root=artifact_root,
        homerun_commit="c8e647f",
        evaluator_version="not-evaluated-v1",
    )


def _successful_result(manifest: DatasetManifest) -> dict[str, object]:
    entries = [
        SnapshotEntry(
            path=item.path or "",
            size_bytes=item.size_bytes,
            mtime_us=1,
            sha256=item.sha256,
            token_ids=item.token_ids,
            provider_dataset_ids=item.provider_dataset_ids,
        )
        for item in manifest.files
    ]
    return {
        "run_id": "hr-1",
        "effective_dataset": {
            "schema_version": "snapshots_v2",
            "created_at_us": 1,
            "content_hash": compute_content_hash(entries),
            "entries": [
                {
                    "path": entry.path,
                    "size_bytes": entry.size_bytes,
                    "mtime_us": entry.mtime_us,
                    "sha256": entry.sha256,
                    "rows": 1,
                    "token_ids": list(entry.token_ids),
                    "provider_dataset_ids": list(entry.provider_dataset_ids),
                    "start_us": None,
                    "end_us": None,
                }
                for entry in entries
            ],
            "extra": {},
            "provider_dataset_ids": list(manifest.provider_dataset_ids),
        },
        "execution": {
            "success": True,
            "validation_errors": [],
            "runtime_error": None,
            "settlement_summary": {"settled_positions": 1},
            "trade_count": 1,
            "total_return_pct": 1.5,
        },
        "effective_football": {
            "provider_dataset_ids": ["dataset-selected"],
            "markets": [
                {
                    "market_id": "market-1",
                    "condition_id": "market-1",
                    "slug": "contract-1",
                    "title": "Home v Away: Over 2.5 total goals",
                    "coin": None,
                    "timeframe": "total_goals_over_under",
                    "yes_token_id": "YES",
                    "no_token_id": "NO",
                    "market_start": "2026-08-01T10:00:00+00:00",
                    "market_close": "2026-08-01T11:00:00+00:00",
                    "price_to_beat": None,
                }
            ],
            "token_settlements": [
                {
                    "token_id": "NO",
                    "condition_id": "market-1",
                    "settlement_price": 0.0,
                    "winning_outcome": "YES",
                    "resolution_time": "2026-08-01T13:01:00+00:00",
                    "source": "evosport:football:fixture-v1",
                },
                {
                    "token_id": "YES",
                    "condition_id": "market-1",
                    "settlement_price": 1.0,
                    "winning_outcome": "YES",
                    "resolution_time": "2026-08-01T13:01:00+00:00",
                    "source": "evosport:football:fixture-v1",
                },
            ],
        },
    }


def test_manifest_effective_hash_normalizes_windows_path_separators() -> None:
    manifest = SimpleNamespace(
        files=(
            SimpleNamespace(
                path=r"C:\canonical\snapshots__YES.parquet",
                sha256="a" * 64,
                size_bytes=17,
            ),
        )
    )
    expected = hashlib.sha256(
        ("C:/canonical/snapshots__YES.parquet|" + "a" * 64 + "|17\n").encode()
    ).hexdigest()

    assert _manifest_effective_content_hash(manifest) == f"sha256:{expected}"


@pytest.mark.asyncio
async def test_runner_publishes_minimal_verified_cache_and_environment_identity(
    runner_fixture: RunnerFixture,
) -> None:
    gateway = AsyncMock()
    gateway.run.return_value = _successful_result(runner_fixture.manifest)
    registry = InMemoryRunRegistry()
    runner = _runner(registry, gateway, runner_fixture.artifact_root)

    first = await runner.run(runner_fixture.spec_path)
    second = await runner.run(runner_fixture.spec_path)

    assert first == second
    assert first.run_id == first.homerun_run_id == "hr-1"
    assert first.status == "SUCCEEDED"
    assert first.decision == "NOT_EVALUATED"
    assert first.artifact_dir.name == first.fingerprint
    assert {item.relative_to(first.artifact_dir).as_posix() for item in first.artifact_dir.rglob("*")} == {
        "experiment.yaml",
        "dataset-manifest.json",
        "strategy-package",
        "strategy-package/strategy.py",
        "environment.lock",
        "environment.json",
        "result.json",
        "decision.json",
        "report.html",
        "artifact-manifest.json",
    }
    environment = json.loads((first.artifact_dir / "environment.json").read_text(encoding="utf-8"))
    assert environment["python_implementation"]
    assert environment["python_version"]
    assert environment["identity_sha256"]
    for artifact in first.artifact_dir.rglob("*"):
        assert S_IMODE(artifact.stat().st_mode) & 0o222 == 0
    publication = await registry.get_by_fingerprint(first.fingerprint)
    assert publication is not None
    assert publication.homerun_run_id == "hr-1"
    assert not hasattr(publication, "result_json")
    gateway.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_environment_lock_fails_before_gateway(runner_fixture: RunnerFixture) -> None:
    runner_fixture.lock_path.write_text("fabricated==9.9.9\n", encoding="utf-8")
    gateway = AsyncMock()

    with pytest.raises(ValueError, match="does not exactly match active environment"):
        await _runner(InMemoryRunRegistry(), gateway, runner_fixture.artifact_root).run(
            runner_fixture.spec_path
        )

    gateway.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_offline_opaque_manifest_is_not_run_eligible(
    runner_fixture: RunnerFixture,
) -> None:
    legacy_dir = runner_fixture.manifest_path.parent / "opaque"
    legacy_dir.mkdir()
    legacy_path = legacy_dir / "manifest.json"
    legacy_path.write_text('{"source_file":"RAW.jsonl"}', encoding="utf-8")
    spec = yaml.safe_load(runner_fixture.spec_path.read_text(encoding="utf-8"))
    spec["dataset_manifest_path"] = str(legacy_path)
    runner_fixture.spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    gateway = AsyncMock()

    with pytest.raises(RuntimeError, match="snapshot integrity"):
        await _runner(InMemoryRunRegistry(), gateway, runner_fixture.artifact_root).run(
            runner_fixture.spec_path
        )

    gateway.run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("dataset_ids", "dataset IDs"),
        ("path", "files"),
        ("sha256", "files"),
        ("runtime_error", "runtime error"),
        ("validation_errors", "validation errors"),
        ("settlement", "settlement"),
        ("football", "effective football"),
    ],
)
async def test_runner_rejects_untruthful_gateway_evidence_before_publication(
    runner_fixture: RunnerFixture,
    mutation: str,
    message: str,
) -> None:
    result = _successful_result(runner_fixture.manifest)
    effective = result["effective_dataset"]
    execution = result["execution"]
    assert isinstance(effective, dict) and isinstance(execution, dict)
    if mutation == "dataset_ids":
        effective["provider_dataset_ids"] = ["contaminating"]
    elif mutation == "path":
        effective["entries"][0]["path"] = "/tmp/contaminating.parquet"
    elif mutation == "sha256":
        effective["entries"][0]["sha256"] = "f" * 64
    elif mutation == "runtime_error":
        execution["runtime_error"] = "engine failed"
    elif mutation == "validation_errors":
        execution["validation_errors"] = ["invalid strategy"]
    elif mutation == "settlement":
        execution.pop("settlement_summary")
    else:
        result["effective_football"]["token_settlements"][1]["settlement_price"] = 0.0
    gateway = AsyncMock()
    gateway.run.return_value = result
    registry = InMemoryRunRegistry()

    with pytest.raises(ValueError, match=message):
        await _runner(registry, gateway, runner_fixture.artifact_root).run(runner_fixture.spec_path)

    assert registry._by_fingerprint == {}
    assert not any(runner_fixture.artifact_root.iterdir())


@pytest.mark.asyncio
async def test_cached_result_tampering_is_rejected_without_gateway_reexecution(
    runner_fixture: RunnerFixture,
) -> None:
    gateway = AsyncMock()
    gateway.run.return_value = _successful_result(runner_fixture.manifest)
    runner = _runner(InMemoryRunRegistry(), gateway, runner_fixture.artifact_root)
    outcome = await runner.run(runner_fixture.spec_path)
    outcome.artifact_dir.chmod(0o755)
    result_path = outcome.artifact_dir / "result.json"
    result_path.chmod(0o644)
    result_path.write_text('{"run_id":"forged"}\n', encoding="utf-8")

    with pytest.raises((RuntimeError, ValueError), match="result|gateway|artifact"):
        await runner.run(runner_fixture.spec_path)

    gateway.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_changed_selected_bytes_fail_before_cache_or_gateway(runner_fixture: RunnerFixture) -> None:
    gateway = AsyncMock()
    gateway.run.return_value = _successful_result(runner_fixture.manifest)
    runner = _runner(InMemoryRunRegistry(), gateway, runner_fixture.artifact_root)
    await runner.run(runner_fixture.spec_path)
    selected = Path(runner_fixture.manifest.files[0].path or "")
    selected.write_bytes(b"changed-selected-bytes")

    with pytest.raises(RuntimeError, match="snapshot integrity"):
        await runner.run(runner_fixture.spec_path)

    gateway.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_runner_uses_one_logical_verified_materialization_and_cleans_it(
    runner_fixture: RunnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_descriptors = _track_open_directory_descriptors(monkeypatch)
    logical_paths = {item.path for item in runner_fixture.manifest.files}
    captured_physical_paths: tuple[str, ...] = ()

    class InspectingGateway:
        async def run(self, request: object) -> dict[str, object]:
            nonlocal captured_physical_paths
            view = request.market_data_view
            captured_physical_paths = view.coverage().all_files()
            assert set(captured_physical_paths).isdisjoint(logical_paths)
            assert all(Path(path).is_file() for path in captured_physical_paths)
            snapshot = view.dataset_snapshot()
            assert {entry.path for entry in snapshot.entries} == logical_paths
            assert {entry.sha256 for entry in snapshot.entries} == {
                item.sha256 for item in runner_fixture.manifest.files
            }
            return _successful_result(runner_fixture.manifest)

    outcome = await _runner(
        InMemoryRunRegistry(),
        InspectingGateway(),
        runner_fixture.artifact_root,
    ).run(runner_fixture.spec_path)

    assert outcome.status == "SUCCEEDED"
    assert captured_physical_paths
    assert all(not Path(path).exists() for path in captured_physical_paths)
    _assert_descriptors_closed(directory_descriptors)


@pytest.mark.asyncio
async def test_runner_cleans_selected_materialization_when_gateway_fails(
    runner_fixture: RunnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_descriptors = _track_open_directory_descriptors(monkeypatch)
    captured_physical_paths: tuple[str, ...] = ()

    class FailingGateway:
        async def run(self, request: object) -> dict[str, object]:
            nonlocal captured_physical_paths
            captured_physical_paths = request.market_data_view.coverage().all_files()
            raise RuntimeError("injected gateway failure")

    with pytest.raises(RuntimeError, match="injected gateway failure"):
        await _runner(
            InMemoryRunRegistry(),
            FailingGateway(),
            runner_fixture.artifact_root,
        ).run(runner_fixture.spec_path)

    assert captured_physical_paths
    assert all(not Path(path).exists() for path in captured_physical_paths)
    assert not any(runner_fixture.artifact_root.iterdir())
    _assert_descriptors_closed(directory_descriptors)


@pytest.mark.asyncio
async def test_selected_cleanup_refusal_note_cannot_mask_primary_gateway_error(
    runner_fixture: RunnerFixture,
) -> None:
    _require_darwin_materialization()
    registry = InMemoryRunRegistry()
    primary_error = _NoteRejectingPrimaryError("primary gateway failure")
    root: Path | None = None
    moved_owned_root: Path | None = None
    replacement_sentinel: Path | None = None

    class RootReplacingGateway:
        async def run(self, request: object) -> dict[str, object]:
            nonlocal root, moved_owned_root, replacement_sentinel
            root = Path(request.market_data_view.coverage().all_files()[0]).parent
            moved_owned_root = root.with_name(f"{root.name}-moved-owned")
            os.rename(root, moved_owned_root)
            root.mkdir(mode=0o755)
            replacement_sentinel = root / "sentinel.txt"
            replacement_sentinel.write_text("external-gateway-replacement", encoding="utf-8")
            raise primary_error

    try:
        with pytest.raises(
            _NoteRejectingPrimaryError,
            match="primary gateway failure",
        ) as exc_info:
            await _runner(
                registry,
                RootReplacingGateway(),
                runner_fixture.artifact_root,
            ).run(runner_fixture.spec_path)

        assert exc_info.value is primary_error
        assert registry._by_fingerprint == {}
        assert any("selected data cleanup also failed" in note for note in exc_info.value.__notes__)
        assert root is not None and root.is_dir()
        assert replacement_sentinel is not None
        assert replacement_sentinel.read_text(encoding="utf-8") == "external-gateway-replacement"
        assert moved_owned_root is not None and moved_owned_root.is_dir()
        assert not any(runner_fixture.artifact_root.iterdir())
    finally:
        if replacement_sentinel is not None and replacement_sentinel.exists():
            replacement_sentinel.unlink()
        if root is not None and root.is_dir():
            root.rmdir()
        if moved_owned_root is not None and moved_owned_root.is_dir():
            moved_owned_root.chmod(0o700)
            for child in moved_owned_root.iterdir():
                child.chmod(0o600)
                child.unlink()
            moved_owned_root.rmdir()


def test_materialized_selected_byte_mutation_fails_closed_and_cleans_owned_bytes(
    runner_fixture: RunnerFixture,
) -> None:
    from evosport.data.freeze import materialize_verified_market_data

    captured_path: Path | None = None
    with pytest.raises(RuntimeError, match="materialized selected data changed"):
        with materialize_verified_market_data(runner_fixture.manifest) as selected:
            physical = Path(selected.view.coverage().all_files()[0])
            captured_path = physical
            physical.chmod(0o644)
            physical.write_bytes(b"mutated-run-owned-bytes")
            selected.verify()

    assert captured_path is not None and not captured_path.exists()


def test_file_reference_capture_aba_never_binds_or_deletes_external_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.data.freeze as freeze

    original_capture = freeze._FS_PATH_MAKE_REF_WITH_OPTIONS
    if original_capture is None or freeze._FS_DELETE_OBJECT is None:
        pytest.skip("Darwin identity-bound deletion primitives are unavailable")

    owned_path = tmp_path / "owned.parquet"
    moved_owned_path = tmp_path / "moved-owned.parquet"
    external_path = tmp_path / "external.parquet"
    owned_path.write_bytes(b"owned-selected-data")
    external_path.write_bytes(b"external-selected-data")
    descriptor: int | None = os.open(
        owned_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    owned_state = os.fstat(descriptor)
    swapped = False

    def capture_after_aba(
        path: bytes,
        options: int,
        reference: object,
        is_directory: object,
    ) -> int:
        nonlocal swapped
        assert Path(os.fsdecode(path)) == owned_path
        os.rename(owned_path, moved_owned_path)
        os.rename(external_path, owned_path)
        try:
            status = original_capture(path, options, reference, is_directory)
        finally:
            os.rename(owned_path, external_path)
            os.rename(moved_owned_path, owned_path)
        swapped = True
        return status

    reference: object | None = None
    capture_error: RuntimeError | None = None
    try:
        monkeypatch.setattr(
            freeze,
            "_FS_PATH_MAKE_REF_WITH_OPTIONS",
            capture_after_aba,
        )
        try:
            reference = freeze._capture_owned_object_reference(
                owned_path,
                owned_state,
                descriptor=descriptor,
                is_directory=False,
            )
        except RuntimeError as exc:
            capture_error = exc
        if reference is not None:
            os.close(descriptor)
            descriptor = None
            freeze._delete_owned_object(reference)

        assert swapped is True
        assert owned_path.read_bytes() == b"owned-selected-data"
        assert external_path.read_bytes() == b"external-selected-data"
        assert capture_error is not None
        assert "ownership" in str(capture_error) or "identity" in str(capture_error)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for path in (owned_path, moved_owned_path, external_path):
            if path.exists():
                path.unlink()


def test_root_reference_capture_aba_never_binds_or_deletes_external_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.data.freeze as freeze

    original_capture = freeze._FS_PATH_MAKE_REF_WITH_OPTIONS
    if original_capture is None or freeze._FS_DELETE_OBJECT is None:
        pytest.skip("Darwin identity-bound deletion primitives are unavailable")

    owned_root = tmp_path / "owned-root"
    moved_owned_root = tmp_path / "moved-owned-root"
    external_root = tmp_path / "external-root"
    owned_root.mkdir()
    external_root.mkdir()
    descriptor: int | None = os.open(
        owned_root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    owned_state = os.fstat(descriptor)
    swapped = False

    def capture_after_aba(
        path: bytes,
        options: int,
        reference: object,
        is_directory: object,
    ) -> int:
        nonlocal swapped
        assert Path(os.fsdecode(path)) == owned_root
        os.rename(owned_root, moved_owned_root)
        os.rename(external_root, owned_root)
        try:
            status = original_capture(path, options, reference, is_directory)
        finally:
            os.rename(owned_root, external_root)
            os.rename(moved_owned_root, owned_root)
        swapped = True
        return status

    reference: object | None = None
    capture_error: RuntimeError | None = None
    try:
        monkeypatch.setattr(
            freeze,
            "_FS_PATH_MAKE_REF_WITH_OPTIONS",
            capture_after_aba,
        )
        try:
            reference = freeze._capture_owned_object_reference(
                owned_root,
                owned_state,
                descriptor=descriptor,
                is_directory=True,
            )
        except RuntimeError as exc:
            capture_error = exc
        if reference is not None:
            os.close(descriptor)
            descriptor = None
            freeze._delete_owned_object(reference)

        assert swapped is True
        assert owned_root.is_dir()
        assert external_root.is_dir()
        assert capture_error is not None
        assert "ownership" in str(capture_error) or "identity" in str(capture_error)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for path in (owned_root, moved_owned_root, external_root):
            if path.is_dir():
                path.rmdir()


@pytest.mark.parametrize(
    "missing_capability",
    [
        "_FS_PATH_MAKE_REF_WITH_OPTIONS",
        "_FS_DELETE_OBJECT",
        "_CF_URL_CREATE_FROM_FS_REF",
        "_CF_URL_COPY_RESOURCE_PROPERTY_FOR_KEY",
    ],
)
def test_missing_identity_cleanup_capability_leaves_no_materialization_root(
    runner_fixture: RunnerFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_capability: str,
) -> None:
    import evosport.data.freeze as freeze

    monkeypatch.setattr(freeze.sys, "platform", "darwin")
    monkeypatch.setattr(freeze, missing_capability, None)
    monkeypatch.setattr(freeze.tempfile, "tempdir", str(tmp_path))

    with pytest.raises(RuntimeError, match="cleanup is unavailable"):
        with freeze.materialize_verified_market_data(runner_fixture.manifest):
            raise AssertionError("capability failure must happen before materialization")

    assert list(tmp_path.glob("evosport-selected-*")) == []


def test_linux_materialization_uses_disk_anonymous_read_only_descriptors(
    runner_fixture: RunnerFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.data.freeze as freeze

    calls: list[tuple[Path, int, int]] = []
    backing_files: list[Path] = []
    backing_by_writer: dict[int, Path] = {}

    def open_tmpfile(directory: Path, flags: int, mode: int) -> int:
        calls.append((directory, flags, mode))
        backing = tmp_path / f"anonymous-backing-{len(backing_files)}"
        backing_files.append(backing)
        descriptor = os.open(backing, os.O_RDWR | os.O_CREAT | os.O_EXCL, mode)
        backing_by_writer[descriptor] = backing
        return descriptor

    def open_readonly(path: Path, flags: int) -> int:
        assert flags & os.O_ACCMODE == os.O_RDONLY
        return os.open(backing_by_writer[int(path.name)], os.O_RDONLY)

    monkeypatch.setattr(freeze.sys, "platform", "linux")
    monkeypatch.setattr(freeze, "_LINUX_O_TMPFILE", 0x400000, raising=False)
    monkeypatch.setattr(freeze, "_LINUX_OPEN_TMPFILE", open_tmpfile, raising=False)
    monkeypatch.setattr(freeze, "_LINUX_OPEN_READONLY", open_readonly, raising=False)
    monkeypatch.setattr(freeze, "_LINUX_FD_ROOT", Path("/dev/fd"), raising=False)

    exposed_paths: tuple[str, ...] = ()
    retained_descriptors: tuple[int, ...] = ()
    with freeze.materialize_verified_market_data(runner_fixture.manifest) as selected:
        exposed_paths = selected.view.coverage().all_files()
        retained_descriptors = tuple(
            item.descriptor for item in selected._files if item.descriptor is not None
        )
        assert all(Path(path).is_file() for path in exposed_paths)
        assert len(retained_descriptors) == len(runner_fixture.manifest.files)
        for descriptor in retained_descriptors:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.write(descriptor, b"mutation")
        for path in exposed_paths:
            assert Path(path).read_bytes() == Path(path).read_bytes()
        selected.verify()

    assert len(calls) == len(runner_fixture.manifest.files)
    assert all(directory == Path(freeze.tempfile.gettempdir()) for directory, _, _ in calls)
    assert all(flags & os.O_RDWR for _, flags, _ in calls)
    assert all(flags & 0x400000 for _, flags, _ in calls)
    assert all(mode == 0o400 for _, _, mode in calls)
    for descriptor in retained_descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)
    assert all(not Path(path).exists() for path in exposed_paths)


def test_linux_read_only_transfer_failure_closes_writer_without_residue(
    runner_fixture: RunnerFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.data.freeze as freeze

    writer_descriptors: list[int] = []

    def open_tmpfile(directory: Path, flags: int, mode: int) -> int:
        descriptor = os.open(
            tmp_path / "anonymous-backing",
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            mode,
        )
        writer_descriptors.append(descriptor)
        return descriptor

    def fail_readonly(path: Path, flags: int) -> int:
        raise RuntimeError("injected read-only transfer failure")

    monkeypatch.setattr(freeze.sys, "platform", "linux")
    monkeypatch.setattr(freeze, "_LINUX_O_TMPFILE", 0x400000, raising=False)
    monkeypatch.setattr(freeze, "_LINUX_OPEN_TMPFILE", open_tmpfile, raising=False)
    monkeypatch.setattr(freeze, "_LINUX_OPEN_READONLY", fail_readonly, raising=False)
    monkeypatch.setattr(freeze, "_LINUX_FD_ROOT", Path("/dev/fd"), raising=False)

    with pytest.raises(RuntimeError, match="injected read-only transfer failure"):
        with freeze.materialize_verified_market_data(runner_fixture.manifest):
            raise AssertionError("materialization must fail before yielding")

    assert len(writer_descriptors) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(writer_descriptors[0])


def test_windows_cleanup_revalidates_handle_identity_before_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.data.freeze as freeze

    information_type = freeze._WindowsFileInformation
    identities = iter(((41, 0x123456789), (41, 0x123456789)))
    disposition_calls: list[tuple[int, int, int, bool]] = []

    def get_information(handle: int, pointer: object) -> int:
        volume, file_id = next(identities)
        information = ctypes.cast(
            pointer,
            ctypes.POINTER(information_type),
        ).contents
        information.dwFileAttributes = 0x80
        information.dwVolumeSerialNumber = volume
        information.nFileIndexHigh = file_id >> 32
        information.nFileIndexLow = file_id & 0xFFFFFFFF
        return 1

    def set_information(
        handle: int,
        information_class: int,
        pointer: object,
        size: int,
    ) -> int:
        disposition = ctypes.cast(
            pointer,
            ctypes.POINTER(freeze._WindowsFileDispositionInformation),
        ).contents
        disposition_calls.append(
            (handle, information_class, size, bool(disposition.DeleteFile))
        )
        return 1

    monkeypatch.setattr(freeze, "_WINDOWS_GET_FILE_INFORMATION_BY_HANDLE", get_information)
    monkeypatch.setattr(freeze, "_WINDOWS_SET_FILE_INFORMATION_BY_HANDLE", set_information)

    reference = freeze._capture_windows_owned_handle(73, is_directory=False)
    freeze._delete_owned_object(reference)

    assert reference.expected_identity == (41, 0x123456789)
    assert disposition_calls == [
        (
            73,
            freeze._WINDOWS_FILE_DISPOSITION_INFO,
            ctypes.sizeof(freeze._WindowsFileDispositionInformation),
            True,
        )
    ]


def test_windows_cleanup_refuses_rebound_handle_before_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.data.freeze as freeze

    information_type = freeze._WindowsFileInformation
    identities = iter(((41, 1001), (41, 2002)))
    disposition_calls: list[int] = []

    def get_information(handle: int, pointer: object) -> int:
        volume, file_id = next(identities)
        information = ctypes.cast(
            pointer,
            ctypes.POINTER(information_type),
        ).contents
        information.dwFileAttributes = 0x10
        information.dwVolumeSerialNumber = volume
        information.nFileIndexHigh = file_id >> 32
        information.nFileIndexLow = file_id & 0xFFFFFFFF
        return 1

    def set_information(*args: object) -> int:
        disposition_calls.append(1)
        return 1

    monkeypatch.setattr(freeze, "_WINDOWS_GET_FILE_INFORMATION_BY_HANDLE", get_information)
    monkeypatch.setattr(freeze, "_WINDOWS_SET_FILE_INFORMATION_BY_HANDLE", set_information)

    reference = freeze._capture_windows_owned_handle(79, is_directory=True)
    with pytest.raises(RuntimeError, match="stale ownership"):
        freeze._delete_owned_object(reference)

    assert disposition_calls == []


def test_windows_materialization_uses_native_read_delete_owner_contract_simulation(
    runner_fixture: RunnerFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.data.freeze as freeze

    create_calls: list[tuple[int, int, int, int]] = []
    duplicate_access: list[int] = []
    disposition_handles: list[int] = []
    close_handles: list[int] = []
    pending_delete: set[int] = set()
    paths_by_handle: dict[int, Path] = {}

    def create_file(
        path: str,
        access: int,
        sharing: int,
        security: object,
        creation: int,
        attributes: int,
        template: object,
    ) -> int:
        assert security is None
        assert template is None
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if creation == getattr(freeze, "_WINDOWS_OPEN_EXISTING", 3):
            flags = os.O_RDONLY
        descriptor = os.open(path, flags, 0o600)
        paths_by_handle[descriptor] = Path(path)
        create_calls.append((access, sharing, creation, attributes))
        return descriptor

    def duplicate_handle(
        source_process: int,
        source_handle: int,
        target_process: int,
        target_pointer: object,
        access: int,
        inherit: bool,
        options: int,
    ) -> int:
        assert source_process == target_process == -1
        assert inherit is False
        assert options == 0
        duplicate = os.dup(source_handle)
        paths_by_handle[duplicate] = paths_by_handle[source_handle]
        ctypes.cast(target_pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = duplicate
        duplicate_access.append(access)
        return 1

    def write_file(
        handle: int,
        buffer: object,
        length: int,
        written_pointer: object,
        overlapped: object,
    ) -> int:
        assert overlapped is None
        written = os.write(handle, ctypes.string_at(buffer, length))
        ctypes.cast(written_pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = written
        return 1

    def read_file(
        handle: int,
        buffer: object,
        length: int,
        read_pointer: object,
        overlapped: object,
    ) -> int:
        assert overlapped is None
        chunk = os.read(handle, length)
        ctypes.memmove(buffer, chunk, len(chunk))
        ctypes.cast(read_pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(chunk)
        return 1

    def get_information(handle: int, pointer: object) -> int:
        state = os.fstat(handle)
        information = ctypes.cast(
            pointer,
            ctypes.POINTER(freeze._WindowsFileInformation),
        ).contents
        information.dwFileAttributes = 0x80
        information.dwVolumeSerialNumber = state.st_dev
        information.nFileIndexHigh = state.st_ino >> 32
        information.nFileIndexLow = state.st_ino & 0xFFFFFFFF
        return 1

    def set_information(
        handle: int,
        information_class: int,
        pointer: object,
        size: int,
    ) -> int:
        assert information_class == freeze._WINDOWS_FILE_DISPOSITION_INFO
        assert size == ctypes.sizeof(freeze._WindowsFileDispositionInformation)
        disposition = ctypes.cast(
            pointer,
            ctypes.POINTER(freeze._WindowsFileDispositionInformation),
        ).contents
        assert bool(disposition.DeleteFile)
        disposition_handles.append(handle)
        pending_delete.add(handle)
        return 1

    def close_handle(handle: int) -> int:
        close_handles.append(handle)
        if handle in pending_delete:
            paths_by_handle[handle].unlink()
        os.close(handle)
        return 1

    def forbidden_fchmod(*args: object) -> None:
        raise AssertionError("Windows materialization touched os.fchmod")

    def flush_file_buffers(handle: int) -> int:
        os.fsync(handle)
        return 1

    monkeypatch.setattr(freeze.sys, "platform", "win32")
    monkeypatch.setattr(freeze, "_WINDOWS_CREATE_FILE", create_file)
    monkeypatch.setattr(freeze, "_WINDOWS_GET_FILE_INFORMATION_BY_HANDLE", get_information)
    monkeypatch.setattr(freeze, "_WINDOWS_SET_FILE_INFORMATION_BY_HANDLE", set_information)
    monkeypatch.setattr(freeze, "_WINDOWS_DUPLICATE_HANDLE", duplicate_handle, raising=False)
    monkeypatch.setattr(freeze, "_WINDOWS_GET_CURRENT_PROCESS", lambda: -1, raising=False)
    monkeypatch.setattr(freeze, "_WINDOWS_WRITE_FILE", write_file, raising=False)
    monkeypatch.setattr(freeze, "_WINDOWS_READ_FILE", read_file, raising=False)
    monkeypatch.setattr(
        freeze,
        "_WINDOWS_FLUSH_FILE_BUFFERS",
        flush_file_buffers,
        raising=False,
    )
    monkeypatch.setattr(freeze, "_WINDOWS_CLOSE_HANDLE", close_handle)
    monkeypatch.setattr(freeze.os, "fchmod", forbidden_fchmod)
    monkeypatch.setattr(freeze.tempfile, "tempdir", str(tmp_path))

    exposed_paths: tuple[str, ...] = ()
    with freeze.materialize_verified_market_data(runner_fixture.manifest) as selected:
        exposed_paths = selected.view.coverage().all_files()
        assert all(Path(path).is_file() for path in exposed_paths)
        selected.verify()

    writer_calls = [call for call in create_calls if call[2] == freeze._WINDOWS_CREATE_NEW]
    assert len(writer_calls) == len(runner_fixture.manifest.files)
    assert all(
        access
        == freeze._WINDOWS_GENERIC_READ
        | freeze._WINDOWS_GENERIC_WRITE
        | freeze._WINDOWS_DELETE
        for access, _, _, _ in writer_calls
    )
    assert all(
        sharing
        == freeze._WINDOWS_FILE_SHARE_READ
        | freeze._WINDOWS_FILE_SHARE_DELETE
        for _, sharing, _, _ in writer_calls
    )
    assert duplicate_access == [
        freeze._WINDOWS_GENERIC_READ | freeze._WINDOWS_DELETE
    ] * len(runner_fixture.manifest.files)
    assert len(disposition_handles) == len(runner_fixture.manifest.files)
    assert all(handle in close_handles for handle in disposition_handles)
    assert all(not Path(path).exists() for path in exposed_paths)


def test_windows_owner_reduction_failure_disposes_and_closes_writer_once(
    runner_fixture: RunnerFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.data.freeze as freeze

    paths_by_handle: dict[int, Path] = {}
    pending_delete: set[int] = set()
    close_handles: list[int] = []
    disposition_handles: list[int] = []

    def create_file(
        path: str,
        access: int,
        sharing: int,
        security: object,
        creation: int,
        attributes: int,
        template: object,
    ) -> int:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        paths_by_handle[descriptor] = Path(path)
        return descriptor

    def get_information(handle: int, pointer: object) -> int:
        state = os.fstat(handle)
        information = ctypes.cast(
            pointer,
            ctypes.POINTER(freeze._WindowsFileInformation),
        ).contents
        information.dwFileAttributes = 0x80
        information.dwVolumeSerialNumber = state.st_dev
        information.nFileIndexHigh = state.st_ino >> 32
        information.nFileIndexLow = state.st_ino & 0xFFFFFFFF
        return 1

    def write_file(
        handle: int,
        buffer: object,
        length: int,
        written_pointer: object,
        overlapped: object,
    ) -> int:
        written = os.write(handle, ctypes.string_at(buffer, length))
        ctypes.cast(written_pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = written
        return 1

    def set_information(
        handle: int,
        information_class: int,
        pointer: object,
        size: int,
    ) -> int:
        disposition_handles.append(handle)
        pending_delete.add(handle)
        return 1

    def close_handle(handle: int) -> int:
        close_handles.append(handle)
        if handle in pending_delete:
            paths_by_handle[handle].unlink()
        os.close(handle)
        return 1

    def flush_file_buffers(handle: int) -> int:
        os.fsync(handle)
        return 1

    monkeypatch.setattr(freeze.sys, "platform", "win32")
    monkeypatch.setattr(freeze, "_WINDOWS_CREATE_FILE", create_file)
    monkeypatch.setattr(freeze, "_WINDOWS_GET_FILE_INFORMATION_BY_HANDLE", get_information)
    monkeypatch.setattr(freeze, "_WINDOWS_SET_FILE_INFORMATION_BY_HANDLE", set_information)
    monkeypatch.setattr(freeze, "_WINDOWS_DUPLICATE_HANDLE", lambda *args: 0)
    monkeypatch.setattr(freeze, "_WINDOWS_GET_CURRENT_PROCESS", lambda: -1)
    monkeypatch.setattr(freeze, "_WINDOWS_WRITE_FILE", write_file)
    monkeypatch.setattr(freeze, "_WINDOWS_READ_FILE", lambda *args: 0)
    monkeypatch.setattr(freeze, "_WINDOWS_FLUSH_FILE_BUFFERS", flush_file_buffers)
    monkeypatch.setattr(freeze, "_WINDOWS_CLOSE_HANDLE", close_handle)
    monkeypatch.setattr(freeze.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        freeze.os,
        "fchmod",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("Windows materialization touched os.fchmod")
        ),
    )

    with pytest.raises(RuntimeError, match="owner handle reduction failed"):
        with freeze.materialize_verified_market_data(runner_fixture.manifest):
            raise AssertionError("materialization must fail before yielding")

    assert disposition_handles == close_handles
    assert len(close_handles) == 1
    assert list(tmp_path.glob("evosport-selected-*.parquet")) == []


def test_materialization_setup_error_never_adopts_a_replacement_root(
    runner_fixture: RunnerFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_darwin_materialization()
    import evosport.data.freeze as freeze

    external = tmp_path / "external-replacement"
    external.mkdir(mode=0o755)
    sentinel = external / "sentinel.txt"
    sentinel.write_text("external-content", encoding="utf-8")
    replacement_path: Path | None = None
    moved_owned_root: Path | None = None

    def fail_after_root_swap(**kwargs: object) -> object:
        nonlocal replacement_path, moved_owned_root
        physical_path = kwargs["physical_path"]
        assert isinstance(physical_path, Path)
        replacement_path = physical_path.parent
        moved_owned_root = replacement_path.with_name(f"{replacement_path.name}-moved")
        os.rename(replacement_path, moved_owned_root)
        replacement_path.symlink_to(external, target_is_directory=True)
        raise RuntimeError("injected capture failure")

    monkeypatch.setattr(freeze, "_capture_manifest_file", fail_after_root_swap)

    try:
        with pytest.raises(RuntimeError, match="injected capture failure") as exc_info:
            with freeze.materialize_verified_market_data(runner_fixture.manifest):
                raise AssertionError("materialization must fail before yielding")

        assert str(exc_info.value) == "injected capture failure"
        assert replacement_path is not None and replacement_path.is_symlink()
        assert moved_owned_root is not None and moved_owned_root.is_dir()
        assert sentinel.read_text(encoding="utf-8") == "external-content"
        assert S_IMODE(external.stat().st_mode) == 0o755
    finally:
        if replacement_path is not None and replacement_path.is_symlink():
            replacement_path.unlink()
        if moved_owned_root is not None and moved_owned_root.is_dir():
            moved_owned_root.rmdir()


def test_materialization_cleanup_root_swap_at_permission_seam_preserves_replacement(
    runner_fixture: RunnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_darwin_materialization()
    import evosport.data.freeze as freeze

    original_chmod = os.chmod
    original_fchmod = os.fchmod
    directory_descriptors = _track_open_directory_descriptors(monkeypatch)
    root: Path | None = None
    moved_owned_root: Path | None = None
    replacement_files: list[Path] = []
    swapped = False

    def swap_root() -> None:
        nonlocal moved_owned_root, swapped
        assert root is not None
        moved_owned_root = root.with_name(f"{root.name}-moved-owned")
        os.rename(root, moved_owned_root)
        root.mkdir(mode=0o755)
        original_chmod(root, 0o755)
        for index in range(len(runner_fixture.manifest.files)):
            replacement_file = root / f"selected-{index:04d}.parquet"
            replacement_file.write_bytes(f"external-root-replacement-{index}".encode())
            original_chmod(replacement_file, 0o640)
            replacement_files.append(replacement_file)
        swapped = True

    def race_chmod(path: object, mode: int, *args: object, **kwargs: object) -> None:
        if mode == 0o700 and not swapped and root is not None and Path(path) == root:
            swap_root()
        original_chmod(path, mode, *args, **kwargs)

    def race_fchmod(descriptor: int, mode: int) -> None:
        if mode == 0o700 and not swapped and S_ISDIR(os.fstat(descriptor).st_mode):
            swap_root()
        original_fchmod(descriptor, mode)

    try:
        with monkeypatch.context() as race:
            race.setattr(os, "chmod", race_chmod)
            race.setattr(os, "fchmod", race_fchmod)
            with pytest.raises(RuntimeError, match="cleanup refused"):
                with freeze.materialize_verified_market_data(runner_fixture.manifest) as selected:
                    root = Path(selected.view.coverage().all_files()[0]).parent

        assert swapped is True
        assert root is not None and root.is_dir()
        assert len(replacement_files) == len(runner_fixture.manifest.files)
        assert replacement_files[0].read_bytes() == b"external-root-replacement-0"
        assert S_IMODE(root.stat().st_mode) == 0o755
        assert all(S_IMODE(path.stat().st_mode) == 0o640 for path in replacement_files)
        assert moved_owned_root is not None and moved_owned_root.is_dir()
        _assert_descriptors_closed(directory_descriptors)
    finally:
        for replacement_file in replacement_files:
            if replacement_file.exists():
                original_chmod(replacement_file, 0o600)
                replacement_file.unlink()
        if root is not None and root.is_dir():
            original_chmod(root, 0o700)
            root.rmdir()
        if moved_owned_root is not None and moved_owned_root.is_dir():
            original_chmod(moved_owned_root, 0o700)
            for child in moved_owned_root.iterdir():
                original_chmod(child, 0o600)
                child.unlink()
            moved_owned_root.rmdir()


def test_materialization_cleanup_file_swap_at_permission_seam_preserves_replacement(
    runner_fixture: RunnerFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_darwin_materialization()
    import evosport.data.freeze as freeze

    original_chmod = os.chmod
    original_fchmod = os.fchmod
    directory_descriptors = _track_open_directory_descriptors(monkeypatch)
    physical_path: Path | None = None
    moved_owned_file = tmp_path / "moved-owned-selected.parquet"
    swapped = False

    def swap_file() -> None:
        nonlocal swapped
        assert physical_path is not None
        os.rename(physical_path, moved_owned_file)
        physical_path.write_bytes(b"external-file-replacement")
        original_chmod(physical_path, 0o640)
        swapped = True

    def race_chmod(path: object, mode: int, *args: object, **kwargs: object) -> None:
        if mode == 0o600 and not swapped and physical_path is not None and Path(path) == physical_path:
            swap_file()
        original_chmod(path, mode, *args, **kwargs)

    def race_fchmod(descriptor: int, mode: int) -> None:
        if mode == 0o600 and not swapped and not S_ISDIR(os.fstat(descriptor).st_mode):
            swap_file()
        original_fchmod(descriptor, mode)

    root: Path | None = None
    try:
        with monkeypatch.context() as race:
            race.setattr(os, "chmod", race_chmod)
            race.setattr(os, "fchmod", race_fchmod)
            with pytest.raises(RuntimeError, match="replacement content"):
                with freeze.materialize_verified_market_data(runner_fixture.manifest) as selected:
                    physical_path = Path(selected.view.coverage().all_files()[0])
                    root = physical_path.parent

        assert swapped is True
        assert physical_path is not None
        assert physical_path.read_bytes() == b"external-file-replacement"
        assert S_IMODE(physical_path.stat().st_mode) == 0o640
        assert moved_owned_file.is_file()
        _assert_descriptors_closed(directory_descriptors)
    finally:
        if physical_path is not None and physical_path.exists():
            original_chmod(physical_path, 0o600)
            physical_path.unlink()
        if root is not None and root.is_dir():
            original_chmod(root, 0o700)
            for child in root.iterdir():
                original_chmod(child, 0o600)
                child.unlink()
            root.rmdir()
        if moved_owned_file.exists():
            original_chmod(moved_owned_file, 0o600)
            moved_owned_file.unlink()


def test_materialization_cleanup_file_swap_inside_delete_preserves_replacement(
    runner_fixture: RunnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_darwin_materialization()
    import evosport.data.freeze as freeze

    manifest = runner_fixture.manifest.model_copy(
        update={"files": (runner_fixture.manifest.files[0],)}
    )
    original_unlink = os.unlink
    original_delete_owned_object = getattr(freeze, "_delete_owned_object", None)
    physical_path: Path | None = None
    root: Path | None = None
    moved_owned_file: Path | None = None
    swapped = False

    def swap_file() -> None:
        nonlocal moved_owned_file, swapped
        assert physical_path is not None
        moved_owned_file = physical_path.parent.with_name(
            f"{physical_path.parent.name}-moved-owned-file"
        )
        os.rename(physical_path, moved_owned_file)
        physical_path.write_bytes(b"external-file-replacement-at-delete")
        os.chmod(physical_path, 0o640)
        swapped = True

    def race_unlink(path: object, *args: object, **kwargs: object) -> None:
        if (
            not swapped
            and physical_path is not None
            and path == physical_path.name
            and kwargs.get("dir_fd") is not None
        ):
            swap_file()
        original_unlink(path, *args, **kwargs)

    def race_delete_owned_object(reference: object) -> None:
        if not swapped and not getattr(reference, "is_directory", False):
            swap_file()
        assert original_delete_owned_object is not None
        original_delete_owned_object(reference)

    try:
        with monkeypatch.context() as race:
            race.setattr(os, "unlink", race_unlink)
            race.setattr(
                freeze,
                "_delete_owned_object",
                race_delete_owned_object,
                raising=False,
            )
            with pytest.raises(RuntimeError, match="replacement content"):
                with freeze.materialize_verified_market_data(manifest) as selected:
                    physical_path = Path(selected.view.coverage().all_files()[0])
                    root = physical_path.parent

        assert swapped is True
        assert physical_path is not None and physical_path.is_file()
        assert physical_path.read_bytes() == b"external-file-replacement-at-delete"
        assert S_IMODE(physical_path.stat().st_mode) == 0o640
    finally:
        if physical_path is not None and physical_path.exists():
            os.chmod(physical_path, 0o600)
            original_unlink(physical_path)
        if root is not None and root.is_dir():
            os.chmod(root, 0o700)
            for child in root.iterdir():
                os.chmod(child, 0o600)
                original_unlink(child)
            root.rmdir()
        if moved_owned_file is not None and moved_owned_file.exists():
            os.chmod(moved_owned_file, 0o600)
            original_unlink(moved_owned_file)


def test_materialization_cleanup_root_swap_inside_delete_preserves_replacement(
    runner_fixture: RunnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_darwin_materialization()
    import evosport.data.freeze as freeze

    manifest = runner_fixture.manifest.model_copy(
        update={"files": (runner_fixture.manifest.files[0],)}
    )
    original_rmdir = os.rmdir
    original_delete_owned_object = getattr(freeze, "_delete_owned_object", None)
    root: Path | None = None
    moved_owned_root: Path | None = None
    swapped = False

    def swap_root() -> None:
        nonlocal moved_owned_root, swapped
        assert root is not None
        moved_owned_root = root.with_name(f"{root.name}-moved-owned-root")
        os.rename(root, moved_owned_root)
        root.mkdir(mode=0o755)
        os.chmod(root, 0o755)
        swapped = True

    def race_rmdir(path: object, *args: object, **kwargs: object) -> None:
        if (
            not swapped
            and root is not None
            and path == root.name
            and kwargs.get("dir_fd") is not None
        ):
            swap_root()
        original_rmdir(path, *args, **kwargs)

    def race_delete_owned_object(reference: object) -> None:
        if not swapped and getattr(reference, "is_directory", False):
            swap_root()
        assert original_delete_owned_object is not None
        original_delete_owned_object(reference)

    try:
        with monkeypatch.context() as race:
            race.setattr(os, "rmdir", race_rmdir)
            race.setattr(
                freeze,
                "_delete_owned_object",
                race_delete_owned_object,
                raising=False,
            )
            with pytest.raises(RuntimeError, match="changed materialization root"):
                with freeze.materialize_verified_market_data(manifest) as selected:
                    root = Path(selected.view.coverage().all_files()[0]).parent

        assert swapped is True
        assert root is not None and root.is_dir()
        assert S_IMODE(root.stat().st_mode) == 0o755
    finally:
        if root is not None and root.is_dir():
            os.chmod(root, 0o700)
            for child in root.iterdir():
                os.chmod(child, 0o600)
                child.unlink()
            original_rmdir(root)
        if moved_owned_root is not None and moved_owned_root.is_dir():
            os.chmod(moved_owned_root, 0o700)
            for child in moved_owned_root.iterdir():
                os.chmod(child, 0o600)
                child.unlink()
            original_rmdir(moved_owned_root)


def test_materialization_setup_chmod_root_swap_cannot_touch_replacement(
    runner_fixture: RunnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_darwin_materialization()
    import evosport.data.freeze as freeze

    original_open = os.open
    original_chmod = os.chmod
    original_fchmod = os.fchmod
    opened_paths: dict[int, Path] = {}
    directory_descriptors: list[int] = []
    root: Path | None = None
    moved_owned_root: Path | None = None
    replacement_sentinel: Path | None = None
    swapped = False

    def track_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if S_ISDIR(os.fstat(descriptor).st_mode):
            directory_descriptors.append(descriptor)
        resolved = Path(path)
        if dir_fd is not None and dir_fd in opened_paths:
            resolved = opened_paths[dir_fd] / resolved
        opened_paths[descriptor] = resolved
        return descriptor

    def swap_root(path: Path) -> None:
        nonlocal root, moved_owned_root, replacement_sentinel, swapped
        root = path
        moved_owned_root = path.with_name(f"{path.name}-moved-owned")
        os.rename(path, moved_owned_root)
        path.mkdir(mode=0o755)
        original_chmod(path, 0o755)
        replacement_sentinel = path / "sentinel.txt"
        replacement_sentinel.write_text("external-setup-replacement", encoding="utf-8")
        original_chmod(replacement_sentinel, 0o640)
        swapped = True

    def race_chmod(path: object, mode: int, *args: object, **kwargs: object) -> None:
        if mode == 0o500 and not swapped:
            swap_root(Path(path))
        original_chmod(path, mode, *args, **kwargs)

    def race_fchmod(descriptor: int, mode: int) -> None:
        if mode == 0o500 and not swapped and S_ISDIR(os.fstat(descriptor).st_mode):
            swap_root(opened_paths[descriptor])
        original_fchmod(descriptor, mode)

    try:
        with monkeypatch.context() as race:
            race.setattr(os, "open", track_open)
            race.setattr(os, "chmod", race_chmod)
            race.setattr(os, "fchmod", race_fchmod)
            with pytest.raises(RuntimeError):
                with freeze.materialize_verified_market_data(runner_fixture.manifest):
                    raise AssertionError("setup swap must fail before yielding")

        assert swapped is True
        assert root is not None and root.is_dir()
        assert replacement_sentinel is not None
        assert replacement_sentinel.read_text(encoding="utf-8") == "external-setup-replacement"
        assert S_IMODE(root.stat().st_mode) == 0o755
        assert S_IMODE(replacement_sentinel.stat().st_mode) == 0o640
        _assert_descriptors_closed(directory_descriptors)
    finally:
        if root is not None and root.is_dir():
            original_chmod(root, 0o700)
        if replacement_sentinel is not None and replacement_sentinel.exists():
            original_chmod(replacement_sentinel, 0o600)
            replacement_sentinel.unlink()
        if root is not None and root.is_dir():
            root.rmdir()
        if moved_owned_root is not None and moved_owned_root.is_dir():
            original_chmod(moved_owned_root, 0o700)
            for child in moved_owned_root.iterdir():
                original_chmod(child, 0o600)
                child.unlink()
            moved_owned_root.rmdir()


def test_partial_capture_cleanup_file_swap_cannot_touch_replacement(
    runner_fixture: RunnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_darwin_materialization()
    import evosport.data.freeze as freeze

    bad_file = runner_fixture.manifest.files[0].model_copy(update={"sha256": "f" * 64})
    bad_manifest = runner_fixture.manifest.model_copy(
        update={"files": (bad_file, *runner_fixture.manifest.files[1:])}
    )
    original_open = os.open
    original_chmod = os.chmod
    original_fchmod = os.fchmod
    opened_paths: dict[int, Path] = {}
    directory_descriptors: list[int] = []
    replacement_path: Path | None = None
    moved_owned_file: Path | None = None
    swapped = False

    def track_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if S_ISDIR(os.fstat(descriptor).st_mode):
            directory_descriptors.append(descriptor)
        raw_path = Path(path)
        if dir_fd is not None and dir_fd in opened_paths:
            raw_path = opened_paths[dir_fd] / raw_path
        opened_paths[descriptor] = raw_path
        return descriptor

    def swap_file(path: Path) -> None:
        nonlocal replacement_path, moved_owned_file, swapped
        replacement_path = path
        moved_owned_file = path.with_name(f"{path.name}-moved-owned")
        os.rename(path, moved_owned_file)
        path.write_bytes(b"external-partial-replacement")
        original_chmod(path, 0o640)
        swapped = True

    def race_chmod(path: object, mode: int, *args: object, **kwargs: object) -> None:
        if mode == 0o600 and not swapped:
            swap_file(Path(path))
        original_chmod(path, mode, *args, **kwargs)

    def race_fchmod(descriptor: int, mode: int) -> None:
        if mode == 0o600 and not swapped and not S_ISDIR(os.fstat(descriptor).st_mode):
            swap_file(opened_paths[descriptor])
        original_fchmod(descriptor, mode)

    try:
        with monkeypatch.context() as race:
            race.setattr(os, "open", track_open)
            race.setattr(os, "chmod", race_chmod)
            race.setattr(os, "fchmod", race_fchmod)
            with pytest.raises(RuntimeError, match="snapshot integrity") as exc_info:
                with freeze.materialize_verified_market_data(bad_manifest):
                    raise AssertionError("bad capture must fail before yielding")

        assert str(exc_info.value).startswith("snapshot integrity verification failed")
        assert swapped is True
        assert replacement_path is not None
        assert replacement_path.read_bytes() == b"external-partial-replacement"
        assert S_IMODE(replacement_path.stat().st_mode) == 0o640
        assert moved_owned_file is not None and not moved_owned_file.exists()
        _assert_descriptors_closed(directory_descriptors)
    finally:
        root = replacement_path.parent if replacement_path is not None else None
        if replacement_path is not None and replacement_path.exists():
            original_chmod(replacement_path, 0o600)
            replacement_path.unlink()
        if moved_owned_file is not None and moved_owned_file.exists():
            original_chmod(moved_owned_file, 0o600)
            moved_owned_file.unlink()
        if root is not None and root.is_dir():
            original_chmod(root, 0o700)
            for child in root.iterdir():
                original_chmod(child, 0o600)
                child.unlink()
            root.rmdir()


def test_capture_error_remains_primary_when_partial_file_cleanup_fails(
    runner_fixture: RunnerFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_darwin_materialization()
    import evosport.data.freeze as freeze

    physical_path = tmp_path / "partial-selected.parquet"
    file_record = runner_fixture.manifest.files[0].model_copy(
        update={"sha256": "f" * 64}
    )
    root_descriptor = os.open(
        tmp_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    original_unlink = os.unlink

    def refuse_partial_unlink(path: object, *args: object, **kwargs: object) -> None:
        if path == physical_path.name and kwargs.get("dir_fd") == root_descriptor:
            raise PermissionError("injected partial cleanup failure")
        original_unlink(path, *args, **kwargs)

    def refuse_identity_delete(reference: object) -> None:
        raise PermissionError("injected partial cleanup failure")

    try:
        with monkeypatch.context() as cleanup_failure:
            cleanup_failure.setattr(os, "unlink", refuse_partial_unlink)
            cleanup_failure.setattr(
                freeze,
                "_delete_owned_object",
                refuse_identity_delete,
                raising=False,
            )
            with pytest.raises(RuntimeError, match="snapshot integrity") as exc_info:
                freeze._capture_manifest_file(
                    file_record=file_record,
                    physical_path=physical_path,
                    root_descriptor=root_descriptor,
                )

        assert any(
            "partial selected data cleanup failed" in note
            for note in exc_info.value.__notes__
        )
    finally:
        os.close(root_descriptor)
        if physical_path.exists():
            physical_path.chmod(0o600)
            original_unlink(physical_path)


def test_partial_capture_swap_inside_delete_preserves_replacement_and_primary_error(
    runner_fixture: RunnerFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_darwin_materialization()
    import evosport.data.freeze as freeze

    physical_path = tmp_path / "partial-selected.parquet"
    moved_owned_file = tmp_path / "partial-selected-moved-owned.parquet"
    file_record = runner_fixture.manifest.files[0]
    primary_error = RuntimeError("injected primary capture failure")
    root_descriptor = os.open(
        tmp_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    original_fsync = os.fsync
    original_unlink = os.unlink
    original_delete_owned_object = getattr(freeze, "_delete_owned_object", None)
    swapped = False

    def fail_capture_fsync(descriptor: int) -> None:
        if not swapped:
            raise primary_error
        original_fsync(descriptor)

    def swap_file() -> None:
        nonlocal swapped
        os.rename(physical_path, moved_owned_file)
        physical_path.write_bytes(b"external-partial-replacement-at-delete")
        os.chmod(physical_path, 0o640)
        swapped = True

    def race_unlink(path: object, *args: object, **kwargs: object) -> None:
        if (
            not swapped
            and path == physical_path.name
            and kwargs.get("dir_fd") == root_descriptor
        ):
            swap_file()
        original_unlink(path, *args, **kwargs)

    def race_delete_owned_object(reference: object) -> None:
        if not swapped and not getattr(reference, "is_directory", False):
            swap_file()
        assert original_delete_owned_object is not None
        original_delete_owned_object(reference)

    try:
        with monkeypatch.context() as race:
            race.setattr(os, "fsync", fail_capture_fsync)
            race.setattr(os, "unlink", race_unlink)
            race.setattr(
                freeze,
                "_delete_owned_object",
                race_delete_owned_object,
                raising=False,
            )
            with pytest.raises(RuntimeError) as exc_info:
                freeze._capture_manifest_file(
                    file_record=file_record,
                    physical_path=physical_path,
                    root_descriptor=root_descriptor,
                )

        assert exc_info.value is primary_error
        assert swapped is True
        assert physical_path.read_bytes() == b"external-partial-replacement-at-delete"
        assert S_IMODE(physical_path.stat().st_mode) == 0o640
        assert any(
            "partial selected data cleanup failed" in note
            for note in exc_info.value.__notes__
        )
    finally:
        os.close(root_descriptor)
        if physical_path.exists():
            os.chmod(physical_path, 0o600)
            original_unlink(physical_path)
        if moved_owned_file.exists():
            os.chmod(moved_owned_file, 0o600)
            original_unlink(moved_owned_file)


@pytest.mark.asyncio
async def test_football_identity_change_during_gateway_aborts_before_publication(
    runner_fixture: RunnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.experiments.runner as runner_module

    stable = _current_football_identity(
        runner_fixture.manifest.football,
        runner_fixture.manifest.provider_dataset_ids,
    )
    changed = json.loads(json.dumps(stable))
    changed["settlement"]["source"] = "mutated-during-run"
    identities = [stable, stable, changed]

    async def load_identity(*args: object, **kwargs: object) -> dict[str, object]:
        return identities.pop(0)

    monkeypatch.setattr(runner_module, "load_current_football_identity", load_identity)
    gateway = AsyncMock(return_value=_successful_result(runner_fixture.manifest))
    registry = InMemoryRunRegistry()

    with pytest.raises(RuntimeError, match="changed during gateway execution"):
        await _runner(registry, gateway, runner_fixture.artifact_root).run(runner_fixture.spec_path)

    assert registry._by_fingerprint == {}
    assert not any(runner_fixture.artifact_root.iterdir())


class _FailingPublicationRegistry(InMemoryRunRegistry):
    async def publish(self, **values: str):
        raise RuntimeError("publication database failed")


class _NoteRejectingPrimaryError(RuntimeError):
    def add_note(self, note: str) -> None:
        raise LookupError("note attachment failed")


class _Python310FallbackError(RuntimeError):
    add_note = None


def test_python310_fallback_notes_attach_when_supported() -> None:
    import evosport.data.freeze as freeze
    import evosport.experiments.runner as runner

    error = _Python310FallbackError("fallback")

    runner._add_note(error, "runner fallback note")
    freeze._add_cleanup_note(error, "freeze fallback note")

    assert error.__notes__ == ["runner fallback note", "freeze fallback note"]


class _PostCommitPublicationRegistry(InMemoryRunRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.publish_error = RuntimeError("publication response was lost")

    async def publish(self, **values: str):
        await super().publish(**values)
        raise self.publish_error


class _ConflictingPostCommitPublicationRegistry(InMemoryRunRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.publish_error = RuntimeError("publication response was lost after conflict")

    async def publish(self, **values: str):
        await super().publish(**{**values, "result_sha256": "f" * 64})
        raise self.publish_error


class _UnavailableReconciliationRegistry(InMemoryRunRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.publish_error = _NoteRejectingPrimaryError(
            "publication database failed ambiguously"
        )
        self._publish_failed = False

    async def get_by_fingerprint(self, fingerprint: str):
        if self._publish_failed:
            raise OSError("publication lookup unavailable")
        return await super().get_by_fingerprint(fingerprint)

    async def publish(self, **values: str):
        self._publish_failed = True
        raise self.publish_error


class _CleanupRefusalPublicationRegistry(InMemoryRunRegistry):
    def __init__(self, artifact_root: Path) -> None:
        super().__init__()
        self._artifact_root = artifact_root
        self.publish_error = _NoteRejectingPrimaryError(
            "publication failed before registration"
        )

    async def publish(self, **values: str):
        artifact_dirs = [path for path in self._artifact_root.iterdir() if path.is_dir()]
        assert len(artifact_dirs) == 1
        artifact_dir = artifact_dirs[0]
        artifact_dir.chmod(0o755)
        (artifact_dir / "unexpected-external-file").write_text(
            "preserve-me",
            encoding="utf-8",
        )
        artifact_dir.chmod(0o555)
        raise self.publish_error


@pytest.mark.asyncio
async def test_publication_record_failure_removes_owned_unregistered_evidence(
    runner_fixture: RunnerFixture,
) -> None:
    gateway = AsyncMock()
    gateway.run.return_value = _successful_result(runner_fixture.manifest)

    with pytest.raises(RuntimeError, match="publication database failed"):
        await _runner(
            _FailingPublicationRegistry(),
            gateway,
            runner_fixture.artifact_root,
        ).run(runner_fixture.spec_path)

    assert not any(runner_fixture.artifact_root.iterdir())


@pytest.mark.asyncio
async def test_post_commit_publication_error_reconciles_exact_record_as_success(
    runner_fixture: RunnerFixture,
) -> None:
    gateway = AsyncMock()
    gateway.run.return_value = _successful_result(runner_fixture.manifest)
    registry = _PostCommitPublicationRegistry()

    outcome = await _runner(registry, gateway, runner_fixture.artifact_root).run(
        runner_fixture.spec_path
    )

    assert await registry.get_by_fingerprint(outcome.fingerprint) is not None
    assert outcome.artifact_dir.is_dir()
    assert (outcome.artifact_dir / "artifact-manifest.json").is_file()


@pytest.mark.asyncio
async def test_post_commit_publication_conflict_preserves_evidence_and_fails_closed(
    runner_fixture: RunnerFixture,
) -> None:
    gateway = AsyncMock()
    gateway.run.return_value = _successful_result(runner_fixture.manifest)
    registry = _ConflictingPostCommitPublicationRegistry()

    with pytest.raises(RuntimeError, match="publication conflict") as exc_info:
        await _runner(registry, gateway, runner_fixture.artifact_root).run(
            runner_fixture.spec_path
        )

    assert exc_info.value.__cause__ is registry.publish_error
    artifact_dirs = list(runner_fixture.artifact_root.iterdir())
    assert len(artifact_dirs) == 1
    assert (artifact_dirs[0] / "artifact-manifest.json").is_file()


@pytest.mark.asyncio
async def test_unavailable_publication_reconciliation_preserves_original_error_and_evidence(
    runner_fixture: RunnerFixture,
) -> None:
    gateway = AsyncMock()
    gateway.run.return_value = _successful_result(runner_fixture.manifest)
    registry = _UnavailableReconciliationRegistry()

    with pytest.raises(RuntimeError, match="publication database failed ambiguously") as exc_info:
        await _runner(registry, gateway, runner_fixture.artifact_root).run(
            runner_fixture.spec_path
        )

    assert exc_info.value is registry.publish_error
    assert any("reconciliation lookup failed" in note for note in exc_info.value.__notes__)
    assert registry._by_fingerprint == {}
    artifact_dirs = list(runner_fixture.artifact_root.iterdir())
    assert len(artifact_dirs) == 1
    assert (artifact_dirs[0] / "artifact-manifest.json").is_file()


@pytest.mark.asyncio
async def test_cleanup_refusal_note_cannot_mask_primary_publication_error(
    runner_fixture: RunnerFixture,
) -> None:
    gateway = AsyncMock()
    gateway.run.return_value = _successful_result(runner_fixture.manifest)
    registry = _CleanupRefusalPublicationRegistry(runner_fixture.artifact_root)

    with pytest.raises(
        _NoteRejectingPrimaryError,
        match="publication failed before registration",
    ) as exc_info:
        await _runner(registry, gateway, runner_fixture.artifact_root).run(
            runner_fixture.spec_path
        )

    assert exc_info.value is registry.publish_error
    assert registry._by_fingerprint == {}
    assert any("cleanup was refused" in note for note in exc_info.value.__notes__)
    assert any("unregistered evidence was preserved" in note for note in exc_info.value.__notes__)
    artifact_dirs = [path for path in runner_fixture.artifact_root.iterdir() if path.is_dir()]
    assert len(artifact_dirs) == 1
    assert (artifact_dirs[0] / "artifact-manifest.json").is_file()
    assert (artifact_dirs[0] / "unexpected-external-file").read_text(encoding="utf-8") == "preserve-me"
