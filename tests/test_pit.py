"""Point-in-time data: store, providers, Yahoo reconstruction, and backtests that use it."""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from smg_trader.backtest import Backtester
from smg_trader.config import StrategyConfig
from smg_trader.market_data import (
    CSVProvider,
    Fundamentals,
    InMemoryProvider,
    YFinanceProvider,
    fundamentals_from_earnings,
)
from smg_trader.pit import PointInTimeProvider, PointInTimeStore, record_snapshot
from smg_trader.strategies import MomentumStrategy, Strategy, StrategyContext, build_strategy

from .conftest import END_DATE, make_prices

START, END = date(2026, 7, 22), date(2026, 9, 22)


def test_store_dedupes_by_day_and_respects_as_of(tmp_path):
    store = PointInTimeStore(tmp_path)
    store.record("fundamentals", "ko", date(2026, 9, 1), {"pe_ratio": 20})
    store.record("fundamentals", "KO", date(2026, 9, 1), {"pe_ratio": 21})  # same day: last wins
    store.record("fundamentals", "KO", date(2026, 9, 8), {"pe_ratio": 22})
    assert [(d.day, r["pe_ratio"]) for d, r in store.entries("fundamentals", "KO")] == [(1, 21), (8, 22)]
    assert store.latest("fundamentals", "KO", date(2026, 9, 7))[1]["pe_ratio"] == 21
    assert store.latest("fundamentals", "KO", date(2026, 8, 31)) is None
    assert len(store.between("fundamentals", "KO", date(2026, 9, 2), date(2026, 9, 30))) == 1
    assert store.days("fundamentals") == {date(2026, 9, 1), date(2026, 9, 8)}


def test_point_in_time_provider_prefers_store_then_base(tmp_path):
    base = InMemoryProvider({}, fundamentals_history={"KO": [(date(2026, 1, 1), Fundamentals(pe_ratio=15))]},
                            news_history={"KO": [(date(2026, 9, 2), "KO base headline")]})
    store = PointInTimeStore(tmp_path)
    store.record("fundamentals", "KO", date(2026, 9, 1), {"pe_ratio": 25, "unknown_field": 1})
    store.record("news", "KO", date(2026, 9, 10), {"headlines": ["KO beats", "KO beats", "KO raises"]})
    pit = PointInTimeProvider(base, store)
    assert pit.fundamentals_at("KO", date(2026, 9, 5)).pe_ratio == 25
    assert pit.fundamentals_at("KO", date(2026, 6, 1)).pe_ratio == 15        # before any snapshot
    assert pit.fundamentals_at("KO", date(2027, 6, 1)).pe_ratio == 15        # snapshot too stale
    assert pit.news_at("KO", date(2026, 9, 8), date(2026, 9, 12)) == ["KO beats", "KO raises"]
    assert pit.news_at("KO", date(2026, 9, 1), date(2026, 9, 3)) == ["KO base headline"]


def test_record_snapshot(tmp_path):
    p = InMemoryProvider({}, fundamentals={"KO": Fundamentals(pe_ratio=22, sector="Consumer Defensive")},
                         news={"KO": ["KO beats"]})
    store = PointInTimeStore(tmp_path)
    assert record_snapshot(store, p, ["KO", "NONE"], date(2026, 9, 1)) == 2
    assert store.latest("fundamentals", "KO", date(2026, 9, 1))[1]["sector"] == "Consumer Defensive"
    assert store.latest("news", "KO", date(2026, 9, 1))[1]["headlines"] == ["KO beats"]


def test_fundamentals_from_earnings_uses_only_published_reports():
    reports = pd.Series([1.0, 1.0, 1.0, 1.0, 1.2, 1.2, 1.2, 1.2],
                        index=pd.to_datetime(["2024-10-25", "2025-01-25", "2025-04-25", "2025-07-25",
                                              "2025-10-25", "2026-01-25", "2026-04-25", "2026-07-25"]))
    divs = pd.Series([0.5, 0.5, 0.5, 0.5], index=pd.to_datetime(["2025-09-01", "2025-12-01", "2026-03-01",
                                                                  "2026-06-01"]))
    f = fundamentals_from_earnings(reports, divs, 96.0, date(2026, 8, 1))
    assert f.pe_ratio == pytest.approx(96 / 4.8) and f.eps_growth == pytest.approx(0.2)
    assert f.dividend_yield == pytest.approx(2.0 / 96)
    # Before the 2026-07-25 report, TTM EPS is 1.0 + 3 x 1.2 and growth needs 8 quarters.
    f = fundamentals_from_earnings(reports, divs, 96.0, date(2026, 7, 24))
    assert f.pe_ratio == pytest.approx(96 / 4.6) and f.eps_growth is None
    assert fundamentals_from_earnings(reports, divs, 96.0, date(2025, 6, 1)) is None


class FakeYahooTicker:
    def __init__(self, symbol):
        self.symbol = symbol
        idx = pd.bdate_range(end=END_DATE, periods=320, tz="America/New_York")
        self._prices = make_prices(0.001, seed=4, start_price=90).set_index(idx)
        self.info = {"sector": "Consumer Defensive", "exchange": "NYQ", "trailingPE": 99}

    def history(self, start, end, auto_adjust=True):
        return self._prices

    def get_earnings_dates(self, limit=40):
        dates = pd.to_datetime(["2026-10-25", "2026-07-25", "2026-04-25", "2026-01-25", "2025-10-25",
                                "2025-07-25", "2025-04-25", "2025-01-25", "2024-10-25"]).tz_localize("America/New_York")
        return pd.DataFrame({"EPS Estimate": 1.0,
                             "Reported EPS": [np.nan, 1.3, 1.2, 1.2, 1.2, 1.0, 1.0, 1.0, 1.0]},
                            index=pd.Index(dates, name="Earnings Date"))

    @property
    def dividends(self):
        return pd.Series([0.5] * 4, index=pd.to_datetime(
            ["2025-09-01", "2025-12-01", "2026-03-01", "2026-06-01"]).tz_localize("America/New_York"))

    @property
    def upgrades_downgrades(self):
        idx = pd.to_datetime(["2026-09-10", "2026-09-01", "2026-06-01"]).tz_localize("UTC")
        return pd.DataFrame({"Firm": ["A", "B", "C"], "ToGrade": ["Buy", "Buy", "Sell"],
                             "FromGrade": ["Hold", "Hold", "Hold"], "Action": ["up", "up", "down"]},
                            index=pd.Index(idx, name="GradeDate"))

    @property
    def news(self):
        return [{"content": {"title": "KO beats estimates", "pubDate": "2026-09-21T14:00:00Z"}},
                {"title": "KO raises dividend",
                 "providerPublishTime": int(datetime(2026, 9, 18, tzinfo=timezone.utc).timestamp())}]


def test_yfinance_point_in_time_with_fake_ticker():
    yf = YFinanceProvider(ticker_factory=FakeYahooTicker)
    f = yf.fundamentals_at("KO", date(2026, 8, 3))
    price = yf.close_at("KO", date(2026, 8, 3))
    assert f.pe_ratio == pytest.approx(price / (1.3 + 1.2 + 1.2 + 1.2))
    assert f.eps_growth == pytest.approx(4.9 / 4.0 - 1)
    assert f.dividend_yield == pytest.approx(2.0 / price)
    assert f.sector == "Consumer Defensive" and f.exchange == "NYSE"
    assert yf.fundamentals("KO").pe_ratio == 99  # live value is untouched

    a = yf.analyst_actions_at("KO", date(2026, 8, 15), date(2026, 9, 5))
    assert (a.upgrades, a.downgrades) == (1, 0)  # the 9/10 upgrade wasn't known yet
    assert yf.analyst_actions_at("KO", date(2026, 5, 1), date(2026, 6, 30)).downgrades == 1

    assert yf.news_at("KO", date(2026, 9, 17), date(2026, 9, 22)) == ["KO beats estimates", "KO raises dividend"]
    assert yf.news_at("KO", date(2026, 8, 1), date(2026, 8, 7)) is None  # older than the feed: unknown
    assert yf.news_headlines("KO") == ["KO beats estimates", "KO raises dividend"]


def test_csv_point_in_time_files(tmp_path):
    make_prices(0.001).to_csv(tmp_path / "KO.csv", index_label="Date")
    (tmp_path / "fundamentals_history.csv").write_text(
        "date,ticker,pe_ratio,eps_growth,dividend_yield\n2026-07-25,KO,18,0.12,0.03\n2026-04-25,KO,30,,0.03\n")
    (tmp_path / "analyst.csv").write_text("date,ticker,action,firm\n2026-09-01,KO,up,Firm A\n")
    (tmp_path / "news.csv").write_text("date,ticker,headline\n2026-09-20,KO,KO shares surge to record\n")
    p = CSVProvider(tmp_path)
    assert set(p.prices) == {"KO"}  # history files aren't mistaken for tickers
    assert p.fundamentals_at("KO", date(2026, 6, 1)).pe_ratio == 30
    assert p.fundamentals_at("KO", date(2026, 8, 1)).eps_growth == 0.12
    assert p.fundamentals_at("KO", date(2026, 1, 1)) is None
    assert p.analyst_actions_at("KO", date(2026, 8, 25), date(2026, 9, 5)).upgrades == 1
    assert p.news_at("KO", date(2026, 9, 15), date(2026, 9, 21)) == ["KO shares surge to record"]


def test_context_routes_live_vs_backtest():
    p = InMemoryProvider({}, fundamentals={"KO": Fundamentals(pe_ratio=99, sector="Tech")},
                         fundamentals_history={"KO": [(date(2026, 1, 1), Fundamentals(pe_ratio=15))]})
    live = StrategyContext(p, as_of=date(2026, 9, 1), live=True)
    replay = StrategyContext(p, as_of=date(2026, 9, 1), live=False)
    assert live.fundamentals("KO").pe_ratio == 99
    assert replay.fundamentals("KO").pe_ratio == 15 and replay.fundamentals("KO").sector == "Tech"
    assert StrategyContext(p, as_of=date(2025, 1, 1), live=False).fundamentals("KO") is None


def test_backtest_uses_point_in_time_fundamentals_without_lookahead(universe_provider, rules):
    # Quarterly snapshots for UPA; the last one is dated after the backtest ends.
    hist = {"UPA": [(date(2026, 4, 25), Fundamentals(pe_ratio=18, eps_growth=0.25, dividend_yield=0.01)),
                    (date(2026, 8, 25), Fundamentals(pe_ratio=60, eps_growth=-0.3)),
                    (date(2026, 12, 1), Fundamentals(pe_ratio=5, eps_growth=1.0))]}
    p = InMemoryProvider(universe_provider.prices, universe_provider._fundamentals, fundamentals_history=hist)
    seen = []

    class Spy(Strategy):
        name = "spy"

        def score(self, ticker, ctx):
            f = ctx.fundamentals(ticker)
            seen.append((ctx.as_of, f.pe_ratio if f else None))
            return 0.0, "", {}

    result = Backtester(Spy(), p, ["UPA", "UPB"], rules).run(START, END)
    by_day = dict(seen[::2])  # UPA's entries
    assert by_day[date(2026, 8, 24)] == 18 and by_day[date(2026, 8, 25)] == 60
    assert 5 not in by_day.values()  # December data never leaks in
    assert 0 < result.metrics["pit_coverage"]["fundamentals"] < 1  # UPA only
    assert "Point-in-time" in result.summary()

    ens = build_strategy(StrategyConfig(weights={"momentum": 0.5, "fundamentals": 0.5}))
    sig = ens.generate("UPA", StrategyContext(p, as_of=date(2026, 9, 1), live=False))
    assert set(sig.details["members"]) == {"momentum", "fundamentals"}


def test_backtest_news_and_analyst_sources(universe_provider, rules):
    news = {"UPA": [(START + timedelta(days=i), "UPA shares surge to record as growth beats")
                    for i in range(0, 60, 3)]}
    analyst = {"UPA": [(START + timedelta(days=10), "up", "Firm A"), (START + timedelta(days=11), "up", "Firm B")]}
    p = InMemoryProvider(universe_provider.prices, universe_provider._fundamentals, news_history=news,
                         analyst_history=analyst)
    sig = MomentumStrategy().generate("UPA", StrategyContext(p, as_of=START + timedelta(days=15), live=False))
    assert sig.details["sources"]["news"] == 1 and sig.details["sources"]["analysts"] == 1
    later = MomentumStrategy().generate("UPA", StrategyContext(p, as_of=START + timedelta(days=50), live=False))
    assert later.details["sources"]["analysts"] == 0  # the upgrades are older than 30 days by then
