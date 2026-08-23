from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from evosport.domain.sports import (
    CanonicalSportsContract,
    CanonicalSportsEvent,
    EventStatus,
    MarketType,
    PostponedAction,
    SettlementOutcome,
    SettlementPolicy,
)
from evosport.domain.time import TemporalEnvelope


def test_temporal_envelope_rejects_naive_datetime():
    with pytest.raises(ValidationError):
        TemporalEnvelope(
            event_time=datetime(2026, 8, 1, 12),
            observed_at=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
            ingested_at=datetime(2026, 8, 1, 11, 1, tzinfo=timezone.utc),
        )


def test_temporal_envelope_normalizes_aware_datetimes_to_utc():
    offset = timezone(timedelta(hours=2))
    envelope = TemporalEnvelope(
        event_time=datetime(2026, 8, 1, 12, tzinfo=offset),
        observed_at=datetime(2026, 8, 1, 11, tzinfo=offset),
        ingested_at=datetime(2026, 8, 1, 11, 1, tzinfo=offset),
        effective_from=datetime(2026, 8, 1, 10, tzinfo=offset),
        effective_to=datetime(2026, 8, 1, 13, tzinfo=offset),
    )

    assert envelope.event_time == datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    assert envelope.observed_at == datetime(2026, 8, 1, 9, tzinfo=timezone.utc)
    assert envelope.ingested_at == datetime(2026, 8, 1, 9, 1, tzinfo=timezone.utc)
    assert envelope.effective_from == datetime(2026, 8, 1, 8, tzinfo=timezone.utc)
    assert envelope.effective_to == datetime(2026, 8, 1, 11, tzinfo=timezone.utc)


def test_temporal_envelope_rejects_observation_after_ingestion():
    with pytest.raises(ValidationError, match="observed_at cannot be after ingested_at"):
        TemporalEnvelope(
            event_time=datetime(2026, 8, 2, tzinfo=timezone.utc),
            observed_at=datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
            ingested_at=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
        )


def test_temporal_envelope_rejects_invalid_effective_interval():
    with pytest.raises(ValidationError, match="effective_from must be before effective_to"):
        TemporalEnvelope(
            event_time=datetime(2026, 8, 2, tzinfo=timezone.utc),
            observed_at=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
            ingested_at=datetime(2026, 8, 1, 11, 1, tzinfo=timezone.utc),
            effective_from=datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
            effective_to=datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
        )


def test_event_normalizes_all_datetime_fields_to_utc():
    offset = timezone(timedelta(hours=2))
    event = CanonicalSportsEvent(
        event_id="football:epl:2026:ars-che",
        competition="epl",
        season="2026",
        home_team="arsenal",
        away_team="chelsea",
        scheduled_start=datetime(2026, 8, 1, 20, tzinfo=offset),
        actual_start=datetime(2026, 8, 1, 20, 15, tzinfo=offset),
    )

    assert event.scheduled_start == datetime(2026, 8, 1, 18, tzinfo=timezone.utc)
    assert event.actual_start == datetime(2026, 8, 1, 18, 15, tzinfo=timezone.utc)
    assert event.scheduled_start.tzinfo is timezone.utc
    assert event.actual_start.tzinfo is timezone.utc


def test_event_rejects_naive_datetime():
    with pytest.raises(ValidationError):
        CanonicalSportsEvent(
            event_id="football:epl:2026:ars-che",
            competition="epl",
            season="2026",
            home_team="arsenal",
            away_team="chelsea",
            scheduled_start=datetime(2026, 8, 1, 20),
        )


def test_contract_normalizes_all_datetime_fields_to_utc():
    offset = timezone(timedelta(hours=-4))
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
        opens_at=datetime(2026, 8, 1, 8, tzinfo=offset),
        closes_at=datetime(2026, 8, 2, 8, tzinfo=offset),
        rule_version="sha256:rules-v1",
        settlement=SettlementPolicy(includes_extra_time=False),
    )

    assert contract.opens_at == datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    assert contract.closes_at == datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
    assert contract.opens_at.tzinfo is timezone.utc
    assert contract.closes_at.tzinfo is timezone.utc


def test_contract_rejects_naive_datetime():
    with pytest.raises(ValidationError):
        CanonicalSportsContract(
            contract_id="pm:epl-ars-che-over-2-5",
            event_id="football:epl:2026:ars-che",
            venue="polymarket",
            venue_market_id="market-1",
            yes_token_id="yes-1",
            no_token_id="no-1",
            market_type=MarketType.TOTAL_GOALS_OVER_UNDER,
            threshold=Decimal("2.5"),
            side="over",
            opens_at=datetime(2026, 8, 1, 12),
            closes_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            rule_version="sha256:rules-v1",
            settlement=SettlementPolicy(),
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


def test_contract_rejects_non_chronological_window():
    with pytest.raises(ValidationError, match="opens_at must be before closes_at"):
        CanonicalSportsContract(
            contract_id="pm:epl-ars-che-over-2-5",
            event_id="football:epl:2026:ars-che",
            venue="polymarket",
            venue_market_id="market-1",
            yes_token_id="yes-1",
            no_token_id="no-1",
            market_type=MarketType.TOTAL_GOALS_OVER_UNDER,
            threshold=Decimal("2.5"),
            side="over",
            opens_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            closes_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            rule_version="sha256:rules-v1",
            settlement=SettlementPolicy(),
        )


def test_contract_rejects_invalid_side():
    with pytest.raises(ValidationError, match="side must be over or under"):
        CanonicalSportsContract(
            contract_id="pm:epl-ars-che-over-2-5",
            event_id="football:epl:2026:ars-che",
            venue="polymarket",
            venue_market_id="market-1",
            yes_token_id="yes-1",
            no_token_id="no-1",
            market_type=MarketType.TOTAL_GOALS_OVER_UNDER,
            threshold=Decimal("2.5"),
            side="push",
            opens_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            closes_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            rule_version="sha256:rules-v1",
            settlement=SettlementPolicy(),
        )


def test_contract_rejects_non_positive_threshold():
    with pytest.raises(ValidationError, match="threshold must be positive"):
        CanonicalSportsContract(
            contract_id="pm:epl-ars-che-over-2-5",
            event_id="football:epl:2026:ars-che",
            venue="polymarket",
            venue_market_id="market-1",
            yes_token_id="yes-1",
            no_token_id="no-1",
            market_type=MarketType.TOTAL_GOALS_OVER_UNDER,
            threshold=Decimal("0"),
            side="over",
            opens_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            closes_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            rule_version="sha256:rules-v1",
            settlement=SettlementPolicy(),
        )


def test_domain_enums_preserve_contract_values():
    assert EventStatus.SCHEDULED.value == "scheduled"
    assert MarketType.TOTAL_GOALS_OVER_UNDER.value == "total_goals_over_under"
    assert PostponedAction.WAIT_FOR_RESCHEDULE.value == "wait_for_reschedule"
    assert SettlementOutcome.UNRESOLVED.value == "unresolved"
