#!/usr/bin/env python3
"""Seed the Homerun market catalog with real, active FOOTBALL (soccer) markets.

Polymarket's active-market universe is dominated by politics / esports, so the
generic seed (seed_market_catalog.py) rarely surfaces soccer.  This script
pages the /markets/keyset endpoint (closed=false, ranked by 24h volume) and
keeps ONLY single-match football O/U markets (question names teams + O/U,
description names a football league), then writes the repository market-catalog
payload file the recording plane reads on its next subscription tick.

Usage (from backend/):
  venv/bin/python scripts/seed_football_catalog.py [COUNT]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "cache" / "market_catalog_latest.json"

_LEAGUE_MARKERS = (
    "j. league", "jleague", "la liga", "premier league", "serie a", "bundesliga",
    "ligue 1", "eredivisie", "champions league", "world cup", "europa league",
    "confederation", "fa cup", "copa", "soccer", "football",
)
_FOOTBALL_CLUB_MARKERS = (
    "tokyo", "jef united", "kashiwa", "v-varen", "nagasaki", "betis",
    "sociedad", "real madrid", "barcelona", "man city", "manchester",
    "liverpool", "arsenal", "chelsea", "bayern", "juventus", "inter ",
    "milan", "psg", "dortmund", "atletico", "seville",
)


def _page(cursor: str | None = None) -> dict:
    params = {
        "closed": "false",
        "limit": "200",
        "order": "volume24hr",
        "ascending": "false",
    }
    if cursor:
        params["after_cursor"] = cursor
    url = "https://gamma-api.polymarket.com/markets/keyset?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 evosport-football"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_football(market: dict) -> bool:
    question = str(market.get("question") or "")
    ql = question.lower()
    description = str(market.get("description") or "").lower()
    if "vs." not in ql and " vs " not in ql:
        return False
    if not any(t in ql for t in ("o/u", "over", "under", "total")):
        return False
    # Exclude esports / NFL / politics: require a football-league or club marker.
    if any(t in description for t in _LEAGUE_MARKERS):
        return True
    if any(t in ql for t in _FOOTBALL_CLUB_MARKERS):
        return True
    return False


def _clean(market: dict) -> dict | None:
    raw_tokens = market.get("clobTokenIds") or market.get("clob_token_ids") or []
    if isinstance(raw_tokens, str):
        try:
            raw_tokens = json.loads(raw_tokens)
        except (json.JSONDecodeError, TypeError):
            raw_tokens = []
    tokens = [str(t) for t in raw_tokens if str(t or "").strip()]
    if not tokens:
        return None
    try:
        liquidity = float(market.get("liquidity") or 0.0)
    except (TypeError, ValueError):
        liquidity = 0.0
    return {
        "id": market.get("id"),
        "question": market.get("question"),
        "slug": market.get("slug") or market.get("ticker"),
        "condition_id": market.get("conditionId"),
        "clob_token_ids": tokens,
        "outcomes": market.get("outcomes") or ["Yes", "No"],
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "liquidity": liquidity,
        "volume24hr": market.get("volume24hr"),
        "startDate": market.get("startDate"),
        "endDate": market.get("endDate"),
    }


def main(count: int) -> int:
    markets: dict[str, dict] = {}
    cursor: str | None = None
    for _ in range(12):  # up to ~2400 markets — enough to surface soccer
        data = _page(cursor)
        for m in data.get("markets", []):
            if not _is_football(m):
                continue
            clean = _clean(m)
            if not clean:
                continue
            key = "|".join(sorted(clean["clob_token_ids"]))
            if key not in markets or clean["liquidity"] > markets[key]["liquidity"]:
                markets[key] = clean
        cursor = data.get("next_cursor")
        if not cursor:
            break
        time.sleep(0.25)

    picks = sorted(markets.values(), key=lambda m: m["liquidity"], reverse=True)[:count]
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_count": 0,
        "market_count": len(picks),
        "fetch_duration_seconds": 0.0,
        "error": None,
        "events": [],
        "markets": picks,
    }
    _CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CATALOG_PATH.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="utf-8"
    )
    print(f"Wrote {len(picks)} FOOTBALL markets to {_CATALOG_PATH}")
    for m in picks:
        print(
            f"  liq={m['liquidity']:.0f} | {m['question'][:58]!r} | "
            f"tokens={len(m['clob_token_ids'])} | end={str(m.get('endDate'))[:16]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 6))