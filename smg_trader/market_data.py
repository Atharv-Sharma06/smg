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
    """Interface every data source implements.

    Two families of methods:

    * current data (``fundamentals``, ``analyst_actions``, ``news_headlines``)
      for live runs, and
    * point-in-time data (``*_at``) for backtests: only what was known on the
      given date. Returning ``None`` means "unknown", and strategies then abstain
      rather than use today's numbers in the past.
    """

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

    # -- point in time ------------------------------------------------------
    def fundamentals_at(self, ticker: str, as_of: date) -> Fundamentals | None:
        return None

    def analyst_actions_at(self, ticker: str, start: date, end: date) -> AnalystActions | None:
        return None

    def news_at(self, ticker: str, start: date, end: date) -> list[str] | None:
        return None

    def close_at(self, ticker: str, as_of: date) -> float | None:
        df = self.history(ticker, as_of - timedelta(days=10), as_of)
        return float(df["Close"].iloc[-1]) if not df.empty else None


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


def _naive_dates(index: Any) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(index, utc=True)).tz_localize(None)
    return idx.normalize()


def _count_actions(dated: list[tuple[date, str, str]], start: date, end: date) -> AnalystActions:
    out = AnalystActions()
    for day, action, note in dated:
        if start <= day <= end:
            if action == "up":
                out.upgrades += 1
            elif action == "down":
                out.downgrades += 1
            if note and len(out.details) < 5:
                out.details.append(f"{day}: {note}")
    return out


def fundamentals_from_earnings(eps: pd.Series, dividends: pd.Series | None, price: float | None,
                               as_of: date) -> Fundamentals | None:
    """Trailing P/E, EPS growth and dividend yield as they stood on ``as_of``.

    ``eps`` is reported quarterly EPS indexed by **report date** (only reports
    published before ``as_of`` are used). ``dividends`` is indexed by ex-date.
    """
    if price is None or price <= 0:
        return None
    eps = eps.dropna()
    eps = eps[eps.index < pd.Timestamp(as_of)].sort_index()
    if len(eps) < 4:
        return None
    ttm = float(eps.iloc[-4:].sum())
    growth = None
    if len(eps) >= 8:
        prev = float(eps.iloc[-8:-4].sum())
        growth = (ttm - prev) / abs(prev) if prev else None
    div_yield = 0.0
    if dividends is not None and len(dividends):
        window = dividends[(dividends.index <= pd.Timestamp(as_of))
                           & (dividends.index > pd.Timestamp(as_of - timedelta(days=365)))]
        div_yield = float(window.sum()) / price
    return Fundamentals(pe_ratio=price / ttm if ttm else None, eps_growth=growth, dividend_yield=div_yield)


class InMemoryProvider(MarketDataProvider):
    """Provider backed by dicts. Used by tests, CSV data and the backtester.

    ``fundamentals`` / ``analyst`` / ``news`` are *current* values. The
    ``*_history`` arguments hold dated values for point-in-time lookups:

    * ``fundamentals_history``: {ticker: [(date, Fundamentals), ...]}
    * ``analyst_history``: {ticker: [(date, "up" | "down", note), ...]}
    * ``news_history``: {ticker: [(date, headline), ...]}
    """

    def __init__(
        self,
        prices: dict[str, pd.DataFrame],
        fundamentals: dict[str, Fundamentals] | None = None,
        analyst: dict[str, AnalystActions] | None = None,
        news: dict[str, list[str]] | None = None,
        *,
        fundamentals_history: dict[str, list[tuple[date, Fundamentals]]] | None = None,
        analyst_history: dict[str, list[tuple[date, str, str]]] | None = None,
        news_history: dict[str, list[tuple[date, str]]] | None = None,
    ):
        self.prices = {k.upper(): _clean_history(v) for k, v in prices.items()}
        self._fundamentals = {k.upper(): v for k, v in (fundamentals or {}).items()}
        self._analyst = {k.upper(): v for k, v in (analyst or {}).items()}
        self._news = {k.upper(): v for k, v in (news or {}).items()}
        self.fundamentals_history = {k.upper(): sorted(v, key=lambda e: e[0])
                                     for k, v in (fundamentals_history or {}).items()}
        self.analyst_history = {k.upper(): sorted(v, key=lambda e: e[0])
                                for k, v in (analyst_history or {}).items()}
        self.news_history = {k.upper(): sorted(v, key=lambda e: e[0]) for k, v in (news_history or {}).items()}

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
        key = ticker.upper()
        if key in self._analyst:
            return self._analyst[key]
        return self.analyst_actions_at(key, since, date.today())

    def news_headlines(self, ticker: str) -> list[str] | None:
        return self._news.get(ticker.upper())

    def fundamentals_at(self, ticker: str, as_of: date) -> Fundamentals | None:
        entries = [f for d, f in self.fundamentals_history.get(ticker.upper(), []) if d <= as_of]
        if not entries:
            return None
        f = entries[-1]
        static = self._fundamentals.get(ticker.upper())
        if static is not None:  # sector / exchange don't change
            f = Fundamentals(**{**f.__dict__, "sector": f.sector or static.sector,
                                "exchange": f.exchange or static.exchange})
        return f

    def analyst_actions_at(self, ticker: str, start: date, end: date) -> AnalystActions | None:
        dated = self.analyst_history.get(ticker.upper())
        return _count_actions(dated, start, end) if dated is not None else None

    def news_at(self, ticker: str, start: date, end: date) -> list[str] | None:
        dated = self.news_history.get(ticker.upper())
        if dated is None:
            return None
        return [h for d, h in dated if start <= d <= end]


class CSVProvider(InMemoryProvider):
    """Loads a folder of CSV data for offline runs and backtests.

    * ``<TICKER>.csv``: Date,Open,High,Low,Close,Volume
    * ``fundamentals.json``: current values, {"AAPL": {"pe_ratio": 28, "sector": "Technology", ...}}
    * ``fundamentals_history.csv``: date,ticker,pe_ratio,eps_growth,dividend_yield
      (``date`` = when the numbers became public, e.g. the earnings report date)
    * ``analyst.csv``: date,ticker,action,firm   (action = up | down | main | init)
    * ``news.csv``: date,ticker,headline
    """

    SPECIAL = {"fundamentals_history", "analyst", "news"}

    def __init__(self, directory: str | Path):
        directory = Path(directory)
        prices = {}
        for path in sorted(directory.glob("*.csv")):
            if path.stem.lower() in self.SPECIAL:
                continue
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            df.columns = [c.strip().title() for c in df.columns]
            prices[path.stem.upper()] = df
        fundamentals = {}
        fpath = directory / "fundamentals.json"
        if fpath.exists():
            for ticker, values in json.loads(fpath.read_text()).items():
                fundamentals[ticker] = Fundamentals(**values)

        def rows(name: str) -> list[dict[str, str]]:
            path = directory / f"{name}.csv"
            if not path.exists():
                return []
            df = pd.read_csv(path, dtype=str).fillna("")
            df.columns = [c.strip().lower() for c in df.columns]
            return df.to_dict("records")

        def num(v: str) -> float | None:
            return float(v) if v not in ("", None) else None

        f_hist: dict[str, list[tuple[date, Fundamentals]]] = {}
        for r in rows("fundamentals_history"):
            f_hist.setdefault(r["ticker"].upper(), []).append((date.fromisoformat(r["date"][:10]), Fundamentals(
                pe_ratio=num(r.get("pe_ratio", "")), eps_growth=num(r.get("eps_growth", "")),
                dividend_yield=num(r.get("dividend_yield", "")))))
        a_hist: dict[str, list[tuple[date, str, str]]] = {}
        for r in rows("analyst"):
            a_hist.setdefault(r["ticker"].upper(), []).append(
                (date.fromisoformat(r["date"][:10]), r.get("action", "").lower(), r.get("firm", "")))
        n_hist: dict[str, list[tuple[date, str]]] = {}
        for r in rows("news"):
            n_hist.setdefault(r["ticker"].upper(), []).append((date.fromisoformat(r["date"][:10]), r["headline"]))
        super().__init__(prices, fundamentals, fundamentals_history=f_hist, analyst_history=a_hist,
                         news_history=n_hist)


class YFinanceProvider(MarketDataProvider):
    """Live data from Yahoo Finance via ``yfinance`` (free, no API key).

    Point-in-time support:

    * fundamentals: trailing P/E and EPS growth rebuilt from reported quarterly
      EPS (by report date) and dividend yield from the dividend history
    * analyst actions: Yahoo's dated upgrade/downgrade history (years deep)
    * news: only Yahoo's most recent headlines, so older dates come back unknown;
      the point-in-time store (``smg_trader.pit``) fills that in over time

    Results are cached per ticker for the life of the provider.
    """

    def __init__(self, ticker_factory: Any = None) -> None:
        if ticker_factory is None:
            import yfinance as yf

            ticker_factory = yf.Ticker
        self._ticker_factory = ticker_factory
        # ticker -> (fetched_start, fetched_end, frame)
        self._history: dict[str, tuple[date, date, pd.DataFrame]] = {}
        self._info: dict[str, dict[str, Any]] = {}
        self._cache: dict[tuple[str, str], Any] = {}

    def _ticker(self, ticker: str):
        return self._ticker_factory(ticker)

    def _cached(self, kind: str, ticker: str, fetch: Any) -> Any:
        key = (kind, ticker.upper())
        if key not in self._cache:
            try:
                self._cache[key] = fetch(self._ticker(ticker.upper()))
            except Exception as exc:  # network / parsing errors
                log.debug("%s(%s) failed: %s", kind, ticker, exc)
                self._cache[key] = None
        return self._cache[key]

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

    # -- point in time ------------------------------------------------------
    def reported_eps(self, ticker: str) -> pd.Series | None:
        """Quarterly reported EPS indexed by report date."""

        def fetch(t: Any) -> pd.Series | None:
            df = t.get_earnings_dates(limit=40)
            if df is not None and not df.empty and "Reported EPS" in df.columns:
                s = pd.Series(df["Reported EPS"].to_numpy(dtype=float), index=_naive_dates(df.index))
                return s.dropna().sort_index()
            # Fallback: income statement by fiscal period end, assumed public 45 days later.
            stmt = t.quarterly_income_stmt
            if stmt is None or stmt.empty or "Diluted EPS" not in stmt.index:
                return None
            row = stmt.loc["Diluted EPS"]
            return pd.Series(row.to_numpy(dtype=float),
                             index=_naive_dates(row.index) + pd.Timedelta(days=45)).dropna().sort_index()

        return self._cached("eps", ticker, fetch)

    def _dividends(self, ticker: str) -> pd.Series | None:
        def fetch(t: Any) -> pd.Series:
            d = t.dividends
            return pd.Series(d.to_numpy(dtype=float), index=_naive_dates(d.index)) if d is not None and len(d) \
                else pd.Series(dtype=float)

        return self._cached("dividends", ticker, fetch)

    def fundamentals_at(self, ticker: str, as_of: date) -> Fundamentals | None:
        eps = self.reported_eps(ticker)
        if eps is None:
            return None
        f = fundamentals_from_earnings(eps, self._dividends(ticker), self.close_at(ticker, as_of), as_of)
        if f is not None:
            info = self._get_info(ticker)
            f.sector, f.exchange = info.get("sector"), normalize_exchange(info.get("exchange"))
        return f

    def _upgrades(self, ticker: str) -> list[tuple[date, str, str]] | None:
        def fetch(t: Any) -> list[tuple[date, str, str]]:
            df = t.upgrades_downgrades
            if df is None or df.empty:
                return []
            out = []
            for day, (_, row) in zip(_naive_dates(df.index), df.iterrows()):
                note = f"{row.get('Firm', '?')}: {row.get('FromGrade', '')}->{row.get('ToGrade', '')}"
                out.append((day.date(), str(row.get("Action", "")).lower(), note))
            return sorted(out, key=lambda e: e[0], reverse=True)

        return self._cached("upgrades", ticker, fetch)

    def analyst_actions_at(self, ticker: str, start: date, end: date) -> AnalystActions | None:
        dated = self._upgrades(ticker)
        return _count_actions(dated, start, end) if dated is not None else None

    def analyst_actions(self, ticker: str, since: date) -> AnalystActions | None:
        return self.analyst_actions_at(ticker, since, date.today())

    def _news(self, ticker: str) -> list[tuple[date | None, str]] | None:
        def fetch(t: Any) -> list[tuple[date | None, str]]:
            out = []
            for item in t.news or []:
                content = item.get("content") if isinstance(item.get("content"), dict) else item
                title = content.get("title")
                if not title:
                    continue
                when = content.get("pubDate") or content.get("displayTime")
                ts = item.get("providerPublishTime")
                day = None
                if when:
                    day = pd.Timestamp(when).date()
                elif ts:
                    day = datetime.fromtimestamp(int(ts)).date()
                out.append((day, str(title)))
            return out

        return self._cached("news", ticker, fetch)

    def news_headlines(self, ticker: str) -> list[str] | None:
        items = self._news(ticker)
        return [t for _, t in items] if items is not None else None

    def news_at(self, ticker: str, start: date, end: date) -> list[str] | None:
        items = [(d, t) for d, t in (self._news(ticker) or []) if d is not None]
        in_window = [t for d, t in items if start <= d <= end]
        if in_window:
            return in_window
        if not items or min(d for d, _ in items) > start:
            return None  # Yahoo's feed doesn't reach back that far: unknown, not "no news"
        return []


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
