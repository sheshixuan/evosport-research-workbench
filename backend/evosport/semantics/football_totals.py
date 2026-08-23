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
    if result.status == EventStatus.SCHEDULED:
        return SettlementOutcome.UNRESOLVED

    if result.status == EventStatus.POSTPONED:
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
