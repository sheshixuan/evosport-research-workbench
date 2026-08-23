from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from services.external_data.parquet_schema import SNAPSHOT_SCHEMA
from services.marketdata.coverage import CoverageMap, TokenCoverage
from services.marketdata.projection import ProjectedMarket
from services.marketdata.view import MarketDataView
from services.strategy_backtester import ExecutionBacktestResult
from services.data_events import DataEvent, EventType
from services.strategies.base import BaseStrategy


_PROJECTED_MARKET = {
    "market_id": "market",
    "condition_id": "market",
    "slug": "contract",
    "title": "Home v Away: Over 2.5 total goals",
    "coin": None,
    "timeframe": "total_goals_over_under",
    "market_start": "2026-08-01T00:00:00+00:00",
    "market_close": "2026-08-02T00:00:00+00:00",
    "yes_token_id": "YES",
    "no_token_id": "NO",
    "price_to_beat": None,
}


async def _replay_with_one_event(
    monkeypatch: pytest.MonkeyPatch,
    strategy: BaseStrategy,
    *,
    event_type: str,
    fail_closed: bool,
) -> list[object]:
    import services.strategy_backtester as strategy_backtester

    async def replay_bus(*, ticks: list[datetime], events_by_tick: list[list[object]], **kwargs: object) -> int:
        market = {
            "id": "market",
            "condition_id": "market",
            "question": "Home v Away over 2.5",
            "slug": "contract",
            "clobTokenIds": ["YES", "NO"],
            "clob_token_ids": ["YES", "NO"],
            "bestBid": 0.40,
            "bestAsk": 0.42,
            "active": True,
            "closed": False,
        }
        events_by_tick[0].append(
            DataEvent(
                event_type=event_type,
                source="selected-fixture",
                timestamp=ticks[0],
                payload={"markets": [market]},
                markets=[market],
                events=[{"id": "event"}],
                prices={"YES": {"best_bid": 0.40, "best_ask": 0.42}},
            )
        )
        return 1

    async def empty_grid(**kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(strategy_backtester, "_replay_bus_events_into_tick_grid", replay_bus)
    monkeypatch.setattr(strategy_backtester, "_build_per_tick_prices_grid", empty_grid)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return await strategy_backtester._replay_discover_opportunities(
        strategy=strategy,
        slug=strategy.strategy_type,
        start_dt=start,
        end_dt=datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc),
        sample_interval_seconds=60.0,
        max_ticks=1,
        provider_dataset_ids=("selected",),
        fail_closed=fail_closed,
        ensure_scan=False,
    )


class _ScannerNoopStrategy(BaseStrategy):
    strategy_type = "selected_scanner_noop"
    name = "Selected scanner noop"
    description = "Selected scanner hydration fixture"
    subscriptions = [EventType.MARKET_DATA_REFRESH]

    def detect(self, events: list[object], markets: list[object], prices: dict[str, object]) -> list[object]:
        return []


class _DeterministicEmissionStrategy(_ScannerNoopStrategy):
    strategy_type = "selected_deterministic_emission"

    def detect(self, events: list[object], markets: list[object], prices: dict[str, object]) -> list[object]:
        return [
            SimpleNamespace(
                title="selected deterministic opportunity",
                event_id="event",
                positions_to_take=[
                    {
                        "action": "BUY",
                        "outcome": "YES",
                        "token_id": "YES",
                        "price": 0.42,
                        "notional_usd": 20.0,
                    }
                ],
                expected_roi=3.5,
                risk_score=0.1,
            )
        ]


class _ScannerProjectionPriceStrategy(BaseStrategy):
    strategy_type = "selected_scanner_projection_price"
    name = "Selected scanner projection price"
    description = "Records the projected selected ask."
    subscriptions = [EventType.MARKET_DATA_REFRESH]

    def __init__(self) -> None:
        super().__init__()
        self.observed_prices: list[float] = []

    def detect(self, events: list[object], markets: list[object], prices: dict[str, object]) -> list[object]:
        self.observed_prices.append(float(prices["YES"]["best_ask"]))
        return []


class _CryptoProjectionPriceStrategy(BaseStrategy):
    strategy_type = "selected_crypto_projection_price"
    name = "Selected crypto projection price"
    description = "Records the projected selected midpoint."
    subscriptions = [EventType.CRYPTO_UPDATE]

    def __init__(self) -> None:
        super().__init__()
        self.observed_prices: list[float] = []

    def detect(self, events: list[object], markets: list[object], prices: dict[str, object]) -> list[object]:
        self.observed_prices.append(float(events[0].payload["markets"][0]["up_price"]))
        return []


def _write_projection_book(
    path: Path,
    *,
    start: datetime,
    bid: float,
    rows: int = 1,
    token_ids: list[str] | None = None,
) -> None:
    observed = [int((start + timedelta(seconds=index)).timestamp() * 1_000_000) for index in range(rows)]
    selected_token_ids = token_ids or ["YES"] * rows
    assert len(selected_token_ids) == rows
    table = pa.table(
        {
            "token_id": pa.array(selected_token_ids, pa.string()),
            "observed_at_us": pa.array(observed, pa.int64()),
            "sequence": pa.array(list(range(rows)), pa.int64()),
            "best_bid": pa.array([bid] * rows, pa.float64()),
            "best_ask": pa.array([bid + 0.02] * rows, pa.float64()),
            "spread_bps": pa.array([None] * rows, pa.float64()),
            "bids_price": pa.array([[bid]] * rows, pa.list_(pa.float64())),
            "bids_size": pa.array([[10.0]] * rows, pa.list_(pa.float64())),
            "asks_price": pa.array([[bid + 0.02]] * rows, pa.list_(pa.float64())),
            "asks_size": pa.array([[10.0]] * rows, pa.list_(pa.float64())),
            "trade_price": pa.array([None] * rows, pa.float64()),
            "trade_size": pa.array([None] * rows, pa.float64()),
            "trade_side": pa.array([None] * rows, pa.string()),
        },
        schema=SNAPSHOT_SCHEMA,
    )
    pq.write_table(table, path)


def _projection_view(
    path: Path,
    *,
    start: datetime,
    end: datetime,
    integrity_checker: object = None,
) -> MarketDataView:
    start_us = int(start.timestamp() * 1_000_000)
    end_us = int(end.timestamp() * 1_000_000)
    return MarketDataView(
        coverage=CoverageMap(
            by_token={
                "YES": TokenCoverage(
                    "YES",
                    files=(str(path),),
                    dataset_ids=("selected",),
                    start_us=start_us,
                    end_us=end_us,
                )
            },
            requested=("YES",),
            window_start_us=start_us,
            window_end_us=end_us,
            dataset_ids_by_path={str(path): ("selected",)},
        ),
        start=start,
        end=end,
        integrity_checker=integrity_checker,
    )


@pytest.mark.asyncio
async def test_reproducible_discovery_ids_ignore_prior_process_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await _replay_with_one_event(
        monkeypatch,
        _DeterministicEmissionStrategy(),
        event_type=EventType.MARKET_DATA_REFRESH,
        fail_closed=True,
    )
    await _replay_with_one_event(
        monkeypatch,
        _DeterministicEmissionStrategy(),
        event_type=EventType.MARKET_DATA_REFRESH,
        fail_closed=True,
    )
    second = await _replay_with_one_event(
        monkeypatch,
        _DeterministicEmissionStrategy(),
        event_type=EventType.MARKET_DATA_REFRESH,
        fail_closed=True,
    )

    assert [(opp.id, opp.stable_id, opp.positions_data) for opp in first] == [
        (opp.id, opp.stable_id, opp.positions_data) for opp in second
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("topic", "projector_name", "strategy", "expected_price"),
    [
        (
            "polymarket.catalog.snapshot",
            "project_market_data_refresh_events",
            _ScannerProjectionPriceStrategy,
            0.42,
        ),
        (
            "crypto.update.dispatch",
            "project_crypto_update_events",
            _CryptoProjectionPriceStrategy,
            0.41,
        ),
    ],
)
async def test_selected_projection_uses_run_owned_view_after_canonical_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    topic: str,
    projector_name: str,
    strategy: type[BaseStrategy],
    expected_price: float,
) -> None:
    import services.marketdata.projection as projection
    import services.recorded_event_bus.backtest_bridge as backtest_bridge
    import services.strategy_backtester as strategy_backtester

    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    selected_path = tmp_path / "run-owned-selected.parquet"
    canonical_path = tmp_path / "canonical.parquet"
    alternate_path = tmp_path / "alternate.parquet"
    canonical_backup = tmp_path / "canonical-backup.parquet"
    _write_projection_book(selected_path, start=start, bid=0.40)
    _write_projection_book(canonical_path, start=start, bid=0.40)
    _write_projection_book(alternate_path, start=start, bid=0.80)
    selected_view = _projection_view(selected_path, start=start, end=end)
    start_us = int(start.timestamp() * 1_000_000)
    projected_market = ProjectedMarket(
        market_id="market",
        condition_id="market",
        slug="contract",
        title="Selected projected market",
        coin="btc",
        timeframe="5m",
        start_us=start_us,
        end_us=start_us + 120_000_000,
        up_token="YES",
        down_token=None,
        price_to_beat=None,
    )
    received_views: list[object] = []
    canonical_builds = 0
    swapped = False

    monkeypatch.setattr(backtest_bridge, "resolve_subscriptions_to_topics", lambda subscriptions: {topic})

    async def no_recorded_events(**kwargs: object):
        if False:
            yield None

    monkeypatch.setattr(backtest_bridge, "replay_events_for_strategy", no_recorded_events)

    async def load_projected_markets(**kwargs: object) -> list[ProjectedMarket]:
        return [projected_market]

    monkeypatch.setattr(projection, "load_projected_markets", load_projected_markets)

    async def build_canonical_view(cls: type[MarketDataView], **kwargs: object) -> MarketDataView:
        nonlocal canonical_builds
        canonical_builds += 1
        return _projection_view(canonical_path, start=start, end=end)

    monkeypatch.setattr(MarketDataView, "build", classmethod(build_canonical_view))
    real_projector = getattr(projection, projector_name)

    async def replace_canonical_then_project(**kwargs: object):
        nonlocal swapped
        received_views.append(kwargs.get("view"))
        if not swapped:
            os.replace(canonical_path, canonical_backup)
            os.replace(alternate_path, canonical_path)
            swapped = True
        return await real_projector(**kwargs)

    monkeypatch.setattr(projection, projector_name, replace_canonical_then_project)
    instance = strategy()

    await strategy_backtester._replay_discover_opportunities(
        strategy=instance,
        slug=instance.strategy_type,
        start_dt=start,
        end_dt=end,
        sample_interval_seconds=60.0,
        max_ticks=1,
        candidate_token_ids=["YES"],
        provider_dataset_ids=("selected",),
        fail_closed=True,
        ensure_scan=False,
        market_data_view=selected_view,
    )

    assert instance.observed_prices == [pytest.approx(expected_price)]
    assert received_views == [selected_view]
    assert canonical_builds == 0
    assert swapped is True


@pytest.mark.asyncio
async def test_selected_coverage_reads_materialized_view_without_resolving_canonical_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.marketdata.coverage as marketdata_coverage
    import services.strategy_backtester as strategy_backtester

    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    selected_path = tmp_path / "materialized-selected.parquet"
    canonical_path = tmp_path / "canonical.parquet"
    canonical_replacement_path = tmp_path / "canonical-replacement.parquet"
    _write_projection_book(
        selected_path,
        start=start,
        bid=0.40,
        rows=9,
        token_ids=["YES", "YES", "NO", "NO", "NO", "NO", "NO", "NO", "NO"],
    )
    _write_projection_book(canonical_path, start=start, bid=0.40, rows=2)
    _write_projection_book(canonical_replacement_path, start=start, bid=0.80, rows=5)
    integrity_checks: list[str] = []
    start_us = int(start.timestamp() * 1_000_000)
    end_us = int(end.timestamp() * 1_000_000)
    selected_view = MarketDataView(
        coverage=CoverageMap(
            by_token={
                "YES": TokenCoverage(
                    "YES",
                    files=(str(selected_path),),
                    dataset_ids=("selected",),
                    start_us=start_us,
                    end_us=end_us,
                ),
                "NO": TokenCoverage(
                    "NO",
                    files=(str(selected_path),),
                    dataset_ids=("selected",),
                    start_us=start_us,
                    end_us=end_us,
                ),
            },
            requested=("YES", "NO"),
            window_start_us=start_us,
            window_end_us=end_us,
            dataset_ids_by_path={str(selected_path): ("selected",)},
        ),
        start=start,
        end=end,
        integrity_checker=lambda: integrity_checks.append("checked"),
    )

    async def forbidden_resolve_coverage(**kwargs: object) -> CoverageMap:
        raise AssertionError("selected coverage must not resolve canonical paths")

    monkeypatch.setattr(marketdata_coverage, "resolve_coverage", forbidden_resolve_coverage)
    os.replace(canonical_replacement_path, canonical_path)

    coverage = await strategy_backtester._measure_data_coverage(
        session=object(),
        opp_tokens=["YES"],
        start_dt=start,
        end_dt=end,
        provider_dataset_ids=("selected",),
        ensure_scan=False,
        market_data_view=selected_view,
    )

    assert coverage["snapshots_total"] == 2
    assert coverage["snaps_per_token"] == {"YES": 2}
    assert integrity_checks == ["checked", "checked"]


@pytest.mark.asyncio
async def test_ordinary_coverage_without_injected_view_resolves_canonical_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.marketdata.coverage as marketdata_coverage
    import services.strategy_backtester as strategy_backtester

    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    canonical_path = tmp_path / "ordinary-canonical.parquet"
    _write_projection_book(canonical_path, start=start, bid=0.55, rows=3)
    resolved = False

    async def resolve_ordinary_coverage(**kwargs: object) -> CoverageMap:
        nonlocal resolved
        resolved = True
        return _projection_view(canonical_path, start=start, end=end).coverage()

    monkeypatch.setattr(marketdata_coverage, "resolve_coverage", resolve_ordinary_coverage)

    coverage = await strategy_backtester._measure_data_coverage(
        session=object(),
        opp_tokens=["YES"],
        start_dt=start,
        end_dt=end,
    )

    assert resolved is True
    assert coverage["snapshots_total"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("hydrator", ["hydrate_markets", "hydrate_events"])
async def test_reproducible_discovery_hydration_failures_raise(
    monkeypatch: pytest.MonkeyPatch,
    hydrator: str,
) -> None:
    import services.strategy_inputs as strategy_inputs

    def fail_hydration(values: list[object]) -> list[object]:
        raise RuntimeError(f"{hydrator} failed")

    monkeypatch.setattr(strategy_inputs, hydrator, fail_hydration)

    assert await _replay_with_one_event(
        monkeypatch,
        _ScannerNoopStrategy(),
        event_type=EventType.MARKET_DATA_REFRESH,
        fail_closed=False,
    ) == []
    with pytest.raises(RuntimeError, match=f"{hydrator} failed"):
        await _replay_with_one_event(
            monkeypatch,
            _ScannerNoopStrategy(),
            event_type=EventType.MARKET_DATA_REFRESH,
            fail_closed=True,
        )


class _DetectFailureStrategy(_ScannerNoopStrategy):
    strategy_type = "selected_detect_failure"

    def detect(self, events: list[object], markets: list[object], prices: dict[str, object]) -> list[object]:
        raise RuntimeError("detect failed")


@pytest.mark.asyncio
async def test_reproducible_discovery_detect_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _replay_with_one_event(
        monkeypatch,
        _DetectFailureStrategy(),
        event_type=EventType.MARKET_DATA_REFRESH,
        fail_closed=False,
    ) == []
    with pytest.raises(RuntimeError, match="detect failed"):
        await _replay_with_one_event(
            monkeypatch,
            _DetectFailureStrategy(),
            event_type=EventType.MARKET_DATA_REFRESH,
            fail_closed=True,
        )


class _OnEventFailureStrategy(BaseStrategy):
    strategy_type = "selected_on_event_failure"
    name = "Selected on-event failure"
    description = "Selected on-event fixture"
    subscriptions = [EventType.CRYPTO_UPDATE]

    async def on_event(self, event: object) -> list[object]:
        raise RuntimeError("on_event failed")


@pytest.mark.asyncio
async def test_reproducible_discovery_on_event_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _replay_with_one_event(
        monkeypatch,
        _OnEventFailureStrategy(),
        event_type=EventType.CRYPTO_UPDATE,
        fail_closed=False,
    ) == []
    with pytest.raises(RuntimeError, match="on_event failed"):
        await _replay_with_one_event(
            monkeypatch,
            _OnEventFailureStrategy(),
            event_type=EventType.CRYPTO_UPDATE,
            fail_closed=True,
        )


class _EvaluateFailureStrategy(BaseStrategy):
    strategy_type = "selected_evaluate_failure"
    name = "Selected evaluate failure"
    description = "Selected evaluate fixture"

    def evaluate(self, signal: object, context: dict[str, object]) -> object:
        raise RuntimeError("evaluate failed")


def test_reproducible_evaluate_failure_raises() -> None:
    import services.strategy_backtester as strategy_backtester

    opportunity = SimpleNamespace(
        id="opportunity",
        strategy_type="selected_evaluate_failure",
        expected_roi=5.0,
        risk_score=0.0,
    )
    positions = {
        "positions_to_take": [
            {
                "token_id": "YES",
                "market_id": "market",
                "action": "BUY",
                "price": 0.40,
                "notional_usd": 20.0,
            }
        ]
    }

    assert strategy_backtester._backtest_evaluate_opportunity(
        strategy=_EvaluateFailureStrategy(),
        opp=opportunity,
        pdata=positions,
        initial_capital_usd=1000.0,
        fail_closed=False,
    ) is None
    with pytest.raises(RuntimeError, match="evaluate failed"):
        strategy_backtester._backtest_evaluate_opportunity(
            strategy=_EvaluateFailureStrategy(),
            opp=opportunity,
            pdata=positions,
            initial_capital_usd=1000.0,
            fail_closed=True,
        )


@pytest.mark.asyncio
async def test_reproducible_unified_run_skips_all_mutable_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.backtest.unified_runner as unified_runner
    import services.external_data.parquet_scanner as parquet_scanner
    import services.external_data.provider_import_service as provider_import_service

    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 2, tzinfo=timezone.utc)
    market_data_view = object()
    received: dict[str, Any] = {}

    async def resolve_scope(dataset_ids: list[str]) -> dict[str, object]:
        return {
            "dataset_ids": dataset_ids,
            "token_ids": ["YES", "NO"],
            "start": start,
            "end": end,
        }

    async def coverage_summary(**kwargs: object) -> tuple[dict[str, object], None]:
        assert kwargs["ensure_scan"] is False
        return ({"requested_tokens": 2, "covered_tokens": 2}, None)

    async def execution_backtest(**kwargs: object) -> ExecutionBacktestResult:
        received.update(kwargs)
        return ExecutionBacktestResult(
            success=True,
            strategy_slug="over25",
            strategy_name="Over 2.5",
            settlement_summary={"settled_positions": 1},
            dataset_snapshot={"provider_dataset_ids": ["selected"]},
            effective_football={"condition_id": "market"},
        )

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("mutable enrichment must not run in reproducible mode")

    async def forbidden_async(*args: object, **kwargs: object) -> None:
        forbidden()

    monkeypatch.setattr(provider_import_service, "resolve_dataset_scope", resolve_scope)
    monkeypatch.setattr(parquet_scanner, "ensure_recent_scan", forbidden_async)
    monkeypatch.setattr(parquet_scanner, "suspend_scan", forbidden)
    monkeypatch.setattr(parquet_scanner, "resume_scan", forbidden)
    monkeypatch.setattr(unified_runner, "_resolve_coverage_summary", coverage_summary)
    monkeypatch.setattr(unified_runner, "run_execution_backtest", execution_backtest)
    for name in (
        "_capture_fill_model_snapshot",
        "_capture_decomposition_summary",
        "_capture_latency",
        "_capture_empirical_constants",
        "_capture_data_quality",
        "_capture_outcome_netting",
        "_capture_trade_order_monte_carlo",
        "_capture_counterfactuals_for_fills",
        "_capture_ensemble_band_for_fills",
        "_compute_fill_calibration",
    ):
        monkeypatch.setattr(
            unified_runner,
            name,
            forbidden_async if name.startswith(("_capture_fill", "_capture_decomposition", "_capture_latency", "_capture_outcome", "_capture_counterfactuals", "_capture_ensemble", "_compute_fill")) else forbidden,
        )

    result = await unified_runner.run_unified_backtest(
        source_code="class Strategy: pass",
        slug="over25",
        config={"edge": 0.03},
        token_ids=["YES", "NO"],
        provider_dataset_ids=["selected"],
        start=start,
        end=end,
        run_id="hr-reproducible",
        execution_mode="evosport_reproducible",
        reproducible_projected_market=_PROJECTED_MARKET,
        market_data_view=market_data_view,
    )

    assert received["execution_mode"] == "evosport_reproducible"
    assert received["market_data_view"] is market_data_view
    assert set(result) == {
        "run_id",
        "strategy_slug",
        "strategy_name",
        "execution",
        "data_coverage",
        "effective_dataset",
        "effective_football",
    }


@pytest.mark.asyncio
async def test_reproducible_execution_uses_only_source_and_spec_strategy_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.strategy_backtester as strategy_backtester

    database_touched = False
    received_config: dict[str, object] = {}
    received_slugs: list[str] = []
    observed_strategy_identities: list[tuple[str, str]] = []
    source_code = '''from services.strategies.base import BaseStrategy

class Fixture(BaseStrategy):
    strategy_type = "source_fixture"
    name = "Fixture"
    description = "Fixture"

    def detect(self, events, markets, prices):
        return []
'''
    real_loader = strategy_backtester.StrategyLoader()

    class ForbiddenSessionFactory:
        def __call__(self) -> object:
            nonlocal database_touched
            database_touched = True
            raise AssertionError("mutable strategy DB config must not be queried")

    class StopAfterLoader:
        def load(
            self,
            slug: str,
            source_code: str,
            config: dict[str, object],
            *,
            reproducible: bool = False,
        ) -> None:
            received_slugs.append(slug)
            received_config.update(config)
            loaded = real_loader.load(slug, source_code, config, reproducible=reproducible)
            observed_strategy_identities.append(
                (loaded.instance.key, loaded.instance.strategy_type)
            )
            raise RuntimeError("stop after observing loader inputs")

    monkeypatch.setattr(
        strategy_backtester,
        "validate_strategy_source",
        lambda source, *, reproducible=False: {
            "valid": True,
            "errors": [],
            "warnings": [],
            "class_name": "Fixture",
        },
    )
    monkeypatch.setattr(strategy_backtester, "AsyncSessionLocal", ForbiddenSessionFactory())
    monkeypatch.setattr(strategy_backtester, "StrategyLoader", StopAfterLoader)
    wall_clock = iter((1000.0, 2000.0))
    monkeypatch.setattr(strategy_backtester.time, "time", lambda: next(wall_clock))

    results = [
        await strategy_backtester.run_execution_backtest(
            source_code,
            slug="fixture",
            config={"spec_value": 7},
            provider_dataset_ids=["selected"],
            execution_mode="evosport_reproducible",
            reproducible_projected_market=_PROJECTED_MARKET,
            market_data_view=object(),
        )
        for _ in range(2)
    ]

    assert database_touched is False
    assert received_config == {"spec_value": 7}
    assert received_slugs == [
        "_bt_exec_fixture_666bed4d1228fdcf",
        "_bt_exec_fixture_666bed4d1228fdcf",
    ]
    assert observed_strategy_identities == [
        ("_bt_exec_fixture_666bed4d1228fdcf", "_bt_exec_fixture_666bed4d1228fdcf"),
        ("_bt_exec_fixture_666bed4d1228fdcf", "_bt_exec_fixture_666bed4d1228fdcf"),
    ]
    assert {
        result.runtime_error for result in results
    } == {"Failed to load strategy: stop after observing loader inputs"}


@pytest.mark.asyncio
async def test_reproducible_execution_uses_the_same_source_contract_before_and_during_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.strategy_backtester as strategy_backtester

    contracts: list[tuple[str, bool]] = []

    def validate(source: str, *, reproducible: bool = False) -> dict[str, object]:
        contracts.append(("preload", reproducible))
        return {"valid": True, "errors": [], "warnings": [], "class_name": "Fixture"}

    class StopAtLoader:
        def load(
            self,
            slug: str,
            source_code: str,
            config: dict[str, object],
            *,
            reproducible: bool = False,
        ) -> None:
            contracts.append(("load", reproducible))
            raise RuntimeError("stop after contract capture")

    monkeypatch.setattr(strategy_backtester, "validate_strategy_source", validate)
    monkeypatch.setattr(strategy_backtester, "StrategyLoader", StopAtLoader)

    result = await strategy_backtester.run_execution_backtest(
        "class Fixture: pass",
        slug="fixture",
        provider_dataset_ids=["selected"],
        execution_mode="evosport_reproducible",
        reproducible_projected_market=_PROJECTED_MARKET,
        market_data_view=object(),
    )

    assert contracts == [("preload", True), ("load", True)]
    assert result.runtime_error == "Failed to load strategy: stop after contract capture"


@pytest.mark.asyncio
async def test_standard_execution_loader_key_remains_timestamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.strategy_backtester as strategy_backtester

    received_slugs: list[str] = []

    class UnavailableSessionFactory:
        def __call__(self) -> object:
            raise RuntimeError("standard fixture has no strategy database")

    class StopAfterLoader:
        def load(
            self,
            slug: str,
            source_code: str,
            config: dict[str, object],
            *,
            reproducible: bool = False,
        ) -> None:
            received_slugs.append(slug)
            raise RuntimeError("stop after observing loader key")

    monkeypatch.setattr(
        strategy_backtester,
        "validate_strategy_source",
        lambda source, *, reproducible=False: {
            "valid": True,
            "errors": [],
            "warnings": [],
            "class_name": "Fixture",
        },
    )
    monkeypatch.setattr(strategy_backtester, "AsyncSessionLocal", UnavailableSessionFactory())
    monkeypatch.setattr(strategy_backtester, "StrategyLoader", StopAfterLoader)
    monkeypatch.setattr(strategy_backtester.time, "time", lambda: 1234.0)

    result = await strategy_backtester.run_execution_backtest(
        "class Fixture: pass",
        slug="fixture",
        config={"spec_value": 7},
    )

    assert received_slugs == ["_bt_exec_fixture_1234"]
    assert result.runtime_error == "Failed to load strategy: stop after observing loader key"


@pytest.mark.asyncio
async def test_reproducible_recorded_event_import_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.strategy_backtester as strategy_backtester

    monkeypatch.setitem(sys.modules, "services.recorded_event_bus.backtest_bridge", None)

    with pytest.raises(ModuleNotFoundError):
        await strategy_backtester._replay_bus_events_into_tick_grid(
            strategy=object(),
            start_dt=datetime(2026, 8, 1, tzinfo=timezone.utc),
            ticks=[],
            actual_interval=1.0,
            n_ticks=0,
            events_by_tick=[],
            candidate_token_ids=["YES", "NO"],
            catalog_markets=[],
            provider_dataset_ids=("selected",),
            fail_closed=True,
        )
