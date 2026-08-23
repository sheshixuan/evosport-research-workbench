import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.trader_orchestrator.order_manager import _check_slippage_bps


RISK = {"slippage_bps": 35.0}


def test_buy_favorable_move_never_rejected():
    # 2026-07-03 Valorant incident: hedge NO leg planned at 0.22, live 0.21
    # (cheaper = favorable). The old symmetric check rejected at 455 bps and
    # stranded the filled YES leg naked.
    rejected, drift_bps, cap = _check_slippage_bps(
        signal_price=0.22,
        intended_price=0.21,
        risk_limits=RISK,
        side="buy",
    )
    assert rejected is False
    assert drift_bps == 0.0
    assert cap == 35.0


def test_buy_adverse_move_beyond_cap_rejected():
    rejected, drift_bps, _ = _check_slippage_bps(
        signal_price=0.22,
        intended_price=0.23,
        risk_limits=RISK,
        side="buy",
    )
    assert rejected is True
    assert drift_bps > 35.0


def test_sell_favorable_move_never_rejected():
    rejected, drift_bps, _ = _check_slippage_bps(
        signal_price=0.50,
        intended_price=0.55,
        risk_limits=RISK,
        side="sell",
    )
    assert rejected is False
    assert drift_bps == 0.0


def test_sell_adverse_move_beyond_cap_rejected():
    rejected, drift_bps, _ = _check_slippage_bps(
        signal_price=0.50,
        intended_price=0.49,
        risk_limits=RISK,
        side="sell",
    )
    assert rejected is True
    assert drift_bps > 35.0


def test_small_adverse_move_within_cap_passes():
    rejected, drift_bps, _ = _check_slippage_bps(
        signal_price=0.6600,
        intended_price=0.6601,
        risk_limits=RISK,
        side="buy",
    )
    assert rejected is False
    assert drift_bps is not None and drift_bps < 35.0


def test_noop_without_cap_or_reference():
    assert _check_slippage_bps(
        signal_price=None, intended_price=0.5, risk_limits=RISK, side="buy"
    ) == (False, None, 35.0)
    assert _check_slippage_bps(
        signal_price=0.5, intended_price=0.5, risk_limits={}, side="buy"
    ) == (False, None, None)
