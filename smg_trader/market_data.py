"""Market data providers.

Strategies never talk to a data vendor directly. They get a
:class:`MarketDataProvider`, so the same strategy code runs against live Yahoo
Finance data, CSV files (for backtests / exported SMG data) or in-memory frames
(for tests).

Price history frames use a ``DatetimeIndex`` (tz-naive, daily) and the columns
``Open, High, Low, Close, Volume``.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# Yahoo exchange codes -> the exchange names SMG rules care about.
EXCHANGE_CODES = {
    "NYQ": "NYSE", "NYSE": "NYSE", "NYS": "NYSE",
    "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NAS": "NASDAQ",
    "NASDAQ": "NASDAQ", "NASDAQGS": "NASDAQ", "NASDAQGM": "NASDAQ", "NASDAQCM": "NASDAQ",
    "ASE": "NYSE AMERICAN", "AMEX": "NYSE AMERICAN",
    "PCX": "NYSE ARCA", "ARCA": "NYSE ARCA",
    "PNK": "OTC", "OTC": "OTC",
}

# GICS-ish sector name -> SPDR sector ETF, used by the momentum "sector trend" source.
SECTOR_ETFS = {
    "Technology": "XLK", "Information Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY", "Consumer Discretionary": "XLY",
    "Consumer Defensive": "XLP", "Consumer Staples": "XLP",
    "Energy": "XLE", "Financial Services": "XLF", "Financials": "XLF",
    "Healthcare": "XLV", "Health Care": "XLV",
    "Industrials": "XLI", "Basic Materials": "XLB", "Materials": "XLB",
    "Real Estate": "XLRE", "Utilities": "XLU",
}


def normalize_exchange(code: str | None) -> str | None:
    if not code:
        return None
    return EXCHANGE_CODES.get(code.strip().upper().replace(" ", ""), code.strip().upper())


@dataclass
class Fundamentals:
    pe_ratio: float | None = None
    forward_pe: float | None = None
    eps_growth: float | None = None       # fraction, 0.12 == 12%
    dividend_yield: float | None = None   # fraction, 0.03 == 3%
    sector: str | None = None
    exchange: str | None = None


@dataclass
class AnalystActions:
    upgrades: int = 0
    downgrades: int = 0
    details: list[str] = field(default_factory=list)


class MarketDataProvider(ABC):
    """Interface every data source implements."""

    @abstractmethod
    def history(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """Daily OHLCV for ``start <= day <= end`` (may be empty)."""

    def latest_price(self, ticker: str) -> float | None:
        end = date.today()
        df = self.history(ticker, end - timedelta(days=10), end)
        return float(df["Close"].iloc[-1]) if not df.empty else None

    def exchange(self, ticker: str) -> str | None:
        f = self.fundamentals(ticker)
        return f.exchange if f else None

    def fundamentals(self, ticker: str) -> Fundamentals | None:
        return None

    def analyst_actions(self, ticker: str, since: date) -> AnalystActions | None:
        return None

    def news_headlines(self, ticker: str) -> list[str] | None:
        return None


def _clean_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=PRICE_COLUMNS, index=pd.DatetimeIndex([]))
    df = df.copy()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[[c for c in PRICE_COLUMNS if c in df.columns]]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["Close"])


class InMemoryProvider(MarketDataProvider):
    """Provider backed by dicts of DataFrames. Used by tests and the backtester."""

    def __init__(
        self,
        prices: dict[str, pd.DataFrame],
        fundamentals: dict[str, Fundamentals] | None = None,
        analyst: dict[str, AnalystActions] | None = None,
        news: dict[str, list[str]] | None = None,
    ):
        self.prices = {k.upper(): _clean_history(v) for k, v in prices.items()}
        self._fundamentals = {k.upper(): v for k, v in (fundamentals or {}).items()}
        self._analyst = {k.upper(): v for k, v in (analyst or {}).items()}
        self._news = {k.upper(): v for k, v in (news or {}).items()}

    def history(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        df = self.prices.get(ticker.upper())
        if df is None:
            return _clean_history(pd.DataFrame())
        return df.loc[pd.Timestamp(start): pd.Timestamp(end)]

    def latest_price(self, ticker: str) -> float | None:
        df = self.prices.get(ticker.upper())
        return float(df["Close"].iloc[-1]) if df is not None and not df.empty else None

    def fundamentals(self, ticker: str) -> Fundamentals | None:
        return self._fundamentals.get(ticker.upper())

    def analyst_actions(self, ticker: str, since: date) -> AnalystActions | None:
        return self._analyst.get(ticker.upper())

    def news_headlines(self, ticker: str) -> list[str] | None:
        return self._news.get(ticker.upper())


class CSVProvider(InMemoryProvider):
    """Loads ``<dir>/<TICKER>.csv`` (Date,Open,High,Low,Close,Volume) and an
    optional ``<dir>/fundamentals.json`` ({"AAPL": {"pe_ratio": 28, ...}}).

    Use this for backtesting on exported / historical data without network access.
    """

    def __init__(self, directory: str | Path):
        directory = Path(directory)
        prices = {}
        for path in sorted(directory.glob("*.csv")):
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            df.columns = [c.strip().title() for c in df.columns]
            prices[path.stem.upper()] = df
        fundamentals = {}
        fpath = directory / "fundamentals.json"
        if fpath.exists():
            for ticker, values in json.loads(fpath.read_text()).items():
                fundamentals[ticker] = Fundamentals(**values)
        super().__init__(prices, fundamentals)


class YFinanceProvider(MarketDataProvider):
    """Live data from Yahoo Finance via ``yfinance`` (free, no API key).

    Price history is cached per ticker for the life of the provider so one
    pipeline run downloads each symbol at most once.
    """

    def __init__(self) -> None:
        import yfinance  # noqa: F401  (fail early with a clear ImportError)

        # ticker -> (fetched_start, fetched_end, frame)
        self._history: dict[str, tuple[date, date, pd.DataFrame]] = {}
        self._info: dict[str, dict[str, Any]] = {}

    def _ticker(self, ticker: str):
        import yfinance as yf

        return yf.Ticker(ticker)

    def history(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        key = ticker.upper()
        cached = self._history.get(key)
        if cached is None or start < cached[0] or end > cached[1]:
            lo = min(start, cached[0]) if cached else start
            hi = max(end, cached[1]) if cached else end
            try:
                raw = self._ticker(key).history(start=lo, end=hi + timedelta(days=1), auto_adjust=True)
            except Exception as exc:  # network / symbol errors
                log.warning("history(%s) failed: %s", key, exc)
                raw = pd.DataFrame()
            cached = (lo, hi, _clean_history(raw))
            self._history[key] = cached
        return cached[2].loc[pd.Timestamp(start): pd.Timestamp(end)]

    def _get_info(self, ticker: str) -> dict[str, Any]:
        key = ticker.upper()
        if key not in self._info:
            try:
                self._info[key] = self._ticker(key).info or {}
            except Exception as exc:
                log.warning("info(%s) failed: %s", key, exc)
                self._info[key] = {}
        return self._info[key]

    def latest_price(self, ticker: str) -> float | None:
        info = self._get_info(ticker)
        for k in ("regularMarketPrice", "currentPrice", "previousClose"):
            if info.get(k):
                return float(info[k])
        return super().latest_price(ticker)

    def fundamentals(self, ticker: str) -> Fundamentals | None:
        info = self._get_info(ticker)
        if not info:
            return None
        div = info.get("trailingAnnualDividendYield")
        if div is None and info.get("dividendRate") and info.get("currentPrice"):
            div = info["dividendRate"] / info["currentPrice"]
        return Fundamentals(
            pe_ratio=info.get("trailingPE"),
            forward_pe=info.get("forwardPE"),
            eps_growth=info.get("earningsGrowth"),
            dividend_yield=div if div is not None else 0.0,
            sector=info.get("sector"),
            exchange=normalize_exchange(info.get("exchange")),
        )

    def analyst_actions(self, ticker: str, since: date) -> AnalystActions | None:
        try:
            df = self._ticker(ticker).upgrades_downgrades
        except Exception as exc:
            log.debug("upgrades_downgrades(%s) failed: %s", ticker, exc)
            return None
        if df is None or df.empty:
            return AnalystActions()
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        recent = df[idx >= pd.Timestamp(since)]
        actions = recent.get("Action", pd.Series(dtype=str)).astype(str).str.lower()
        out = AnalystActions(
            upgrades=int((actions == "up").sum()),
            downgrades=int((actions == "down").sum()),
        )
        for _, row in recent.head(5).iterrows():
            out.details.append(f"{row.get('Firm', '?')}: {row.get('FromGrade', '')}->{row.get('ToGrade', '')}")
        return out

    def news_headlines(self, ticker: str) -> list[str] | None:
        try:
            items = self._ticker(ticker).news or []
        except Exception as exc:
            log.debug("news(%s) failed: %s", ticker, exc)
            return None
        titles = []
        for item in items:
            content = item.get("content") if isinstance(item.get("content"), dict) else item
            title = content.get("title")
            if title:
                titles.append(str(title))
        return titles


def make_provider(kind: str, directory: str | None = None) -> MarketDataProvider:
    if kind == "yfinance":
        return YFinanceProvider()
    if kind == "csv":
        if not directory:
            raise ValueError("CSV market data needs market_data_dir")
        return CSVProvider(directory)
    raise ValueError(f"Unknown market data provider: {kind}")


def as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)
