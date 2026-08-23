from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from evosport.experiments.gateway import BacktestRequest, HomerunBacktestGateway
from evosport.experiments.spec import FrozenConfig


_MARKET_DATA_VIEW = object()


def _request_kwargs() -> dict[str, object]:
    return {
        "source_code": "class Strategy: pass",
        "slug": "over25",
        "config": {"edge": 0.03},
        "token_ids": ("yes", "no"),
        "provider_dataset_ids": ("football-selected",),
        "market_data_view": _MARKET_DATA_VIEW,
        "projected_market": {
            "market_id": "market-real",
            "condition_id": "market-real",
            "slug": "contract-real",
            "title": "Home v Away: Over 2.5 total goals",
            "coin": None,
            "timeframe": "total_goals_over_under",
            "market_start": "2026-08-01T00:00:00+00:00",
            "market_close": "2026-08-02T00:00:00+00:00",
            "yes_token_id": "yes",
            "no_token_id": "no",
            "price_to_beat": None,
        },
        "start": "2026-08-01T00:00:00+00:00",
        "end": "2026-08-02T00:00:00+00:00",
        "initial_capital_usd": 1000.0,
        "submit_p50_ms": 50.0,
        "submit_p95_ms": 100.0,
        "cancel_p50_ms": 50.0,
        "cancel_p95_ms": 100.0,
        "seed": 7,
        "n_trials": 3,
    }


@pytest.mark.asyncio
async def test_gateway_maps_only_unified_backtest_fields_with_materialized_collections() -> None:
    runner = AsyncMock(return_value={"run_id": "hr-1", "execution": {"trade_count": 2}})
    config = FrozenConfig({"edge": 0.03, "levels": (1, FrozenConfig({"enabled": True}))})
    request = BacktestRequest(**(_request_kwargs() | {"config": config}))

    result = await HomerunBacktestGateway(runner=runner).run(request)

    assert result["run_id"] == "hr-1"
    runner.assert_awaited_once_with(
        source_code="class Strategy: pass",
        slug="over25",
        config={"edge": 0.03, "levels": [1, {"enabled": True}]},
        token_ids=["yes", "no"],
        provider_dataset_ids=["football-selected"],
        market_data_view=_MARKET_DATA_VIEW,
        start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        end=datetime(2026, 8, 2, tzinfo=timezone.utc),
        initial_capital_usd=1000.0,
        submit_p50_ms=50.0,
        submit_p95_ms=100.0,
        cancel_p50_ms=50.0,
        cancel_p95_ms=100.0,
        seed=7,
        n_trials=3,
        execution_mode="evosport_reproducible",
        reproducible_projected_market={
            "market_id": "market-real",
            "condition_id": "market-real",
            "slug": "contract-real",
            "title": "Home v Away: Over 2.5 total goals",
            "coin": None,
            "timeframe": "total_goals_over_under",
            "market_start": "2026-08-01T00:00:00+00:00",
            "market_close": "2026-08-02T00:00:00+00:00",
            "yes_token_id": "yes",
            "no_token_id": "no",
            "price_to_beat": None,
        },
    )
    passed_config = runner.await_args.kwargs["config"]
    assert passed_config is not config
    assert passed_config["levels"] is not config["levels"]


def test_request_normalizes_aware_iso_timestamps_to_utc() -> None:
    request = BacktestRequest(
        **(
            _request_kwargs()
            | {
                "start": "2026-08-01T08:00:00+08:00",
                "end": "2026-08-02T08:00:00+08:00",
            }
        )
    )

    assert request.start_datetime == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert request.end_datetime == datetime(2026, 8, 2, tzinfo=timezone.utc)
    assert request.start_datetime.tzinfo is timezone.utc
    assert request.end_datetime.tzinfo is timezone.utc


def test_request_normalizes_terminal_uppercase_z_before_fromisoformat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evosport.domain.time as time_boundary

    class Python310Datetime:
        @staticmethod
        def fromisoformat(value: str) -> datetime:
            assert not value.endswith("Z")
            return datetime.fromisoformat(value)

    monkeypatch.setattr(time_boundary, "datetime", Python310Datetime)

    request = BacktestRequest(
        **(
            _request_kwargs()
            | {
                "start": "2026-08-01T00:00:00Z",
                "end": "2026-08-02T00:00:00Z",
            }
        )
    )

    assert request.start_datetime == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert request.end_datetime == datetime(2026, 8, 2, tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start", "2026-08-01T00:00:00"),
        ("end", "not-an-iso-timestamp"),
        ("end", "2026-08-02T00:00:00"),
        ("end", "2026-08-01T00:00:00+00:00"),
        ("initial_capital_usd", -0.01),
        ("initial_capital_usd", 0.0),
        ("initial_capital_usd", float("inf")),
        ("initial_capital_usd", float("nan")),
        ("submit_p50_ms", -0.01),
        ("submit_p50_ms", float("inf")),
        ("submit_p95_ms", float("nan")),
        ("submit_p95_ms", 49.0),
        ("cancel_p50_ms", -0.01),
        ("cancel_p95_ms", float("inf")),
        ("cancel_p95_ms", 49.0),
        ("n_trials", 0),
    ],
)
async def test_invalid_request_never_invokes_runner(field: str, value: object) -> None:
    runner = AsyncMock()
    HomerunBacktestGateway(runner=runner)

    with pytest.raises(ValueError):
        BacktestRequest(**(_request_kwargs() | {field: value}))

    runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in (
            "initial_capital_usd",
            "submit_p50_ms",
            "submit_p95_ms",
            "cancel_p50_ms",
            "cancel_p95_ms",
        )
        for value in (True, "not-a-number")
    ]
    + [("seed", 1.5), ("seed", True), ("n_trials", 1.5), ("n_trials", True)],
)
async def test_invalid_runtime_scalar_never_invokes_runner(field: str, value: object) -> None:
    runner = AsyncMock()
    HomerunBacktestGateway(runner=runner)

    with pytest.raises(ValueError, match="must be a non-boolean"):
        BacktestRequest(**(_request_kwargs() | {field: value}))

    runner.assert_not_awaited()


def test_gateway_module_keeps_homerun_import_lazy_in_fresh_process() -> None:
    script = """
import builtins

original_import = builtins.__import__

def reject_homerun_import(name, *args, **kwargs):
    if name == "services.backtest.unified_runner":
        raise AssertionError("Homerun must not import while injecting a runner")
    return original_import(name, *args, **kwargs)

builtins.__import__ = reject_homerun_import

from evosport.experiments.gateway import HomerunBacktestGateway

async def runner(**kwargs):
    return {}

HomerunBacktestGateway(runner=runner)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
