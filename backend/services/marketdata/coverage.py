"""Unified parquet coverage resolution for the market-data layer.

Replaces the four copy-pasted ``ensure_recent_scan() + find_parquet_coverage()``
call sites and the SQL ``_measure_data_coverage`` density queries with one
resolver that returns a structured :class:`CoverageMap`.

Two correctness improvements over the legacy ``find_parquet_coverage`` (which
returned ``{token -> single file}``):

  * **All covering files per token.** A token whose history spans multiple
    window directories (common for the live sink's 15-min windows, and for
    polybacktest markets re-imported across windows) now resolves to every
    covering file, chained on read. The legacy one-file-per-token behaviour
    silently dropped coverage.
  * **Explicit covered/uncovered accounting.** Callers can see which requested
    tokens have no data, instead of a silently-missing dict key.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)


def _uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":  # file:///C:/...
        path = path[1:]
    return Path(path)


def _to_naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


@dataclass(frozen=True)
class TokenCoverage:
    """Per-token coverage: which canonical parquet files cover the window."""

    token_id: str
    files: tuple[str, ...] = ()
    dataset_ids: tuple[str, ...] = ()
    start_us: Optional[int] = None
    end_us: Optional[int] = None

    @property
    def covered(self) -> bool:
        return bool(self.files)


@dataclass(frozen=True)
class CoverageMap:
    """Resolved coverage for a set of requested tokens over a window."""

    by_token: dict[str, TokenCoverage] = field(default_factory=dict)
    requested: tuple[str, ...] = ()
    window_start_us: Optional[int] = None
    window_end_us: Optional[int] = None
    dataset_ids_by_path: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def covered_tokens(self) -> tuple[str, ...]:
        return tuple(t for t in self.requested if self.by_token.get(t, TokenCoverage(t)).covered)

    @property
    def uncovered_tokens(self) -> tuple[str, ...]:
        return tuple(t for t in self.requested if not self.by_token.get(t, TokenCoverage(t)).covered)

    @property
    def coverage_fraction(self) -> float:
        if not self.requested:
            return 0.0
        return len(self.covered_tokens) / len(self.requested)

    def files_for(self, token_id: str) -> tuple[str, ...]:
        tc = self.by_token.get(str(token_id))
        return tc.files if tc else ()

    def all_files(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for tc in self.by_token.values():
            for f in tc.files:
                seen.setdefault(f, None)
        return tuple(seen.keys())

    def as_per_token_files(self) -> dict[str, list[str]]:
        """Adapter to the legacy ``{token -> [paths]}`` shape some callers want."""
        return {t: list(tc.files) for t, tc in self.by_token.items() if tc.files}


async def resolve_coverage(
    *,
    token_ids: Iterable[str],
    start: datetime,
    end: datetime,
    providers: Optional[Iterable[str]] = None,
    dataset_ids: Optional[Iterable[str]] = None,
    ensure_scan: bool = True,
) -> CoverageMap:
    """Resolve which canonical parquet files cover each requested token in
    ``[start, end]``. Optionally restricts to ``providers`` (e.g. only
    ``polybacktest``); by default considers every parquet-backed dataset.
    """
    from sqlalchemy import select
    from models.database import AsyncSessionLocal, ProviderDataset
    from services.external_data.parquet_schema import _safe_segment, bundle_path_for

    requested = tuple(str(t) for t in token_ids if t)
    start_us = int(start.astimezone(timezone.utc).timestamp() * 1_000_000) if start.tzinfo else int(start.timestamp() * 1_000_000)
    end_us = int(end.astimezone(timezone.utc).timestamp() * 1_000_000) if end.tzinfo else int(end.timestamp() * 1_000_000)
    if not requested:
        return CoverageMap(by_token={}, requested=(), window_start_us=start_us, window_end_us=end_us)

    if ensure_scan:
        # Pick up newly-dropped files.  30-min staleness with the cross-
        # process scan stamp — the old 60s cache re-walked the entire store
        # (hundreds of thousands of file stats) on effectively every call.
        try:
            from services.external_data import parquet_scanner as _scan
            await _scan.ensure_recent_scan(max_age_seconds=1800.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("resolve_coverage: ensure_recent_scan skipped: %s", exc)

    requested_set = set(requested)
    provider_list = list(providers) if providers is not None else None
    if dataset_ids is not None:
        dataset_id_list = tuple(dict.fromkeys(str(value) for value in dataset_ids if value))
        if not dataset_id_list:
            raise ValueError("dataset_ids cannot be empty when explicit selection is requested")
    else:
        dataset_id_list = ()
    start_naive, end_naive = _to_naive_utc(start), _to_naive_utc(end)

    async with AsyncSessionLocal() as session:
        stmt = select(ProviderDataset)
        if dataset_id_list:
            stmt = stmt.where(ProviderDataset.id.in_(dataset_id_list))
        else:
            stmt = stmt.where(
                ProviderDataset.storage_type == "parquet",
                ProviderDataset.start_ts <= end_naive,
                ProviderDataset.end_ts >= start_naive,
            )
        if provider_list is not None:
            stmt = stmt.where(ProviderDataset.provider.in_(provider_list))
        rows = (await session.execute(stmt)).scalars().all()

    if dataset_id_list:
        found_ids = {str(row.id) for row in rows}
        missing_ids = set(dataset_id_list) - found_ids
        if missing_ids:
            raise ValueError(f"unknown provider dataset IDs: {sorted(missing_ids)}")
        selected_tokens = {str(token) for row in rows for token in (row.token_ids_json or ())}
        if not requested_set.issubset(selected_tokens):
            raise ValueError(
                "selected dataset token mismatch: "
                f"requested={sorted(requested_set)} selected={sorted(selected_tokens)}"
            )
        rows = [
            row
            for row in rows
            if row.storage_type == "parquet"
            and row.start_ts is not None
            and row.end_ts is not None
            and _to_naive_utc(row.start_ts) <= end_naive
            and _to_naive_utc(row.end_ts) >= start_naive
        ]

    # Accumulate files per token across all overlapping datasets.
    files_by_token: dict[str, list[str]] = {}
    datasets_by_token: dict[str, list[str]] = {}
    datasets_by_path: dict[str, set[str]] = {}
    span_by_token: dict[str, tuple[Optional[int], Optional[int]]] = {}
    intervals_by_token: dict[str, list[tuple[int, int]]] = {}

    for r in rows:
        tokens_here = requested_set & set(r.token_ids_json or [])
        if not tokens_here:
            continue
        if not r.storage_uri or not r.storage_uri.startswith("file://"):
            continue
        try:
            window_dir = _uri_to_path(r.storage_uri)
        except Exception:
            continue
        ds_start_us = int(r.start_ts.replace(tzinfo=timezone.utc).timestamp() * 1_000_000) if r.start_ts else None
        ds_end_us = int(r.end_ts.replace(tzinfo=timezone.utc).timestamp() * 1_000_000) if r.end_ts else None
        for tok in tokens_here:
            resolved: Optional[str] = None
            bundle = bundle_path_for(window_dir, "snapshots")
            candidates = (
                (bundle,)
                if bundle.exists()
                else (
                    window_dir / f"snapshots__{_safe_segment(tok)}.parquet",
                    window_dir / f"snapshots__{tok}.parquet",
                )
            )
            for cand in candidates:
                if cand.exists():
                    resolved = str(cand)
                    break
            if resolved is None:
                continue
            flist = files_by_token.setdefault(tok, [])
            if resolved not in flist:
                flist.append(resolved)
            datasets_by_token.setdefault(tok, []).append(str(r.id))
            datasets_by_path.setdefault(resolved, set()).add(str(r.id))
            if ds_start_us is not None and ds_end_us is not None:
                intervals_by_token.setdefault(tok, []).append((ds_start_us, ds_end_us))
            cur = span_by_token.get(tok)
            lo = ds_start_us if cur is None else (min(cur[0], ds_start_us) if (cur[0] is not None and ds_start_us is not None) else (cur[0] or ds_start_us))
            hi = ds_end_us if cur is None else (max(cur[1], ds_end_us) if (cur[1] is not None and ds_end_us is not None) else (cur[1] or ds_end_us))
            span_by_token[tok] = (lo, hi)

    if dataset_id_list:
        insufficient: list[str] = []
        for tok in requested:
            intervals = sorted(intervals_by_token.get(tok, ()))
            cursor = start_us
            for interval_start, interval_end in intervals:
                if interval_end < cursor:
                    continue
                if interval_start > cursor:
                    break
                cursor = max(cursor, interval_end)
                if cursor >= end_us:
                    break
            if cursor < end_us:
                insufficient.append(tok)
        if insufficient:
            raise ValueError(
                f"selected datasets do not fully cover requested window for tokens: {insufficient}"
            )

    by_token: dict[str, TokenCoverage] = {}
    for tok in requested:
        files = tuple(sorted(files_by_token.get(tok, [])))
        span = span_by_token.get(tok, (None, None))
        by_token[tok] = TokenCoverage(
            token_id=tok,
            files=files,
            dataset_ids=tuple(dict.fromkeys(datasets_by_token.get(tok, []))),
            start_us=span[0],
            end_us=span[1],
        )

    return CoverageMap(
        by_token=by_token,
        requested=requested,
        window_start_us=start_us,
        window_end_us=end_us,
        dataset_ids_by_path={
            path: tuple(sorted(dataset_ids))
            for path, dataset_ids in sorted(datasets_by_path.items())
        },
    )


__all__ = ["TokenCoverage", "CoverageMap", "resolve_coverage"]
