#!/usr/bin/env python3
"""Seed the Homerun market catalog file with real, active, liquid markets.

The recording plane's proactive recorder-subscription loop reads the local
market-catalog payload file (backend/data/cache/market_catalog_latest.json)
and subscribes the recording pool to the top-liquidity tokens.  This script
fetches currently-active, liquid Polymarket markets from Gamma and writes
that file so the recorder has real tokens to capture — without requiring the
heavy full-catalog scanner/reconcile to run first.

Usage (from backend/):
  venv/bin/python scripts/seed_market_catalog.py [COUNT]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "cache" / "market_catalog_latest.json"


def _fetch_markets(limit: int = 40) -> list[dict]:
    url = (
        "https://gamma-api.polymarket.com/markets"
        f"?limit={limit}&active=true&closed=false&order=volume24hr&ascending=false"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 evosport-seed"})
    with urllib.request.urlopen(request, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _clean_market(m: dict) -> dict | None:
    """Normalise a Gamma market dict into the catalog file's market shape."""
    raw_tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or []
    if isinstance(raw_tokens, str):
        try:
            raw_tokens = json.loads(raw_tokens)
        except (json.JSONDecodeError, TypeError):
            raw_tokens = []
    raw_tokens = [t for t in raw_tokens if t] or []
    if not raw_tokens:
        return None
    try:
        liquidity = float(m.get("liquidity") or m.get("liquidityNum") or 0.0)
    except (TypeError, ValueError):
        liquidity = 0.0
    active = bool(m.get("active", True)) and not bool(m.get("closed", False))
    return {
        "id": m.get("id"),
        "question": m.get("question"),
        "slug": m.get("slug") or m.get("ticker"),
        "condition_id": m.get("conditionId"),
        "clob_token_ids": raw_tokens,
        "outcomes": m.get("outcomes") or ["Yes", "No"],
        "active": active,
        "closed": bool(m.get("closed", False)),
        "accepting_orders": bool(m.get("acceptingOrders", True)),
        "liquidity": liquidity,
        "volume24hr": m.get("volume24hr"),
    }


def main(count: int) -> int:
    raw = _fetch_markets(limit=max(count * 2, 20))
    markets = [cm for cm in (_clean_market(m) for m in raw) if cm and cm["active"]]
    # de-dupe by token set; keep highest liquidity
    seen: dict[frozenset, dict] = {}
    for m in markets:
        key = frozenset(m["clob_token_ids"])
        if key not in seen or m["liquidity"] > seen[key]["liquidity"]:
            seen[key] = m
    markets = [seen[k] for k in seen][:count]
    markets.sort(key=lambda m: m["liquidity"], reverse=True)

    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_count": 0,
        "market_count": len(markets),
        "fetch_duration_seconds": 0.0,
        "error": None,
        "events": [],
        "markets": markets,
    }
    _CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CATALOG_PATH.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(markets)} markets to {_CATALOG_PATH}")
    total_tokens = sum(len(m["clob_token_ids"]) for m in markets)
    print(f"Total tokens: {total_tokens}")
    for m in markets[:6]:
        print(
            f"  liq={m['liquidity']:.0f} | {m['question'][:50]!r} | "
            f"tokens={len(m['clob_token_ids'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 5))
