from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, model_validator

from evosport.domain.sports import (
    CanonicalSportsContract,
    CanonicalSportsEvent,
    EventStatus,
    MarketType,
    SettlementOutcome,
)
from evosport.domain.time import TemporalEnvelope, parse_utc_iso
from evosport.semantics.football_totals import FootballMatchResult, settle_total_goals
from services.backtest.settlement_store import (
    SettlementRecord,
    TokenMarketMeta,
    build_token_settlements,
    get_settlement_records,
    upsert_settlement,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class FootballDatasetBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event: CanonicalSportsEvent
    contract: CanonicalSportsContract
    result: FootballMatchResult
    result_time: TemporalEnvelope

    @model_validator(mode="after")
    def validate_binding(self) -> "FootballDatasetBinding":
        if self.event.sport != "football":
            raise ValueError("event sport must be football")
        if self.contract.event_id != self.event.event_id:
            raise ValueError("contract event_id must match event")
        if self.contract.market_type != MarketType.TOTAL_GOALS_OVER_UNDER:
            raise ValueError("contract must be total goals over/under")
        if self.contract.threshold != Decimal("2.5"):
            raise ValueError("contract threshold must be exactly 2.5")
        if self.contract.side != "over":
            raise ValueError("the football slice requires YES to mean over 2.5")
        match_start = self.event.actual_start or self.event.scheduled_start
        if self.contract.closes_at > match_start:
            raise ValueError("contract must close before or at match start")
        if self.result.status != self.event.status:
            raise ValueError("result status must match event status")
        if self.result_time.event_time < self.contract.closes_at:
            raise ValueError("result event_time must not precede market close")
        if self.result_time.event_time < match_start:
            raise ValueError("result event_time must not precede match start")
        if self.result_time.event_time > self.result_time.observed_at:
            raise ValueError("result event_time cannot be after observed_at")
        if self.event.status == EventStatus.SCHEDULED and self.result_time.event_time <= match_start:
            raise ValueError("an unresolved scheduled event cannot carry an early final result")
        return self

    def validate_provider_dataset(self, dataset: object) -> None:
        payload = getattr(dataset, "payload_json", None)
        if not isinstance(payload, dict):
            raise ValueError("provider dataset payload_json must be an object")
        expected = {
            "canonical": True,
            "schema_version": "snapshots_v2",
            "sport": "football",
            "event_id": self.event.event_id,
            "condition_id": self.contract.venue_market_id,
            "clob_token_up": self.contract.yes_token_id,
            "clob_token_down": self.contract.no_token_id,
            "market_type": self.contract.market_type.value,
        }
        mismatches = [key for key, value in expected.items() if payload.get(key) != value]
        try:
            threshold = Decimal(str(payload.get("threshold")))
        except (InvalidOperation, TypeError, ValueError):
            threshold = None
        if threshold != self.contract.threshold:
            mismatches.append("threshold")
        if getattr(dataset, "provider", None) != self.contract.venue:
            mismatches.append("provider")
        if set(getattr(dataset, "token_ids_json", None) or ()) != {
            self.contract.yes_token_id,
            self.contract.no_token_id,
        }:
            mismatches.append("token_ids_json")
        if getattr(dataset, "storage_type", None) != "parquet":
            mismatches.append("storage_type")

        dataset_start = getattr(dataset, "start_ts", None)
        dataset_end = getattr(dataset, "end_ts", None)
        if dataset_start is None or _utc(dataset_start) > self.contract.opens_at:
            mismatches.append("start_ts")
        if dataset_end is None or _utc(dataset_end) < self.contract.closes_at:
            mismatches.append("end_ts")
        for payload_key, expected_time in (
            ("market_start_time", self.contract.opens_at),
            ("market_end_time", self.contract.closes_at),
        ):
            try:
                actual_time = parse_utc_iso(
                    f"provider dataset {getattr(dataset, 'id', '<unknown>')} {payload_key}",
                    payload.get(payload_key),
                )
            except ValueError:
                actual_time = None
            if actual_time != expected_time:
                mismatches.append(payload_key)
        if mismatches:
            raise ValueError(
                f"provider dataset {getattr(dataset, 'id', '<unknown>')} does not match football binding: "
                f"{sorted(set(mismatches))}"
            )


def expected_football_settlement(binding: FootballDatasetBinding) -> SettlementRecord:
    outcome = settle_total_goals(binding.contract, binding.result)
    winning_token_id = None
    if outcome == SettlementOutcome.YES:
        winning_token_id = binding.contract.yes_token_id
    elif outcome == SettlementOutcome.NO:
        winning_token_id = binding.contract.no_token_id
    return SettlementRecord(
        condition_id=binding.contract.venue_market_id,
        slug=None,
        winning_token_id=winning_token_id,
        winning_outcome=outcome.value.upper(),
        token_ids=(binding.contract.yes_token_id, binding.contract.no_token_id),
        resolution_time=binding.result_time.observed_at,
        coin_price_start=None,
        coin_price_end=None,
        resolved=outcome != SettlementOutcome.UNRESOLVED,
        source=f"evosport:football:{binding.contract.rule_version}",
    )


def frozen_projected_market_identity(binding: FootballDatasetBinding) -> dict[str, object]:
    threshold = format(binding.contract.threshold, "f")
    return {
        "market_id": binding.contract.venue_market_id,
        "condition_id": binding.contract.venue_market_id,
        "slug": binding.contract.contract_id,
        "title": (
            f"{binding.event.home_team} v {binding.event.away_team}: "
            f"{binding.contract.side.capitalize()} {threshold} total goals"
        ),
        "coin": None,
        "timeframe": binding.contract.market_type.value,
        "market_start": _utc(binding.contract.opens_at).isoformat(),
        "market_close": _utc(binding.contract.closes_at).isoformat(),
        "yes_token_id": binding.contract.yes_token_id,
        "no_token_id": binding.contract.no_token_id,
        "price_to_beat": None,
    }


def _settlement_identity(record: SettlementRecord) -> dict[str, object]:
    return {
        "condition_id": record.condition_id,
        "slug": record.slug,
        "winning_token_id": record.winning_token_id,
        "winning_outcome": record.winning_outcome,
        "token_ids": sorted(record.token_ids),
        "resolution_time": _utc(record.resolution_time).isoformat() if record.resolution_time else None,
        "coin_price_start": record.coin_price_start,
        "coin_price_end": record.coin_price_end,
        "resolved": record.resolved,
        "source": record.source,
    }


async def load_current_football_identity(
    binding: FootballDatasetBinding,
    provider_dataset_ids: Sequence[str],
) -> dict[str, object]:
    from sqlalchemy import select

    from models.database import AsyncSessionLocal, ProviderDataset

    selected_ids = tuple(sorted(set(str(value) for value in provider_dataset_ids if value)))
    if not selected_ids:
        raise ValueError("provider_dataset_ids cannot be empty")
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(select(ProviderDataset).where(ProviderDataset.id.in_(selected_ids)))
        ).scalars().all()
    rows_by_id = {str(row.id): row for row in rows}
    if set(rows_by_id) != set(selected_ids):
        raise ValueError(
            "current football catalog IDs do not match manifest: "
            f"expected={list(selected_ids)} actual={sorted(rows_by_id)}"
        )

    provider_datasets: list[dict[str, object]] = []
    for dataset_id in selected_ids:
        row = rows_by_id[dataset_id]
        binding.validate_provider_dataset(row)
        payload = row.payload_json
        provider_datasets.append(
            {
                "provider_dataset_id": dataset_id,
                "provider": str(row.provider),
                "canonical": payload["canonical"],
                "schema_version": payload["schema_version"],
                "sport": payload["sport"],
                "event_id": payload["event_id"],
                "condition_id": payload["condition_id"],
                "yes_token_id": payload["clob_token_up"],
                "no_token_id": payload["clob_token_down"],
                "market_type": payload["market_type"],
                "threshold": str(Decimal(str(payload["threshold"]))),
                "token_ids": sorted(str(token) for token in row.token_ids_json),
                "storage_type": str(row.storage_type),
                "row_start": _utc(row.start_ts).isoformat(),
                "row_end": _utc(row.end_ts).isoformat(),
                "market_start": parse_utc_iso(
                    f"provider dataset {dataset_id} market_start_time",
                    payload["market_start_time"],
                ).isoformat(),
                "market_end": parse_utc_iso(
                    f"provider dataset {dataset_id} market_end_time",
                    payload["market_end_time"],
                ).isoformat(),
            }
        )

    expected = expected_football_settlement(binding)
    records = await get_settlement_records([expected.condition_id])
    actual = records.get(expected.condition_id)
    if actual is None:
        raise ValueError(f"current football settlement is missing: {expected.condition_id}")
    expected_identity = _settlement_identity(expected)
    actual_identity = _settlement_identity(actual)
    if actual_identity != expected_identity:
        raise ValueError(
            "current football settlement does not match frozen binding: "
            f"expected={expected_identity} actual={actual_identity}"
        )
    return {
        "provider_dataset_ids": list(selected_ids),
        "provider_datasets": provider_datasets,
        "settlement": actual_identity,
    }


def validate_effective_football_evidence(
    binding: FootballDatasetBinding,
    provider_dataset_ids: Sequence[str],
    current_identity: dict[str, object],
    evidence: object,
) -> None:
    selected_ids = sorted(set(str(value) for value in provider_dataset_ids if value))
    expected_record = expected_football_settlement(binding)
    if current_identity.get("provider_dataset_ids") != selected_ids:
        raise ValueError("current football catalog IDs do not match frozen manifest")
    if current_identity.get("settlement") != _settlement_identity(expected_record):
        raise ValueError("current football settlement does not match frozen binding")
    token_settlements = build_token_settlements(
        [
            TokenMarketMeta(
                token_id=binding.contract.yes_token_id,
                condition_id=binding.contract.venue_market_id,
            ),
            TokenMarketMeta(
                token_id=binding.contract.no_token_id,
                condition_id=binding.contract.venue_market_id,
            ),
        ],
        {binding.contract.venue_market_id: expected_record},
    )
    projected_market = frozen_projected_market_identity(binding)
    expected = {
        "provider_dataset_ids": selected_ids,
        "markets": [projected_market],
        "token_settlements": [
            {
                "token_id": token_id,
                "condition_id": settlement.condition_id,
                "settlement_price": settlement.settle_price,
                "winning_outcome": settlement.winning_outcome,
                "resolution_time": (
                    _utc(settlement.resolution_time).isoformat()
                    if settlement.resolution_time is not None
                    else None
                ),
                "source": settlement.source,
            }
            for token_id, settlement in sorted(token_settlements.items())
        ],
    }
    if evidence != expected:
        raise ValueError(
            "effective football semantics do not match frozen/current identity: "
            f"expected={expected} actual={evidence}"
        )


async def store_football_settlement(binding: FootballDatasetBinding) -> SettlementRecord:
    record = expected_football_settlement(binding)
    await upsert_settlement(record)
    return record
