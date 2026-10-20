"""Point-in-time store: record what the bot saw each day, replay it in backtests.

Free data sources only expose *today's* P/E, news and ratings. Each live run
appends a snapshot per ticker to ``data/pit/<kind>/<TICKER>.jsonl``; after a
few weeks of runs, backtests over that period use exactly the values the bot
had on each day.

:class:`PointInTimeProvider` wraps any provider: live calls pass straight
through, and ``*_at`` calls check the store first, then the wrapped provider
(e.g. Yahoo's reconstructed fundamentals and dated analyst history).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .market_data import AnalystActions, Fundamentals, MarketDataProvider

log = logging.getLogger(__name__)

FUNDAMENTALS_MAX_AGE = timedelta(days=120)  # ignore snapshots older than about a quarter


class PointInTimeStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self._memo: dict[tuple[str, str], list[tuple[date, dict[str, Any]]]] = {}

    def _path(self, kind: str, ticker: str) -> Path:
        return self.dir / kind / f"{ticker.upper()}.jsonl"

    def record(self, kind: str, ticker: str, day: date, payload: dict[str, Any]) -> None:
        path = self._path(kind, ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"date": day.isoformat(), **payload}, default=str) + "\n")
        self._memo.pop((kind, ticker.upper()), None)

    def entries(self, kind: str, ticker: str) -> list[tuple[date, dict[str, Any]]]:
        key = (kind, ticker.upper())
        if key not in self._memo:
            by_day: dict[date, dict[str, Any]] = {}
            path = self._path(kind, ticker)
            if path.exists():
                for line in path.read_text().splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    by_day[date.fromisoformat(row.pop("date"))] = row  # last write per day wins
            self._memo[key] = sorted(by_day.items())
        return self._memo[key]

    def latest(self, kind: str, ticker: str, as_of: date) -> tuple[date, dict[str, Any]] | None:
        rows = [r for r in self.entries(kind, ticker) if r[0] <= as_of]
        return rows[-1] if rows else None

    def between(self, kind: str, ticker: str, start: date, end: date) -> list[tuple[date, dict[str, Any]]]:
        return [r for r in self.entries(kind, ticker) if start <= r[0] <= end]

    def days(self, kind: str) -> set[date]:
        folder = self.dir / kind
        return {d for p in folder.glob("*.jsonl") for d, _ in self.entries(kind, p.stem)} if folder.exists() else set()


def record_snapshot(store: PointInTimeStore, provider: MarketDataProvider, tickers: list[str], day: date) -> int:
    """Save today's fundamentals and headlines for ``tickers``. Returns rows written."""
    written = 0
    for t in tickers:
        try:
            f = provider.fundamentals(t)
            if f is not None:
                store.record("fundamentals", t, day, asdict(f))
                written += 1
            headlines = provider.news_headlines(t)
            if headlines:
                store.record("news", t, day, {"headlines": headlines})
                written += 1
        except Exception as exc:  # recording must never break a run
            log.debug("point-in-time snapshot for %s failed: %s", t, exc)
    return written


class PointInTimeProvider(MarketDataProvider):
    def __init__(self, base: MarketDataProvider, store: PointInTimeStore):
        self.base = base
        self.store = store

    # -- pass-through -------------------------------------------------------
    def history(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        return self.base.history(ticker, start, end)

    def latest_price(self, ticker: str) -> float | None:
        return self.base.latest_price(ticker)

    def exchange(self, ticker: str) -> str | None:
        return self.base.exchange(ticker)

    def fundamentals(self, ticker: str) -> Fundamentals | None:
        return self.base.fundamentals(ticker)

    def analyst_actions(self, ticker: str, since: date) -> AnalystActions | None:
        return self.base.analyst_actions(ticker, since)

    def news_headlines(self, ticker: str) -> list[str] | None:
        return self.base.news_headlines(ticker)

    def close_at(self, ticker: str, as_of: date) -> float | None:
        return self.base.close_at(ticker, as_of)

    # -- point in time ------------------------------------------------------
    def fundamentals_at(self, ticker: str, as_of: date) -> Fundamentals | None:
        hit = self.store.latest("fundamentals", ticker, as_of)
        if hit is not None and as_of - hit[0] <= FUNDAMENTALS_MAX_AGE:
            known = {k: v for k, v in hit[1].items() if k in Fundamentals.__dataclass_fields__}
            return Fundamentals(**known)
        return self.base.fundamentals_at(ticker, as_of)

    def analyst_actions_at(self, ticker: str, start: date, end: date) -> AnalystActions | None:
        return self.base.analyst_actions_at(ticker, start, end)

    def news_at(self, ticker: str, start: date, end: date) -> list[str] | None:
        rows = self.store.between("news", ticker, start, end)
        if rows:
            seen: dict[str, None] = {}
            for _, row in rows:
                for h in row.get("headlines", []):
                    seen.setdefault(h, None)
            return list(seen)
        return self.base.news_at(ticker, start, end)
