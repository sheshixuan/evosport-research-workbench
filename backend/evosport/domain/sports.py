from decimal import Decimal
from enum import Enum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .time import normalize_utc


class EventStatus(str, Enum):
    SCHEDULED = "scheduled"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class MarketType(str, Enum):
    TOTAL_GOALS_OVER_UNDER = "total_goals_over_under"


class PostponedAction(str, Enum):
    VOID = "void"
    WAIT_FOR_RESCHEDULE = "wait_for_reschedule"


class SettlementOutcome(str, Enum):
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

    @model_validator(mode="after")
    def normalize_datetimes(self) -> "CanonicalSportsEvent":
        object.__setattr__(self, "scheduled_start", normalize_utc(self.scheduled_start))
        if self.actual_start is not None:
            object.__setattr__(self, "actual_start", normalize_utc(self.actual_start))
        return self


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
        object.__setattr__(self, "opens_at", normalize_utc(self.opens_at))
        object.__setattr__(self, "closes_at", normalize_utc(self.closes_at))
        if self.opens_at >= self.closes_at:
            raise ValueError("opens_at must be before closes_at")
        if self.side not in {"over", "under"}:
            raise ValueError("side must be over or under")
        if self.threshold <= 0:
            raise ValueError("threshold must be positive")
        return self
