from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
from numbers import Real
from typing import Any, Awaitable, Callable, Protocol

from evosport.domain.time import parse_utc_iso


@dataclass(frozen=True)
class BacktestRequest:
    source_code: str
    slug: str
    config: dict[str, Any]
    token_ids: tuple[str, ...]
    provider_dataset_ids: tuple[str, ...]
    market_data_view: Any = field(repr=False, compare=False)
    projected_market: dict[str, Any]
    start: str
    end: str
    initial_capital_usd: float
    submit_p50_ms: float
    submit_p95_ms: float
    cancel_p50_ms: float
    cancel_p95_ms: float
    seed: int
    n_trials: int
    _start_datetime: datetime = field(init=False, repr=False, compare=False)
    _end_datetime: datetime = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        start = parse_utc_iso("start", self.start)
        end = parse_utc_iso("end", self.end)
        if start >= end:
            raise ValueError("start must be before end")
        _validate_finite("initial_capital_usd", self.initial_capital_usd, positive=True)
        _validate_finite("submit_p50_ms", self.submit_p50_ms)
        _validate_finite("submit_p95_ms", self.submit_p95_ms)
        _validate_finite("cancel_p50_ms", self.cancel_p50_ms)
        _validate_finite("cancel_p95_ms", self.cancel_p95_ms)
        _validate_integer("seed", self.seed)
        _validate_integer("n_trials", self.n_trials)
        if self.submit_p95_ms < self.submit_p50_ms:
            raise ValueError("submit_p95_ms cannot be below submit_p50_ms")
        if self.cancel_p95_ms < self.cancel_p50_ms:
            raise ValueError("cancel_p95_ms cannot be below cancel_p50_ms")
        if self.n_trials < 1:
            raise ValueError("n_trials must be at least 1")
        if not self.provider_dataset_ids:
            raise ValueError("provider_dataset_ids cannot be empty")
        if self.provider_dataset_ids != tuple(sorted(set(self.provider_dataset_ids))):
            raise ValueError("provider_dataset_ids must be unique and sorted")
        object.__setattr__(self, "_start_datetime", start)
        object.__setattr__(self, "_end_datetime", end)

    @property
    def start_datetime(self) -> datetime:
        return self._start_datetime

    @property
    def end_datetime(self) -> datetime:
        return self._end_datetime


class BacktestGateway(Protocol):
    async def run(self, request: BacktestRequest) -> dict[str, Any]:
        """Run a validated EvoSport backtest request."""


Runner = Callable[..., Awaitable[dict[str, Any]]]


class HomerunBacktestGateway:
    def __init__(self, runner: Runner | None = None) -> None:
        if runner is None:
            from services.backtest.unified_runner import run_unified_backtest

            runner = run_unified_backtest
        self._runner = runner

    async def run(self, request: BacktestRequest) -> dict[str, Any]:
        return await self._runner(
            source_code=request.source_code,
            slug=request.slug,
            config=_materialize_config(request.config),
            token_ids=list(request.token_ids),
            provider_dataset_ids=list(request.provider_dataset_ids),
            market_data_view=request.market_data_view,
            start=request.start_datetime,
            end=request.end_datetime,
            initial_capital_usd=request.initial_capital_usd,
            submit_p50_ms=request.submit_p50_ms,
            submit_p95_ms=request.submit_p95_ms,
            cancel_p50_ms=request.cancel_p50_ms,
            cancel_p95_ms=request.cancel_p95_ms,
            seed=request.seed,
            n_trials=request.n_trials,
            execution_mode="evosport_reproducible",
            reproducible_projected_market=_materialize_config(request.projected_market),
        )


def _validate_finite(name: str, value: object, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a non-boolean real number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if positive:
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    elif value < 0:
        raise ValueError(f"{name} cannot be negative")


def _validate_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a non-boolean integer")


def _materialize_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _materialize_config(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_materialize_config(item) for item in value]
    return value
