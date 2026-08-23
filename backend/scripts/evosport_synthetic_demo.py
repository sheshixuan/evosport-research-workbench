#!/usr/bin/env python3
"""EvoSport synthetic football O/U 2.5 end-to-end demo.

Builds a fake pre-match football total-goals 2.5 market's canonical
SNAPSHOT_SCHEMA parquet, registers the matching ProviderDataset rows,
freezes the selected dataset, stores the offline football settlement,
and runs the real HomerunBacktestGateway through ExperimentRunner to
produce a NOT_EVALUATED evidence report.

Run from backend/ with a live PostgreSQL (homerun) and:
  export DATABASE_URL=... REDIS_URL=... KMP_DUPLICATE_LIB_OK=TRUE
  venv/bin/python scripts/evosport_synthetic_demo.py [OUTPUT_ROOT]
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import yaml

from models.database import AsyncSessionLocal, ProviderDataset
from evosport.data.freeze import freeze_provider_datasets
from evosport.domain.sports import (
    CanonicalSportsContract,
    CanonicalSportsEvent,
    EventStatus,
    MarketType,
    SettlementPolicy,
)
from evosport.domain.time import TemporalEnvelope
from evosport.experiments.environment import active_distribution_pins
from evosport.experiments.gateway import HomerunBacktestGateway
from evosport.experiments.registry import SqlRunRegistry
from evosport.experiments.runner import ExperimentRunner
from evosport.semantics.football_binding import FootballDatasetBinding, store_football_settlement
from evosport.semantics.football_totals import FootballMatchResult
from services.external_data.parquet_schema import SNAPSHOT_SCHEMA
from services.marketdata.writer import write_canonical_table


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


def _write_dataset_series(root: Path, token_id: str, points: list[tuple[datetime, float]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    tables = [_snapshot_table(token_id, t, b) for t, b in points]
    path = root / f"snapshots__{token_id}.parquet"
    write_canonical_table(
        pa.concat_tables(tables),
        dest_path=path,
        kind="snapshots",
        provider="evosport-test",
    )
    return path


STRATEGY_SOURCE = """
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
        if (
            ask is None
            or float(ask) >= 0.5
            or not markets
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

    def should_exit(self, position, market_state):
        return ExitDecision("hold", "fixture hold")

    def resolve_exit_policy(self, decision, close_trigger):
        return None
"""


async def main(output_root: Path) -> int:
    root = Path(tempfile.mkdtemp(prefix="evosport-demo-"))
    selected_root = root / "selected"

    start = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    kickoff = start + timedelta(hours=1)
    result_observed = kickoff + timedelta(hours=2)

    _write_dataset_series(
        selected_root, "YES",
        [(start, 0.40), (start + timedelta(seconds=1), 0.40), (result_observed, 0.40)],
    )
    _write_dataset_series(
        selected_root, "NO",
        [(start, 0.56), (start + timedelta(seconds=1), 0.56), (result_observed, 0.56)],
    )

    payload = {
        "canonical": True,
        "schema_version": "snapshots_v2",
        "sport": "football",
        "event_id": "event-demo",
        "condition_id": "market-demo",
        "clob_token_up": "YES",
        "clob_token_down": "NO",
        "market_type": "total_goals_over_under",
        "threshold": "2.5",
        "market_start_time": start.isoformat().replace("+00:00", "Z"),
        "market_end_time": kickoff.isoformat().replace("+00:00", "Z"),
    }

    async with AsyncSessionLocal() as session:
        session.add(
            ProviderDataset(
                id="synthetic-demo",
                provider="evosport-test",
                external_id="synthetic-demo",
                external_slug="home-away-over-2-5",
                title="Home v Away O/U 2.5 (synthetic)",
                asset_class="prediction",
                token_ids_json=["YES", "NO"],
                start_ts=start.replace(tzinfo=None),
                end_ts=result_observed.replace(tzinfo=None),
                snapshot_count=6,
                trade_count=0,
                payload_json=payload,
                storage_type="parquet",
                storage_uri=selected_root.resolve().as_uri(),
            )
        )
        await session.commit()

    binding = FootballDatasetBinding(
        event=CanonicalSportsEvent(
            event_id="event-demo",
            competition="fixture-league",
            season="2026",
            home_team="Home",
            away_team="Away",
            scheduled_start=kickoff,
            actual_start=kickoff,
            status=EventStatus.FINISHED,
        ),
        contract=CanonicalSportsContract(
            contract_id="contract-demo",
            event_id="event-demo",
            venue="evosport-test",
            venue_market_id="market-demo",
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

    manifests_dir = root / "manifests"
    manifest = await freeze_provider_datasets(
        provider_dataset_ids=["synthetic-demo"],
        output_root=manifests_dir,
        start=start,
        end=result_observed,
        football=binding,
    )
    await store_football_settlement(binding)
    print("FREEZE manifest_id:", manifest.manifest_id)

    source_path = root / "strategy.py"
    source_path.write_text(STRATEGY_SOURCE, encoding="utf-8")
    lock_path = root / "environment.lock"
    lock_path.write_text("\n".join(active_distribution_pins()) + "\n", encoding="utf-8")
    manifest_path = manifests_dir / manifest.manifest_id / "manifest.json"
    spec_path = root / "experiment.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "name": "synthetic-football-over25-demo",
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

    artifacts_dir = output_root.resolve()
    registry = SqlRunRegistry(AsyncSessionLocal)
    runner = ExperimentRunner(
        registry=registry,
        gateway=HomerunBacktestGateway(),
        artifact_root=artifacts_dir,
        homerun_commit="synthetic-demo-fixture",
        evaluator_version="not-evaluated-v1",
    )
    outcome = await runner.run(spec_path)
    print("RUN decision:", outcome.decision)
    print("RUN artifact_dir:", outcome.artifact_dir)
    print("REPORT:", outcome.artifact_dir / "report.html")
    print("RESULT:", (outcome.artifact_dir / "result.json").name)
    return 0


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("evosport-demo-artifacts")
    raise SystemExit(asyncio.run(main(out)))
