import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from evosport.domain.sports import (
    CanonicalSportsEvent,
    CanonicalSportsContract,
    EventStatus,
    MarketType,
    PostponedAction,
    SettlementPolicy,
)
from evosport.domain.time import TemporalEnvelope
from evosport.semantics import football_binding
from evosport.semantics.football_binding import FootballDatasetBinding
from evosport.semantics.football_totals import FootballMatchResult, settle_total_goals
from services.backtest import (
    BacktestConfig,
    BacktestEngine,
    BookSnapshot,
    InMemoryBookReplay,
    LatencyModel,
    PortfolioConfig,
    PriceLevel,
    TradeIntent,
)
from services.backtest.matching_engine import FeeModel
from services.backtest.settlement_store import TokenMarketMeta, build_token_settlements
from services.backtest.venue_model import TIF_IOC


CASES = json.loads((Path(__file__).parent / "fixtures/evosport/football_total_cases.json").read_text())


def _finished_binding() -> FootballDatasetBinding:
    opens = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    kickoff = opens + timedelta(hours=1)
    return FootballDatasetBinding(
        event=CanonicalSportsEvent(
            event_id="event",
            competition="league",
            season="2026",
            home_team="Home",
            away_team="Away",
            scheduled_start=kickoff,
            actual_start=kickoff,
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
            opens_at=opens,
            closes_at=opens + timedelta(minutes=30),
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


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_football_total_golden_cases(case):
    opens = datetime(2026, 8, 1, tzinfo=timezone.utc)
    contract = CanonicalSportsContract(
        contract_id="case",
        event_id="event",
        venue="fixture",
        venue_market_id="market",
        yes_token_id="yes",
        no_token_id="no",
        market_type=MarketType.TOTAL_GOALS_OVER_UNDER,
        threshold=Decimal("2.5"),
        side=case["side"],
        opens_at=opens,
        closes_at=opens + timedelta(days=1),
        rule_version="fixture-v1",
        settlement=SettlementPolicy(
            includes_extra_time=case["includes_extra_time"],
            postponed_action=PostponedAction(case.get("postponed_action", "void")),
            cancelled_action=PostponedAction(case.get("cancelled_action", "void")),
            abandoned_action=PostponedAction(case.get("abandoned_action", "void")),
        ),
    )
    result = FootballMatchResult(
        regulation_home=case["home"],
        regulation_away=case["away"],
        extra_time_home=case["extra_home"],
        extra_time_away=case["extra_away"],
        status=EventStatus(case["status"]),
    )

    assert settle_total_goals(contract, result).value == case["expected"]


def test_football_match_result_rejects_negative_scores():
    with pytest.raises(ValidationError):
        FootballMatchResult(regulation_home=-1, regulation_away=0, status=EventStatus.FINISHED)

    with pytest.raises(ValidationError):
        FootballMatchResult(regulation_home=0, regulation_away=0, extra_time_home=-1, status=EventStatus.FINISHED)


def test_sports_enums_remain_importable_on_python_310():
    source = (Path(__file__).parents[1] / "evosport/domain/sports.py").read_text(encoding="utf-8")

    assert "StrEnum" not in source
    assert isinstance(EventStatus.FINISHED, str)
    assert isinstance(MarketType.TOTAL_GOALS_OVER_UNDER, str)


def test_finished_result_cannot_be_observed_before_match_start():
    opens = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    kickoff = opens + timedelta(hours=1)
    with pytest.raises(ValidationError, match="match start"):
        FootballDatasetBinding(
            event=CanonicalSportsEvent(
                event_id="event",
                competition="league",
                season="2026",
                home_team="Home",
                away_team="Away",
                scheduled_start=kickoff,
                actual_start=kickoff,
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
                opens_at=opens,
                closes_at=opens + timedelta(minutes=30),
                rule_version="fixture-v1",
                settlement=SettlementPolicy(),
            ),
            result=FootballMatchResult(
                regulation_home=2,
                regulation_away=1,
                status=EventStatus.FINISHED,
            ),
            result_time=TemporalEnvelope(
                event_time=opens + timedelta(minutes=45),
                observed_at=opens + timedelta(minutes=46),
                ingested_at=opens + timedelta(minutes=47),
            ),
        )


def test_football_binding_rejects_non_object_provider_metadata() -> None:
    with pytest.raises(ValueError, match="payload_json must be an object"):
        _finished_binding().validate_provider_dataset(SimpleNamespace(payload_json=["invalid"]))


def _provider_dataset_metadata(
    binding: FootballDatasetBinding,
    *,
    market_start_time: str,
    market_end_time: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="football-selected",
        provider=binding.contract.venue,
        token_ids_json=[binding.contract.yes_token_id, binding.contract.no_token_id],
        storage_type="parquet",
        start_ts=binding.contract.opens_at.replace(tzinfo=None),
        end_ts=binding.contract.closes_at.replace(tzinfo=None),
        payload_json={
            "canonical": True,
            "schema_version": "snapshots_v2",
            "sport": "football",
            "event_id": binding.event.event_id,
            "condition_id": binding.contract.venue_market_id,
            "clob_token_up": binding.contract.yes_token_id,
            "clob_token_down": binding.contract.no_token_id,
            "market_type": binding.contract.market_type.value,
            "threshold": "2.5",
            "market_start_time": market_start_time,
            "market_end_time": market_end_time,
        },
    )


def test_football_provider_metadata_accepts_terminal_uppercase_z_on_python_310(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.domain.time as time_boundary

    class Python310Datetime:
        @staticmethod
        def fromisoformat(value: str) -> datetime:
            assert not value.endswith("Z")
            return datetime.fromisoformat(value)

    monkeypatch.setattr(time_boundary, "datetime", Python310Datetime)
    binding = _finished_binding()
    dataset = _provider_dataset_metadata(
        binding,
        market_start_time=binding.contract.opens_at.isoformat().replace("+00:00", "Z"),
        market_end_time=binding.contract.closes_at.isoformat().replace("+00:00", "Z"),
    )

    binding.validate_provider_dataset(dataset)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("market_start_time", "2026-08-01T10:00:00"),
        ("market_end_time", "not-an-iso-timestamp"),
        ("market_end_time", "2026-08-01T10:30:00z"),
    ],
)
def test_football_provider_metadata_rejects_naive_or_malformed_times(
    field: str,
    value: str,
) -> None:
    binding = _finished_binding()
    values = {
        "market_start_time": binding.contract.opens_at.isoformat(),
        "market_end_time": binding.contract.closes_at.isoformat(),
        field: value,
    }
    dataset = _provider_dataset_metadata(binding, **values)

    with pytest.raises(ValueError, match=field):
        binding.validate_provider_dataset(dataset)


@pytest.mark.parametrize(
    ("status", "home", "away", "winning_token", "winning_outcome", "resolved", "redemptions"),
    [
        (EventStatus.FINISHED, 2, 1, "YES", "YES", True, {"YES": 1.0, "NO": 0.0}),
        (EventStatus.FINISHED, 1, 0, "NO", "NO", True, {"YES": 0.0, "NO": 1.0}),
        (EventStatus.POSTPONED, 0, 0, None, "VOID", True, {"YES": 0.5, "NO": 0.5}),
        (EventStatus.SCHEDULED, 0, 0, None, "UNRESOLVED", False, {"YES": None, "NO": None}),
    ],
)
def test_expected_football_settlement_drives_exact_token_redemption(
    status: EventStatus,
    home: int,
    away: int,
    winning_token: str | None,
    winning_outcome: str,
    resolved: bool,
    redemptions: dict[str, float | None],
) -> None:
    base = _finished_binding()
    binding = FootballDatasetBinding(
        event=base.event.model_copy(update={"status": status}),
        contract=base.contract,
        result=FootballMatchResult(
            regulation_home=home,
            regulation_away=away,
            status=status,
        ),
        result_time=base.result_time,
    )

    record = football_binding.expected_football_settlement(binding)
    settlements = build_token_settlements(
        [
            TokenMarketMeta(token_id="YES", condition_id="market"),
            TokenMarketMeta(token_id="NO", condition_id="market"),
        ],
        {"market": record},
    )

    assert record.condition_id == "market"
    assert record.winning_token_id == winning_token
    assert record.winning_outcome == winning_outcome
    assert record.token_ids == ("YES", "NO")
    assert record.resolution_time == base.result_time.observed_at
    assert record.resolved is resolved
    assert record.source == "evosport:football:fixture-v1"
    assert {token: settlements[token].settle_price for token in ("YES", "NO")} == redemptions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "home", "away", "final_equity", "settled", "marked_to_mid"),
    [
        (EventStatus.FINISHED, 1, 0, 990.0, 1, 0),
        (EventStatus.POSTPONED, 0, 0, 1000.0, 1, 0),
        (EventStatus.SCHEDULED, 0, 0, 999.9, 0, 1),
    ],
    ids=("no", "void", "unresolved"),
)
async def test_football_no_void_and_unresolved_settlements_drive_engine_economics(
    status: EventStatus,
    home: int,
    away: int,
    final_equity: float,
    settled: int,
    marked_to_mid: int,
) -> None:
    base = _finished_binding()
    binding = FootballDatasetBinding(
        event=base.event.model_copy(update={"status": status}),
        contract=base.contract,
        result=FootballMatchResult(
            regulation_home=home,
            regulation_away=away,
            status=status,
        ),
        result_time=base.result_time,
    )
    record = football_binding.expected_football_settlement(binding)
    settlements = build_token_settlements(
        [
            TokenMarketMeta(token_id="YES", condition_id="market"),
            TokenMarketMeta(token_id="NO", condition_id="market"),
        ],
        {"market": record},
    )
    snapshots = InMemoryBookReplay(
        [
            BookSnapshot(
                token_id="YES",
                observed_at=observed_at,
                bids=(PriceLevel(0.49, 50.0),),
                asks=(PriceLevel(0.50, 50.0),),
            )
            for observed_at in (
                base.contract.opens_at,
                base.contract.opens_at + timedelta(seconds=1),
                base.result_time.observed_at + timedelta(seconds=1),
            )
        ]
    )
    engine = BacktestEngine(
        config=BacktestConfig(
            portfolio=PortfolioConfig(initial_capital_usd=1000.0),
            latency=LatencyModel.deterministic(submit_ms=100, cancel_ms=50),
            fees=FeeModel(
                per_fill_gas_usd=0.0,
                resolution_fee_rate=0.0,
                use_taker_fee_curve=False,
            ),
            settlements=settlements,
            seed=42,
        )
    )
    result = await engine.run(
        book_source=snapshots,
        trade_intents=[
            TradeIntent(
                intent_id="football-entry",
                emitted_at=base.contract.opens_at,
                token_id="YES",
                side="BUY",
                size=20.0,
                limit_price=0.51,
                tif=TIF_IOC,
                post_only=False,
                strategy_slug="football-settlement-fixture",
            )
        ],
    )

    assert result.final_equity_usd == pytest.approx(final_equity, abs=1e-9)
    assert result.closed_position_count == 1
    assert result.notes["settlement"] == {
        "settled_positions": settled,
        "marked_to_mid_positions": marked_to_mid,
        "settlements_available": 2,
    }
