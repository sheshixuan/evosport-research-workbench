from __future__ import annotations

from typing import Any

from utils.converters import safe_float, safe_int


SUPPORTED_EXECUTION_POLICIES = {
    "SINGLE_LEG",
    "PARALLEL_MAKER",
    "SEQUENTIAL_HEDGE",
    "REPRICE_LOOP",
    "TIMEBOX_EXIT",
    "PAIR_LOCK",
}

# Venue floor: Polymarket CLOB rejects orders below 5 shares, and CTF
# split/merge legs inherit the same floor so the bundle stays atomic.
MIN_BUNDLE_EXECUTION_SHARES = 5.0


def signal_payload(signal: Any) -> dict[str, Any]:
    payload = getattr(signal, "payload_json", None)
    return payload if isinstance(payload, dict) else {}


def required_roster_market_ids(signal: Any) -> list[str]:
    payload = signal_payload(signal)
    roster = payload.get("market_roster")
    if not isinstance(roster, dict):
        return []
    if str(roster.get("scope") or "").strip().lower() != "event":
        return []

    required_ids: list[str] = []
    for market in roster.get("markets") or []:
        if not isinstance(market, dict):
            continue
        market_id = str(market.get("id") or market.get("market_id") or "").strip()
        if market_id and market_id not in required_ids:
            required_ids.append(market_id)
    return required_ids


def selected_market_ids(legs: list[dict[str, Any]]) -> list[str]:
    selected_ids: list[str] = []
    for leg in legs:
        market_id = str(leg.get("market_id") or "").strip()
        if market_id and market_id not in selected_ids:
            selected_ids.append(market_id)
    return selected_ids


def requires_full_bundle_execution(signal: Any, legs: list[dict[str, Any]]) -> bool:
    if len(legs) < 2:
        return False
    payload = signal_payload(signal)
    execution_plan = payload.get("execution_plan")
    execution_plan = execution_plan if isinstance(execution_plan, dict) else {}
    metadata = execution_plan.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    market_coverage = metadata.get("market_coverage")
    market_coverage = market_coverage if isinstance(market_coverage, dict) else {}
    # Generic atomic-bundle signals (replacing the per-strategy slug hardcode):
    #   * a strategy that declares requires_atomic_execution, or
    #   * a guaranteed event-internal arb (requires_full_market_coverage).
    if bool(metadata.get("requires_atomic_bundle")):
        return True
    if bool(market_coverage.get("requires_full_market_coverage")):
        return True
    if not bool(payload.get("is_guaranteed")):
        return False
    if len(selected_market_ids(legs)) == 1:
        return True
    roster = payload.get("market_roster")
    if isinstance(roster, dict) and str(roster.get("scope") or "").strip().lower() == "event":
        return len(required_roster_market_ids(signal)) > 1
    return False


def bundle_minimum_executable_notional_usd(
    legs: list[dict[str, Any]],
    *,
    min_order_size_usd: float,
    min_shares: float = MIN_BUNDLE_EXECUTION_SHARES,
) -> float | None:
    """Smallest total bundle notional at which every leg clears the venue
    minimums (``min_shares`` shares and ``min_order_size_usd`` notional),
    under the same weight allocation as :func:`allocate_leg_notionals`.

    Returns ``None`` when the minimum cannot be derived (no legs, or a leg
    without a positive limit price) — callers must then leave sizing alone
    and let the execution-side preflight decide.
    """
    if not legs:
        return None

    weights: list[float] = [
        max(0.0001, safe_float(leg.get("notional_weight"), 1.0)) for leg in legs
    ]
    total_weight = sum(weights)
    if total_weight <= 0:
        return None

    min_order = max(0.0, safe_float(min_order_size_usd, 0.0))
    floor_shares = max(0.0, safe_float(min_shares, 0.0))
    minimum_total = 0.0
    for leg, weight in zip(legs, weights):
        limit_price = safe_float(leg.get("limit_price"), 0.0)
        if limit_price <= 0.0:
            return None
        # notional_i = S * w_i / W  and  shares_i = notional_i / p_i
        if min_order > 0.0:
            minimum_total = max(minimum_total, min_order * total_weight / weight)
        if floor_shares > 0.0:
            minimum_total = max(
                minimum_total, floor_shares * limit_price * total_weight / weight
            )
    return minimum_total if minimum_total > 0.0 else None


def normalize_execution_policy(value: Any, *, legs_count: int) -> str:
    policy = str(value or "").strip().upper()
    if not policy:
        return "PARALLEL_MAKER" if legs_count > 1 else "SINGLE_LEG"
    if policy in SUPPORTED_EXECUTION_POLICIES:
        return policy
    return "PARALLEL_MAKER" if legs_count > 1 else "SINGLE_LEG"


def normalize_execution_constraints(raw: Any) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    return {
        "max_unhedged_notional_usd": max(0.0, safe_float(payload.get("max_unhedged_notional_usd"), 0.0)),
        "hedge_timeout_seconds": max(1, safe_int(payload.get("hedge_timeout_seconds"), 20)),
        "session_timeout_seconds": max(1, safe_int(payload.get("session_timeout_seconds"), 300)),
        "max_reprice_attempts": max(0, safe_int(payload.get("max_reprice_attempts"), 3)),
        "pair_lock": bool(payload.get("pair_lock", True)),
        "leg_fill_tolerance_ratio": max(
            0.0,
            min(1.0, safe_float(payload.get("leg_fill_tolerance_ratio"), 0.02)),
        ),
    }


def allocate_leg_notionals(total_notional_usd: float, legs: list[dict[str, Any]]) -> list[float]:
    notional = max(0.0, safe_float(total_notional_usd, 0.0))
    if not legs:
        return []

    weights: list[float] = []
    for leg in legs:
        weight = max(0.0001, safe_float(leg.get("notional_weight"), 1.0))
        weights.append(weight)

    total_weight = sum(weights) if weights else 0.0
    if total_weight <= 0:
        equal = notional / len(legs)
        return [equal for _ in legs]

    notionals = [(weight / total_weight) * notional for weight in weights]
    if notionals:
        drift = notional - sum(notionals)
        notionals[-1] += drift
    return notionals


def execution_waves(policy: str, legs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    normalized = normalize_execution_policy(policy, legs_count=len(legs))
    if normalized == "SEQUENTIAL_HEDGE":
        return [[leg] for leg in legs]
    return [list(legs)]


def requires_pair_lock(policy: str, constraints: dict[str, Any]) -> bool:
    normalized = normalize_execution_policy(policy, legs_count=2)
    if normalized == "PAIR_LOCK":
        return True
    return bool(constraints.get("pair_lock", False))


def supports_reprice(policy: str) -> bool:
    normalized = normalize_execution_policy(policy, legs_count=2)
    return normalized in {"REPRICE_LOOP", "PARALLEL_MAKER", "SEQUENTIAL_HEDGE"}


def reprice_limit_price(base_price: float | None, side: str, attempt: int) -> float | None:
    if base_price is None:
        return None
    ticks = max(1, int(attempt))
    direction = 1.0 if str(side or "").strip().lower() == "buy" else -1.0
    adjusted = float(base_price) + (0.01 * ticks * direction)
    return max(0.01, min(0.99, round(adjusted, 4)))
