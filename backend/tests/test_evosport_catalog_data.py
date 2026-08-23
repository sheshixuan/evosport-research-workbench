from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest
import yaml
from sqlalchemy import select

from models.database import Base, ProviderDataset
from evosport.data.freeze import freeze_provider_datasets
from evosport.data.manifest import sha256_file
from evosport.domain.sports import (
    CanonicalSportsContract,
    CanonicalSportsEvent,
    EventStatus,
    MarketType,
    SettlementPolicy,
)
from evosport.domain.time import TemporalEnvelope
from evosport.semantics import football_binding
from evosport.semantics.football_binding import FootballDatasetBinding, store_football_settlement
from evosport.semantics.football_totals import FootballMatchResult
from services.external_data.parquet_schema import SNAPSHOT_SCHEMA
from services.marketdata.coverage import resolve_coverage
from services.marketdata.view import MarketDataView
from services.marketdata.book_source import MarketDataViewSource
from services.marketdata.writer import write_canonical_table
from services.backtest.settlement_store import get_settlement_records
from evosport.experiments.gateway import HomerunBacktestGateway
from evosport.experiments.environment import active_distribution_pins
from evosport.experiments.models import EvidencePublication
from evosport.experiments.registry import SqlRunRegistry
from evosport.experiments.runner import ExperimentRunner
from tests.postgres_test_db import build_postgres_session_factory


def _snapshot_table(token_id: str, observed_at: datetime, bid: float) -> pa.Table:
    observed_at_us = int(observed_at.timestamp() * 1_000_000)
    return pa.table(
        {
            "token_id": pa.array([token_id], pa.string()),
            "observed_at_us": pa.array([observed_at_us], pa.int64()),
            "sequence": pa.array([1], pa.int64()),
            "best_bid": pa.array([bid], pa.float64()),
            "best_ask": pa.array([bid + 0.02], pa.float64()),
            "spread_bps": pa.array([None], pa.float64()),
            "bids_price": pa.array([[bid]], pa.list_(pa.float64())),
            "bids_size": pa.array([[100.0]], pa.list_(pa.float64())),
            "asks_price": pa.array([[bid + 0.02]], pa.list_(pa.float64())),
            "asks_size": pa.array([[100.0]], pa.list_(pa.float64())),
            "trade_price": pa.array([None], pa.float64()),
            "trade_size": pa.array([None], pa.float64()),
            "trade_side": pa.array([None], pa.string()),
        },
        schema=SNAPSHOT_SCHEMA,
    )


def _write_dataset_file(root: Path, token_id: str, observed_at: datetime, bid: float) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"snapshots__{token_id}.parquet"
    write_canonical_table(
        _snapshot_table(token_id, observed_at, bid),
        dest_path=path,
        kind="snapshots",
        provider="evosport-test",
    )
    return path


def _write_dataset_series(
    root: Path,
    token_id: str,
    points: list[tuple[datetime, float]],
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    tables = [_snapshot_table(token_id, observed_at, bid) for observed_at, bid in points]
    path = root / f"snapshots__{token_id}.parquet"
    write_canonical_table(
        pa.concat_tables(tables),
        dest_path=path,
        kind="snapshots",
        provider="evosport-test",
    )
    return path


def _lineage_binding(start: datetime, close: datetime) -> FootballDatasetBinding:
    return FootballDatasetBinding(
        event=CanonicalSportsEvent(
            event_id="event-lineage",
            competition="fixture-league",
            season="2026",
            home_team="Home",
            away_team="Away",
            scheduled_start=close,
            actual_start=close,
            status=EventStatus.FINISHED,
        ),
        contract=CanonicalSportsContract(
            contract_id="contract-lineage",
            event_id="event-lineage",
            venue="evosport-test",
            venue_market_id="market-lineage",
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


def _lineage_dataset(
    *,
    dataset_id: str,
    root: Path,
    start: datetime,
    close: datetime,
) -> ProviderDataset:
    return ProviderDataset(
        id=dataset_id,
        provider="evosport-test",
        external_id=dataset_id,
        asset_class="prediction",
        token_ids_json=["YES", "NO"],
        start_ts=start.replace(tzinfo=None),
        end_ts=close.replace(tzinfo=None),
        snapshot_count=2,
        trade_count=0,
        payload_json={
            "canonical": True,
            "schema_version": "snapshots_v2",
            "sport": "football",
            "event_id": "event-lineage",
            "condition_id": "market-lineage",
            "clob_token_up": "YES",
            "clob_token_down": "NO",
            "market_type": "total_goals_over_under",
            "threshold": "2.5",
            "market_start_time": start.isoformat(),
            "market_end_time": close.isoformat(),
        },
        storage_type="parquet",
        storage_uri=root.resolve().as_uri(),
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_explicit_dataset_ids_exclude_conflicting_same_token_catalog_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "evosport_dataset_filter")
    try:
        import models.database as database

        monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        selected_file = _write_dataset_file(tmp_path / "selected", "YES", start, 0.40)
        contaminating_file = _write_dataset_file(tmp_path / "contaminating", "YES", start, 0.80)

        async with session_factory() as session:
            session.add_all(
                [
                    ProviderDataset(
                        id="selected",
                        provider="evosport-test",
                        external_id="selected",
                        asset_class="prediction",
                        token_ids_json=["YES"],
                        start_ts=start.replace(tzinfo=None),
                        end_ts=end.replace(tzinfo=None),
                        snapshot_count=1,
                        trade_count=0,
                        payload_json={},
                        storage_type="parquet",
                        storage_uri=selected_file.parent.resolve().as_uri(),
                    ),
                    ProviderDataset(
                        id="contaminating",
                        provider="evosport-test",
                        external_id="contaminating",
                        asset_class="prediction",
                        token_ids_json=["YES"],
                        start_ts=start.replace(tzinfo=None),
                        end_ts=end.replace(tzinfo=None),
                        snapshot_count=1,
                        trade_count=0,
                        payload_json={},
                        storage_type="parquet",
                        storage_uri=contaminating_file.parent.resolve().as_uri(),
                    ),
                ]
            )
            await session.commit()

        coverage = await resolve_coverage(
            token_ids=["YES"],
            start=start,
            end=end,
            dataset_ids=["selected"],
            ensure_scan=False,
        )

        assert coverage.files_for("YES") == (str(selected_file),)
        assert coverage.by_token["YES"].dataset_ids == ("selected",)
        assert str(contaminating_file) not in coverage.all_files()
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_selected_dataset_lineage_is_exact_per_file_and_union_for_shared_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "evosport_file_lineage")
    try:
        import models.database as database

        monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)
        start = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
        close = start + timedelta(hours=1)
        distinct_a = tmp_path / "distinct-a"
        distinct_b = tmp_path / "distinct-b"
        shared = tmp_path / "shared"
        distinct_files = {
            _write_dataset_file(root, token, start, bid)
            for root, bid in ((distinct_a, 0.40), (distinct_b, 0.42))
            for token in ("YES", "NO")
        }
        shared_files = {
            _write_dataset_file(shared, token, start, 0.40)
            for token in ("YES", "NO")
        }
        async with session_factory() as session:
            session.add_all(
                [
                    _lineage_dataset(dataset_id="distinct-a", root=distinct_a, start=start, close=close),
                    _lineage_dataset(dataset_id="distinct-b", root=distinct_b, start=start, close=close),
                    _lineage_dataset(dataset_id="shared-a", root=shared, start=start, close=close),
                    _lineage_dataset(dataset_id="shared-b", root=shared, start=start, close=close),
                ]
            )
            await session.commit()

        distinct_coverage = await resolve_coverage(
            token_ids=["YES", "NO"],
            start=start,
            end=close,
            dataset_ids=["distinct-a", "distinct-b"],
            ensure_scan=False,
        )
        assert distinct_coverage.dataset_ids_by_path == {
            str(path): (("distinct-a",) if path.parent == distinct_a else ("distinct-b",))
            for path in sorted(distinct_files)
        }
        distinct_manifest = await freeze_provider_datasets(
            provider_dataset_ids=["distinct-a", "distinct-b"],
            output_root=tmp_path / "distinct-manifests",
            start=start,
            end=close,
            football=_lineage_binding(start, close),
        )
        assert {item.path: item.provider_dataset_ids for item in distinct_manifest.files} == {
            path: distinct_coverage.dataset_ids_by_path[path]
            for path in sorted(distinct_coverage.dataset_ids_by_path)
        }
        distinct_snapshot = MarketDataView(
            coverage=distinct_coverage,
            start=start,
            end=close,
        ).dataset_snapshot()
        assert {
            item.path: item.provider_dataset_ids for item in distinct_snapshot.entries
        } == distinct_coverage.dataset_ids_by_path

        shared_coverage = await resolve_coverage(
            token_ids=["YES", "NO"],
            start=start,
            end=close,
            dataset_ids=["shared-a", "shared-b"],
            ensure_scan=False,
        )
        assert shared_coverage.dataset_ids_by_path == {
            str(path): ("shared-a", "shared-b") for path in sorted(shared_files)
        }
        shared_manifest = await freeze_provider_datasets(
            provider_dataset_ids=["shared-a", "shared-b"],
            output_root=tmp_path / "shared-manifests",
            start=start,
            end=close,
            football=_lineage_binding(start, close),
        )
        assert {item.path: item.provider_dataset_ids for item in shared_manifest.files} == {
            path: ("shared-a", "shared-b") for path in sorted(shared_coverage.dataset_ids_by_path)
        }
        shared_snapshot = MarketDataView(
            coverage=shared_coverage,
            start=start,
            end=close,
        ).dataset_snapshot()
        assert {
            item.path: item.provider_dataset_ids for item in shared_snapshot.entries
        } == shared_coverage.dataset_ids_by_path
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_explicit_dataset_selection_rejects_unknown_catalog_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "evosport_unknown_dataset")
    try:
        import models.database as database

        monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)

        with pytest.raises(ValueError, match="unknown provider dataset IDs:.*missing"):
            await resolve_coverage(
                token_ids=["YES"],
                start=start,
                end=start + timedelta(hours=1),
                dataset_ids=["missing"],
                ensure_scan=False,
            )
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_explicit_empty_dataset_selection_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "evosport_empty_dataset")
    try:
        import models.database as database

        monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)

        with pytest.raises(ValueError, match="dataset_ids cannot be empty"):
            await resolve_coverage(
                token_ids=["YES"],
                start=start,
                end=start + timedelta(hours=1),
                dataset_ids=[],
                ensure_scan=False,
            )
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_explicit_dataset_selection_rejects_uncovered_requested_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "evosport_uncovered_window")
    try:
        import models.database as database

        monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        file_path = _write_dataset_file(tmp_path / "short", "YES", start, 0.40)
        async with session_factory() as session:
            session.add(
                ProviderDataset(
                    id="short",
                    provider="evosport-test",
                    external_id="short",
                    asset_class="prediction",
                    token_ids_json=["YES"],
                    start_ts=start.replace(tzinfo=None),
                    end_ts=(start + timedelta(minutes=30)).replace(tzinfo=None),
                    snapshot_count=1,
                    trade_count=0,
                    payload_json={},
                    storage_type="parquet",
                    storage_uri=file_path.parent.resolve().as_uri(),
                )
            )
            await session.commit()

        with pytest.raises(ValueError, match="selected datasets do not fully cover.*YES"):
            await resolve_coverage(
                token_ids=["YES"],
                start=start,
                end=start + timedelta(hours=1),
                dataset_ids=["short"],
                ensure_scan=False,
            )
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_explicit_dataset_selection_rejects_tokens_absent_from_catalog_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "evosport_extra_token")
    try:
        import models.database as database

        monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        file_path = _write_dataset_file(tmp_path / "mixed", "YES", start, 0.40)
        async with session_factory() as session:
            session.add(
                ProviderDataset(
                    id="mixed",
                    provider="evosport-test",
                    external_id="mixed",
                    asset_class="prediction",
                    token_ids_json=["YES", "UNREQUESTED"],
                    start_ts=start.replace(tzinfo=None),
                    end_ts=end.replace(tzinfo=None),
                    snapshot_count=1,
                    trade_count=0,
                    payload_json={},
                    storage_type="parquet",
                    storage_uri=file_path.parent.resolve().as_uri(),
                )
            )
            await session.commit()

        with pytest.raises(ValueError, match="selected dataset token mismatch"):
            await resolve_coverage(
                token_ids=["YES", "MISSING"],
                start=start,
                end=end,
                dataset_ids=["mixed"],
                ensure_scan=False,
            )
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_freeze_pins_selected_canonical_files_and_football_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "evosport_catalog_freeze")
    try:
        import models.database as database

        monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(database, "BacktestAsyncSessionLocal", session_factory)
        start = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
        close = start + timedelta(hours=1)
        window = tmp_path / "canonical"
        yes_file = _write_dataset_file(window, "YES", start, 0.40)
        no_file = _write_dataset_file(window, "NO", start, 0.58)
        payload = {
            "canonical": True,
            "schema_version": "snapshots_v2",
            "sport": "football",
            "event_id": "event-1",
            "condition_id": "market-1",
            "clob_token_up": "YES",
            "clob_token_down": "NO",
            "market_type": "total_goals_over_under",
            "threshold": "2.5",
            "market_start_time": start.isoformat(),
            "market_end_time": close.isoformat(),
        }
        async with session_factory() as session:
            session.add(
                ProviderDataset(
                    id="football-selected",
                    provider="evosport-test",
                    external_id="football-selected",
                    external_slug="home-away-over-2-5",
                    title="Home v Away O/U 2.5",
                    asset_class="prediction",
                    token_ids_json=["YES", "NO"],
                    start_ts=start.replace(tzinfo=None),
                    end_ts=close.replace(tzinfo=None),
                    snapshot_count=2,
                    trade_count=0,
                    payload_json=payload,
                    storage_type="parquet",
                    storage_uri=window.resolve().as_uri(),
                )
            )
            await session.commit()

        binding = FootballDatasetBinding(
            event=CanonicalSportsEvent(
                event_id="event-1",
                competition="fixture-league",
                season="2026",
                home_team="Home",
                away_team="Away",
                scheduled_start=close,
                actual_start=close,
                status=EventStatus.FINISHED,
            ),
            contract=CanonicalSportsContract(
                contract_id="contract-1",
                event_id="event-1",
                venue="evosport-test",
                venue_market_id="market-1",
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

        with pytest.raises(ValueError, match="unknown provider dataset IDs"):
            await freeze_provider_datasets(
                provider_dataset_ids=["missing-dataset"],
                output_root=tmp_path / "datasets",
                start=start,
                end=close,
                football=binding,
            )

        manifest = await freeze_provider_datasets(
            provider_dataset_ids=["football-selected"],
            output_root=tmp_path / "datasets",
            start=start,
            end=close,
            football=binding,
        )
        repeated = await freeze_provider_datasets(
            provider_dataset_ids=["football-selected"],
            output_root=tmp_path / "datasets",
            start=start,
            end=close,
            football=binding,
        )

        assert repeated == manifest
        assert manifest.provider_dataset_ids == ("football-selected",)
        assert manifest.token_ids == ("NO", "YES")
        assert {item.path for item in manifest.files} == {
            str(no_file.resolve()),
            str(yes_file.resolve()),
        }
        assert {item.sha256 for item in manifest.files} == {
            sha256_file(no_file),
            sha256_file(yes_file),
        }
        snapshot = tmp_path / "datasets" / manifest.manifest_id
        assert {entry.name for entry in snapshot.iterdir()} == {"manifest.json"}

        stored = await store_football_settlement(binding)
        records = await get_settlement_records(["market-1"])
        assert stored.winning_token_id == "YES"
        assert records["market-1"] == stored

        identity = await football_binding.load_current_football_identity(
            binding,
            ("football-selected",),
        )
        assert identity == {
            "provider_dataset_ids": ["football-selected"],
            "provider_datasets": [
                {
                    "provider_dataset_id": "football-selected",
                    "provider": "evosport-test",
                    "canonical": True,
                    "schema_version": "snapshots_v2",
                    "sport": "football",
                    "event_id": "event-1",
                    "condition_id": "market-1",
                    "yes_token_id": "YES",
                    "no_token_id": "NO",
                    "market_type": "total_goals_over_under",
                    "threshold": "2.5",
                    "token_ids": ["NO", "YES"],
                    "storage_type": "parquet",
                    "row_start": start.isoformat(),
                    "row_end": close.isoformat(),
                    "market_start": start.isoformat(),
                    "market_end": close.isoformat(),
                }
            ],
            "settlement": {
                "condition_id": "market-1",
                "slug": None,
                "winning_token_id": "YES",
                "winning_outcome": "YES",
                "token_ids": ["NO", "YES"],
                "resolution_time": (close + timedelta(hours=2, minutes=1)).isoformat(),
                "coin_price_start": None,
                "coin_price_end": None,
                "resolved": True,
                "source": "evosport:football:fixture-v1",
            },
        }

        async with session_factory() as session:
            dataset = await session.get(ProviderDataset, "football-selected")
            assert dataset is not None
            dataset.payload_json = {**dataset.payload_json, "condition_id": "mutated-market"}
            await session.commit()
        with pytest.raises(ValueError, match="condition_id"):
            await football_binding.load_current_football_identity(
                binding,
                ("football-selected",),
            )

        async with session_factory() as session:
            dataset = await session.get(ProviderDataset, "football-selected")
            assert dataset is not None
            dataset.payload_json = payload
            await session.commit()
        await football_binding.upsert_settlement(
            football_binding.expected_football_settlement(binding).__class__(
                **{
                    **football_binding.expected_football_settlement(binding).__dict__,
                    "winning_token_id": "NO",
                    "winning_outcome": "NO",
                }
            )
        )
        with pytest.raises(ValueError, match="settlement"):
            await football_binding.load_current_football_identity(
                binding,
                ("football-selected",),
            )

        _write_dataset_file(window, "EXTRA", start, 0.20)
        with pytest.raises(ValueError, match="extra parquet files"):
            await freeze_provider_datasets(
                provider_dataset_ids=["football-selected"],
                output_root=tmp_path / "datasets",
                start=start,
                end=close,
                football=binding,
            )
    finally:
        await engine.dispose()


@pytest.mark.db
@pytest.mark.asyncio
async def test_real_gateway_consumes_only_selected_bytes_and_stored_football_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await build_postgres_session_factory(Base, "evosport_real_gateway")
    try:
        import models.database as database
        import services.external_data.parquet_scanner as parquet_scanner
        import services.external_data.provider_import_service as provider_import_service
        import services.fill_simulator as fill_simulator
        import services.strategy_backtester as strategy_backtester
        import services.trader_orchestrator.decision_gates as decision_gates
        import evosport.domain.time as time_boundary

        class Python310Datetime:
            @staticmethod
            def fromisoformat(value: str) -> datetime:
                assert not value.endswith("Z")
                return datetime.fromisoformat(value)

        monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(database, "BacktestAsyncSessionLocal", session_factory)
        monkeypatch.setattr(provider_import_service, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(strategy_backtester, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(time_boundary, "datetime", Python310Datetime)
        fill_model_loaded = False

        async def forbidden_fill_model(*args: object, **kwargs: object) -> None:
            nonlocal fill_model_loaded
            fill_model_loaded = True
            raise AssertionError("active fill model must not be loaded")

        monkeypatch.setattr(fill_simulator, "load_active_fill_model", forbidden_fill_model)

        start = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
        kickoff = start + timedelta(hours=1)
        result_observed = kickoff + timedelta(hours=2)
        selected_root = tmp_path / "selected"
        contaminating_root = tmp_path / "contaminating"
        selected_yes = _write_dataset_series(
            selected_root,
            "YES",
            [(start, 0.40), (start + timedelta(seconds=1), 0.40), (result_observed, 0.40)],
        )
        selected_no = _write_dataset_series(
            selected_root,
            "NO",
            [(start, 0.56), (start + timedelta(seconds=1), 0.56), (result_observed, 0.56)],
        )
        contaminating_yes = _write_dataset_series(
            contaminating_root,
            "YES",
            [(start, 0.80), (start + timedelta(seconds=1), 0.80), (result_observed, 0.80)],
        )
        _write_dataset_series(
            contaminating_root,
            "NO",
            [(start, 0.18), (start + timedelta(seconds=1), 0.18), (result_observed, 0.18)],
        )
        payload = {
            "canonical": True,
            "schema_version": "snapshots_v2",
            "sport": "football",
            "event_id": "event-real",
            "condition_id": "market-real",
            "clob_token_up": "YES",
            "clob_token_down": "NO",
            "market_type": "total_goals_over_under",
            "threshold": "2.5",
            "market_start_time": start.isoformat().replace("+00:00", "Z"),
            "market_end_time": kickoff.isoformat().replace("+00:00", "Z"),
        }
        async with session_factory() as session:
            session.add_all(
                [
                    ProviderDataset(
                        id="selected-real",
                        provider="evosport-test",
                        external_id="selected-real",
                        external_slug="home-away-over-2-5",
                        title="Home v Away O/U 2.5",
                        asset_class="prediction",
                        token_ids_json=["YES", "NO"],
                        start_ts=start.replace(tzinfo=None),
                        end_ts=result_observed.replace(tzinfo=None),
                        snapshot_count=6,
                        trade_count=0,
                        payload_json=payload,
                        storage_type="parquet",
                        storage_uri=selected_root.resolve().as_uri(),
                    ),
                    ProviderDataset(
                        id="contaminating-real",
                        provider="evosport-test",
                        external_id="contaminating-real",
                        external_slug="home-away-over-2-5-contaminating",
                        title="Conflicting prices",
                        asset_class="prediction",
                        token_ids_json=["YES", "NO"],
                        start_ts=start.replace(tzinfo=None),
                        end_ts=result_observed.replace(tzinfo=None),
                        snapshot_count=6,
                        trade_count=0,
                        payload_json=payload,
                        storage_type="parquet",
                        storage_uri=contaminating_root.resolve().as_uri(),
                    ),
                ]
            )
            await session.commit()

        binding = FootballDatasetBinding(
            event=CanonicalSportsEvent(
                event_id="event-real",
                competition="fixture-league",
                season="2026",
                home_team="Home",
                away_team="Away",
                scheduled_start=kickoff,
                actual_start=kickoff,
                status=EventStatus.FINISHED,
            ),
            contract=CanonicalSportsContract(
                contract_id="contract-real",
                event_id="event-real",
                venue="evosport-test",
                venue_market_id="market-real",
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
                event_time=result_observed - timedelta(minutes=1),
                observed_at=result_observed,
                ingested_at=result_observed + timedelta(minutes=1),
            ),
        )
        manifest = await freeze_provider_datasets(
            provider_dataset_ids=["selected-real"],
            output_root=tmp_path / "manifests",
            start=start,
            end=result_observed,
            football=binding,
        )
        await store_football_settlement(binding)
        ambient_scan_called = False

        async def forbidden_ambient_scan(*args: object, **kwargs: object) -> None:
            nonlocal ambient_scan_called
            ambient_scan_called = True
            raise AssertionError("reproducible execution must not scan ambient parquet")

        monkeypatch.setattr(parquet_scanner, "ensure_recent_scan", forbidden_ambient_scan)

        source_code = """
from services.strategies.base import BaseStrategy, ExitDecision, StrategyDecision

class EvoSportSelectedDatasetStrategy(BaseStrategy):
    strategy_type = "evosport_selected_dataset"
    name = "EvoSport selected dataset integration"
    description = "Buys football over only when selected canonical ask is below 0.5."
    subscriptions = ["market_data_refresh"]

    def detect(self, events, markets, prices):
        book = prices.get("YES") or {}
        ask = book.get("best_ask") or book.get("ask")
        market = markets[0] if markets else None
        market_identity = (
            str(getattr(market, "id", "")),
            str(getattr(market, "slug", "")),
            str(getattr(market, "question", "")),
        )
        if (
            ask is None
            or float(ask) >= 0.5
            or int(book.get("ts") or 0) >= MARKET_CLOSE_EPOCH
            or not markets
            or market_identity not in (
                ("selected-real", "home-away-over-2-5", "Home v Away O/U 2.5"),
                ("market-real", "contract-real", "Home v Away: Over 2.5 total goals"),
            )
        ):
            return []
        opportunity = self.create_opportunity(
            title="selected over 2.5",
            description="selected canonical football ask",
            total_cost=float(ask),
            markets=[markets[0]],
            positions=[{
                "action": "BUY",
                "outcome": "YES",
                "token_id": "YES",
                "price": float(ask),
                "notional_usd": float(self.config.get("notional_usd", 20.0)),
                "time_in_force": "IOC",
            }],
            expected_payout=1.0,
            is_guaranteed=False,
            skip_fee_model=True,
            custom_roi_percent=100.0,
            custom_risk_score=0.0,
        )
        return [opportunity] if opportunity is not None else []

    def evaluate(self, signal, context):
        live_market = context.get("live_market") or {}
        if not live_market.get("available"):
            raise RuntimeError("evaluation did not receive selected market data")
        if float(live_market.get("live_selected_price") or 1.0) >= 0.5:
            raise RuntimeError("evaluation consumed non-selected market data")
        return StrategyDecision(
            "selected",
            "selected market data verified",
            size_usd=float(self.config.get("notional_usd", 20.0)),
        )

    def on_fill(self, *args, **kwargs):
        if self.config.get("fail_hook") == "on_fill":
            raise RuntimeError("on_fill failed")

    def on_partial_fill(self, *args, **kwargs):
        if self.config.get("fail_hook") == "on_partial_fill":
            raise RuntimeError("on_partial_fill failed")

    def on_cancel(self, *args, **kwargs):
        if self.config.get("fail_hook") == "on_cancel":
            raise RuntimeError("on_cancel failed")

    def should_exit(self, position, market_state):
        fail_hook = self.config.get("fail_hook")
        if fail_hook == "should_exit":
            raise RuntimeError("should_exit failed")
        if fail_hook == "resolve_exit_policy":
            return ExitDecision(
                "close",
                "policy_failure",
                close_price=market_state["current_price"],
            )
        return ExitDecision("hold", "fixture hold")

    def resolve_exit_policy(self, decision, close_trigger):
        if self.config.get("fail_hook") == "resolve_exit_policy":
            raise RuntimeError("resolve_exit_policy failed")
        return None
""".replace("MARKET_CLOSE_EPOCH", str(int(kickoff.timestamp())))
        source_path = tmp_path / "strategy.py"
        source_path.write_text(source_code, encoding="utf-8")
        lock_path = tmp_path / "environment.lock"
        lock_path.write_text("\n".join(active_distribution_pins()) + "\n", encoding="utf-8")
        manifest_path = tmp_path / "manifests" / manifest.manifest_id / "manifest.json"
        spec_path = tmp_path / "experiment.yaml"
        spec_path.write_text(
            yaml.safe_dump(
                {
                    "name": "real-selected-football",
                    "family_id": "football-over25",
                    "dataset_manifest_path": str(manifest_path),
                    "strategy": {
                        "slug": "evosport_selected_dataset",
                        "source_path": str(source_path),
                        "dependency_lock_path": str(lock_path),
                        "config": {"max_gross_exposure_usd": 500.0},
                    },
                    "window": {
                        "train_start": "2026-01-01T00:00:00Z",
                        "train_end": start.isoformat(),
                        "validation_start": manifest.start.isoformat(),
                        "validation_end": manifest.end.isoformat(),
                    },
                    "execution": {
                        "initial_capital_usd": 1000.0,
                        "submit_p50_ms": 1.0,
                        "submit_p95_ms": 2.0,
                        "cancel_p50_ms": 1.0,
                        "cancel_p95_ms": 2.0,
                    },
                    "split_method": "time",
                    "max_trials": 1,
                    "seed": 7,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        registry = SqlRunRegistry(session_factory)
        runner = ExperimentRunner(
            registry=registry,
            gateway=HomerunBacktestGateway(),
            artifact_root=tmp_path / "artifacts",
            homerun_commit="real-integration-fixture",
            evaluator_version="not-evaluated-v1",
        )

        async with session_factory() as session:
            selected_row = await session.get(ProviderDataset, "selected-real")
            assert selected_row is not None
            selected_row.payload_json = {**payload, "condition_id": "mutated-market"}
            await session.commit()
        with pytest.raises(ValueError, match="football.*condition_id|condition_id.*football"):
            await runner.run(spec_path)
        assert not (tmp_path / "artifacts").exists() or not any((tmp_path / "artifacts").iterdir())

        async with session_factory() as session:
            selected_row = await session.get(ProviderDataset, "selected-real")
            assert selected_row is not None
            selected_row.payload_json = payload
            await session.commit()
        expected_record = football_binding.expected_football_settlement(binding)
        await football_binding.upsert_settlement(
            replace(expected_record, winning_token_id="NO", winning_outcome="NO")
        )
        with pytest.raises(ValueError, match="football settlement"):
            await runner.run(spec_path)
        assert not (tmp_path / "artifacts").exists() or not any((tmp_path / "artifacts").iterdir())

        await football_binding.upsert_settlement(expected_record)

        original_decision_gates = decision_gates.apply_platform_decision_gates

        def failed_decision_gates(**kwargs: object) -> dict[str, object]:
            raise RuntimeError("decision gates failed")

        monkeypatch.setattr(
            decision_gates,
            "apply_platform_decision_gates",
            failed_decision_gates,
        )
        with pytest.raises(RuntimeError, match="decision gates failed"):
            await runner.run(spec_path)
        assert not (tmp_path / "artifacts").exists() or not any((tmp_path / "artifacts").iterdir())
        monkeypatch.setattr(
            decision_gates,
            "apply_platform_decision_gates",
            original_decision_gates,
        )

        baseline_spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        for hook in (
            "on_fill",
            "on_partial_fill",
            "on_cancel",
            "should_exit",
            "resolve_exit_policy",
        ):
            lifecycle_spec = {
                **baseline_spec,
                "strategy": {
                    **baseline_spec["strategy"],
                    "config": {
                        "max_gross_exposure_usd": 500.0,
                        "notional_usd": (
                            80.0 if hook in {"on_partial_fill", "on_cancel"} else 20.0
                        ),
                        "fail_hook": hook,
                    },
                },
            }
            spec_path.write_text(
                yaml.safe_dump(lifecycle_spec, sort_keys=True),
                encoding="utf-8",
            )
            with pytest.raises(RuntimeError, match=f"{hook} failed"):
                await runner.run(spec_path)
            assert not (tmp_path / "artifacts").exists() or not any(
                (tmp_path / "artifacts").iterdir()
            )
            async with session_factory() as session:
                assert (
                    await session.execute(select(EvidencePublication))
                ).scalars().all() == []
        spec_path.write_text(
            yaml.safe_dump(baseline_spec, sort_keys=True),
            encoding="utf-8",
        )

        async with session_factory() as session:
            selected_row = await session.get(ProviderDataset, "selected-real")
            assert selected_row is not None
            selected_row.external_id = "mutated-external-id"
            selected_row.external_slug = "mutated-slug"
            selected_row.title = "Mutated title"
            selected_row.coin = "BTC"
            selected_row.payload_json = {
                **payload,
                "market_id": "mutated-payload-id",
                "slug": "mutated-payload-slug",
                "title": "Mutated payload title",
                "coin": "ETH",
                "coin_price_start": 1234.5,
            }
            await session.commit()

        alternate_root = tmp_path / "alternate-selected"
        alternate_yes = _write_dataset_series(
            alternate_root,
            "YES",
            [(start, 0.80), (start + timedelta(seconds=1), 0.80), (result_observed, 0.80)],
        )
        original_yes_backup = tmp_path / "selected-yes-original.parquet"
        selected_source_view_ids: list[int] = []
        selected_source_paths: list[tuple[str, ...]] = []
        original_source_init = MarketDataViewSource.__init__
        original_evaluate = strategy_backtester._backtest_evaluate_opportunity
        original_dataset_snapshot = MarketDataView.dataset_snapshot
        canonical_swapped = False
        canonical_restored = False

        def track_source(self: object, view: MarketDataView, **kwargs: object) -> None:
            selected_source_view_ids.append(id(view))
            selected_source_paths.append(view.coverage().all_files())
            original_source_init(self, view, **kwargs)

        def swap_after_discovery(*args: object, **kwargs: object) -> object:
            nonlocal canonical_swapped
            if not canonical_swapped:
                os.replace(selected_yes, original_yes_backup)
                os.replace(alternate_yes, selected_yes)
                canonical_swapped = True
            return original_evaluate(*args, **kwargs)

        def restore_before_engine_evidence(self: MarketDataView):
            nonlocal canonical_restored
            if canonical_swapped and not canonical_restored:
                os.replace(selected_yes, alternate_yes)
                os.replace(original_yes_backup, selected_yes)
                canonical_restored = True
            return original_dataset_snapshot(self)

        monkeypatch.setattr(MarketDataViewSource, "__init__", track_source)
        monkeypatch.setattr(
            strategy_backtester,
            "_backtest_evaluate_opportunity",
            swap_after_discovery,
        )
        monkeypatch.setattr(MarketDataView, "dataset_snapshot", restore_before_engine_evidence)

        outcome = await runner.run(spec_path)
        response = outcome.result

        assert not response["execution"]["validation_errors"], response["execution"]
        assert response["execution"]["runtime_error"] is None, response["execution"]
        effective = response["effective_dataset"]
        assert effective, response
        assert effective["provider_dataset_ids"] == ["selected-real"]
        assert {entry["path"] for entry in effective["entries"]} == {
            str(selected_yes.resolve()),
            str(selected_no.resolve()),
        }
        assert {entry["sha256"] for entry in effective["entries"]} == {
            sha256_file(selected_yes),
            sha256_file(selected_no),
        }
        assert str(contaminating_yes.resolve()) not in {
            entry["path"] for entry in effective["entries"]
        }
        execution = response["execution"]
        assert response["effective_football"] == {
            "provider_dataset_ids": ["selected-real"],
            "markets": [
                {
                    "market_id": "market-real",
                    "condition_id": "market-real",
                    "slug": "contract-real",
                    "title": "Home v Away: Over 2.5 total goals",
                    "coin": None,
                    "timeframe": "total_goals_over_under",
                    "yes_token_id": "YES",
                    "no_token_id": "NO",
                    "market_start": start.isoformat(),
                    "market_close": kickoff.isoformat(),
                    "price_to_beat": None,
                }
            ],
            "token_settlements": [
                {
                    "token_id": "NO",
                    "condition_id": "market-real",
                    "settlement_price": 0.0,
                    "winning_outcome": "YES",
                    "resolution_time": result_observed.isoformat(),
                    "source": "evosport:football:fixture-v1",
                },
                {
                    "token_id": "YES",
                    "condition_id": "market-real",
                    "settlement_price": 1.0,
                    "winning_outcome": "YES",
                    "resolution_time": result_observed.isoformat(),
                    "source": "evosport:football:fixture-v1",
                },
            ],
        }
        assert fill_model_loaded is False
        assert ambient_scan_called is False
        assert canonical_swapped is True and canonical_restored is True
        assert len(selected_source_view_ids) >= 2
        assert len(set(selected_source_view_ids)) == 1
        assert all(
            set(paths).isdisjoint({str(selected_yes.resolve()), str(selected_no.resolve())})
            for paths in selected_source_paths
        )
        assert execution["n_intents"] >= 1
        assert execution["total_fills"] >= 1
        assert execution["settlement_summary"]["settled_positions"] >= 1
        assert execution["final_equity_usd"] == pytest.approx(1027.310283337984, abs=1e-9)
        assert execution["total_return_pct"] == pytest.approx(2.7310283337984, abs=1e-9)
        assert outcome.run_id == outcome.homerun_run_id == response["run_id"]
        assert outcome.decision == "NOT_EVALUATED"
        assert json.loads((outcome.artifact_dir / "result.json").read_text(encoding="utf-8"))[
            "effective_dataset"
        ] == effective

        async with session_factory() as session:
            selected_row = await session.get(ProviderDataset, "selected-real")
            assert selected_row is not None
            selected_row.end_ts = kickoff.replace(tzinfo=None)
            await session.commit()
        with pytest.raises(ValueError, match="fully cover requested window"):
            await runner.run(spec_path)
        async with session_factory() as session:
            selected_row = await session.get(ProviderDataset, "selected-real")
            assert selected_row is not None
            selected_row.end_ts = result_observed.replace(tzinfo=None)
            await session.commit()

        replacement_root = tmp_path / "replacement-selected"
        _write_dataset_series(
            replacement_root,
            "YES",
            [(start, 0.40), (start + timedelta(seconds=1), 0.40), (result_observed, 0.40)],
        )
        _write_dataset_series(
            replacement_root,
            "NO",
            [(start, 0.56), (start + timedelta(seconds=1), 0.56), (result_observed, 0.56)],
        )
        async with session_factory() as session:
            selected_row = await session.get(ProviderDataset, "selected-real")
            assert selected_row is not None
            selected_row.storage_uri = replacement_root.resolve().as_uri()
            await session.commit()
        with pytest.raises(ValueError, match="coverage paths"):
            await runner.run(spec_path)
        async with session_factory() as session:
            selected_row = await session.get(ProviderDataset, "selected-real")
            assert selected_row is not None
            selected_row.storage_uri = selected_root.resolve().as_uri()
            await session.commit()

        await football_binding.upsert_settlement(
            replace(expected_record, winning_token_id="NO", winning_outcome="NO")
        )
        with pytest.raises(ValueError, match="football settlement"):
            await runner.run(spec_path)
        selected_yes.write_bytes(b"tampered-selected-canonical-bytes")
        with pytest.raises(RuntimeError, match="snapshot integrity"):
            await runner.run(spec_path)
    finally:
        await engine.dispose()
