from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from stat import S_IMODE

import pytest

from evosport.data import freeze
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
from evosport.experiments import runner
from evosport.semantics.football_binding import FootballDatasetBinding
from evosport.semantics.football_totals import FootballMatchResult


def _snapshot(tmp_path: Path) -> tuple[Path, Path]:
    start = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    close = start + timedelta(hours=1)
    payload = tmp_path / "snapshots__YES.parquet"
    payload.write_bytes(b"canonical-v2-payload")
    binding = FootballDatasetBinding(
        event=CanonicalSportsEvent(
            event_id="event",
            competition="league",
            season="2026",
            home_team="Home",
            away_team="Away",
            scheduled_start=close,
            actual_start=close,
            status=EventStatus.FINISHED,
        ),
        contract=CanonicalSportsContract(
            contract_id="contract",
            event_id="event",
            venue="fixture",
            venue_market_id="market",
            yes_token_id="YES",
            no_token_id="NO",
            market_type=MarketType.TOTAL_GOALS_OVER_UNDER,
            threshold=Decimal("2.5"),
            side="over",
            opens_at=start,
            closes_at=close,
            rule_version="fixture-v1",
            settlement=SettlementPolicy(),
        ),
        result=FootballMatchResult(
            regulation_home=2,
            regulation_away=1,
            status=EventStatus.FINISHED,
        ),
        result_time=TemporalEnvelope(
            event_time=close + timedelta(hours=2),
            observed_at=close + timedelta(hours=2, minutes=1),
            ingested_at=close + timedelta(hours=2, minutes=2),
        ),
    )
    file_record = DatasetFile(
        path=str(payload.resolve()),
        sha256=sha256_file(payload),
        size_bytes=payload.stat().st_size,
        provider_dataset_ids=("selected",),
        token_ids=("YES",),
    )
    manifest = DatasetManifest(
        schema_version=CATALOG_SCHEMA_VERSION,
        manifest_id=manifest_id_for(
            schema_version=CATALOG_SCHEMA_VERSION,
            source="homerun_catalog",
            token_ids=("YES",),
            start=start,
            end=close,
            files=(file_record,),
            provider_dataset_ids=("selected",),
            football=binding,
        ),
        source="homerun_catalog",
        token_ids=("YES",),
        start=start,
        end=close,
        files=(file_record,),
        provider_dataset_ids=("selected",),
        football=binding,
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    manifest_path = snapshot / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest_path, payload


def test_manifest_descriptor_replacement_during_verification_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _ = _snapshot(tmp_path)
    original_verify = freeze._verify_snapshot_tree

    def replace_after_tree_verification(target: Path, manifest: DatasetManifest) -> DatasetManifest:
        verified = original_verify(target, manifest)
        replacement = target / "replacement.json"
        replacement.write_bytes(manifest_path.read_bytes())
        os.replace(replacement, manifest_path)
        return verified

    monkeypatch.setattr(freeze, "_verify_snapshot_tree", replace_after_tree_verification)

    with pytest.raises(RuntimeError, match="snapshot integrity"):
        freeze.load_verified_snapshot(manifest_path)


def test_manifest_symlink_is_rejected(tmp_path: Path) -> None:
    manifest_path, _ = _snapshot(tmp_path)
    real_manifest = tmp_path / "real-manifest.json"
    manifest_path.replace(real_manifest)
    manifest_path.symlink_to(real_manifest)

    with pytest.raises(RuntimeError, match="snapshot integrity"):
        freeze.load_verified_snapshot(manifest_path)


def test_payload_parquet_symlink_is_rejected(tmp_path: Path) -> None:
    manifest_path, payload = _snapshot(tmp_path)
    real_payload = tmp_path / "real-payload.parquet"
    payload.replace(real_payload)
    payload.symlink_to(real_payload)

    with pytest.raises(RuntimeError, match="snapshot integrity"):
        freeze.load_verified_snapshot(manifest_path)


@pytest.mark.parametrize("nested", [False, True], ids=["top-level", "nested-file"])
def test_unknown_manifest_fields_are_rejected(tmp_path: Path, nested: bool) -> None:
    manifest_path, _ = _snapshot(tmp_path)
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if nested:
        document["files"][0]["unknown_nested"] = True
    else:
        document["unknown_top_level"] = True
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RuntimeError, match="snapshot integrity"):
        freeze.load_verified_snapshot(manifest_path)


def test_stale_and_concurrent_snapshot_locks_are_refused_without_deleting_owner(
    tmp_path: Path,
) -> None:
    stale = tmp_path / ".stale.lock"
    stale.write_text("pid=external\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="busy or stale"):
        with freeze._publication_lock(tmp_path, "stale"):
            raise AssertionError("unreachable")
    assert stale.read_text(encoding="utf-8") == "pid=external\n"

    owned = tmp_path / ".concurrent.lock"
    with freeze._publication_lock(tmp_path, "concurrent"):
        owner_bytes = owned.read_bytes()
        with pytest.raises(RuntimeError, match="busy or stale"):
            with freeze._publication_lock(tmp_path, "concurrent"):
                raise AssertionError("unreachable")
        assert owned.read_bytes() == owner_bytes
    assert not owned.exists()


def test_cleanup_refuses_replaced_artifact_tree_without_chmod_or_deletion(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "owned.txt").write_text("owned", encoding="utf-8")
    owned = runner._owned_artifact_tree(artifact)
    original = tmp_path / "original"
    artifact.rename(original)
    artifact.mkdir()
    external = artifact / "external.txt"
    external.write_text("external replacement", encoding="utf-8")
    external.chmod(0o644)
    artifact.chmod(0o755)

    assert runner._cleanup_owned_directory(owned) is False
    assert external.read_text(encoding="utf-8") == "external replacement"
    assert S_IMODE(external.stat().st_mode) == 0o644
    assert S_IMODE(artifact.stat().st_mode) == 0o755
    assert (original / "owned.txt").read_text(encoding="utf-8") == "owned"
