from datetime import date

import numpy as np
import pandas as pd

from smg_trader.config import StrategyConfig
from smg_trader.market_data import AnalystActions, InMemoryProvider
from smg_trader.models import Action
from smg_trader.strategies import (
    STRATEGY_REGISTRY,
    MomentumStrategy,
    Strategy,
    StrategyContext,
    TechnicalStrategy,
    Vote,
    build_strategy,
    register_strategy,
)
from smg_trader.strategies.indicators import rsi, sma
from smg_trader.strategies.momentum import headline_sentiment

from .conftest import END_DATE, make_prices

UP = ["UPA", "UPB", "UPC", "UPD", "UPE", "UPF"]
DOWN = ["DNA", "DNB", "DNC", "DND"]
ALL = UP + DOWN + ["FLA", "FLB"]


def ctx(provider, live=True):
    return StrategyContext(provider, as_of=END_DATE.date(), live=live)


def test_momentum_buys_uptrends_and_sells_downtrends(universe_provider):
    signals = {s.ticker: s for s in MomentumStrategy(min_signals=3).generate_all(ALL, ctx(universe_provider))}
    assert len(signals) == 12
    for t in UP:
        assert signals[t].action == Action.BUY, signals[t].reason
        assert signals[t].details["bullish"] >= 3
    for t in DOWN:
        assert signals[t].action == Action.SELL, signals[t].reason
    for s in signals.values():
        assert 0 <= s.confidence <= 1 and s.reason and s.strategy == "momentum"


def test_momentum_requires_n_aligned_sources(universe_provider):
    strict = MomentumStrategy(min_signals=6)  # more sources than can vote in this data
    sig = strict.generate("UPA", ctx(universe_provider))
    assert sig.action == Action.HOLD
    assert sig.score > 0  # still bullish, just not enough agreement


def test_momentum_live_only_sources_abstain_in_backtest(universe_provider):
    universe_provider._analyst["UPA"] = AnalystActions(upgrades=3, downgrades=0)
    universe_provider._news["UPA"] = ["UPA beats estimates, shares surge to record"]
    live = MomentumStrategy().generate("UPA", ctx(universe_provider, live=True))
    replay = MomentumStrategy().generate("UPA", ctx(universe_provider, live=False))
    assert live.details["sources"]["analysts"] == 1 and live.details["sources"]["news"] == 1
    assert "analysts" not in replay.details["sources"] and "news" not in replay.details["sources"]


def test_custom_momentum_source(universe_provider):
    always_bear = lambda ticker, c: Vote("bear", -1, "always bearish")  # noqa: E731
    sig = MomentumStrategy(min_signals=1, sources=[always_bear]).generate("UPA", ctx(universe_provider))
    assert sig.action == Action.SELL and sig.score == -1


def test_headline_sentiment():
    assert headline_sentiment(["Stock surges after earnings beat", "Analyst upgrade"]) >= 2
    assert headline_sentiment(["Shares plunge on lawsuit", "Company warns of weak demand"]) <= -2


def test_technical_rsi_oversold_and_ma_cross():
    # Long uptrend, then a sharp drop: price below 50d MA and RSI oversold.
    df = make_prices(0.002, n=260, noise=0.001)
    df.iloc[-8:, df.columns.get_loc("Close")] = df["Close"].iloc[-9] * 0.97 ** np.arange(1, 9)
    p = InMemoryProvider({"X": df})
    sig = TechnicalStrategy().generate("X", ctx(p))
    assert sig.details["rsi"] < 30
    assert "oversold" in sig.reason
    assert "below 50d MA" in sig.reason


def test_technical_detects_cross_above():
    closes = [100.0] * 60 + [100 - i * 0.5 for i in range(20)] + [90 + i * 2 for i in range(5)]
    idx = pd.bdate_range(end=END_DATE, periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1e6}, index=idx)
    sig = TechnicalStrategy().generate("X", ctx(InMemoryProvider({"X": df})))
    assert "crossed above 50d MA" in sig.reason
    assert sig.details["components"]["ma_cross"] == 1.0


def test_indicators():
    s = pd.Series(range(1, 31), dtype=float)
    assert sma(s, 5).iloc[-1] == 28.0
    assert rsi(s).iloc[-1] == 100.0
    assert rsi(pd.Series([10.0] * 30)).iloc[-1] == 50.0


def test_fundamentals_abstain_in_backtest(universe_provider):
    f = build_strategy(StrategyConfig(), only="fundamentals")
    assert f.generate("UPA", ctx(universe_provider, live=False)) is None
    live = f.generate("UPA", ctx(universe_provider, live=True))
    assert live.score > 0 and "P/E 18.0" in live.reason
    assert f.generate("DNA", ctx(universe_provider)).score < 0


def test_ensemble_weighted_average(universe_provider):
    cfg = StrategyConfig(weights={"momentum": 0.5, "technical": 0.3, "fundamentals": 0.2})
    ens = build_strategy(cfg)
    c = ctx(universe_provider)
    sig = ens.generate("UPA", c)
    members = sig.details["members"]
    expected = sum(m["score"] * m["weight"] for m in members.values())
    assert abs(sig.score - expected) < 1e-3
    assert set(members) == {"momentum", "technical", "fundamentals"}
    # In a backtest fundamentals abstain and weights renormalise over the rest.
    replay = ens.generate("UPA", ctx(universe_provider, live=False))
    assert set(replay.details["members"]) == {"momentum", "technical"}
    assert abs(sum(m["weight"] for m in replay.details["members"].values()) - 1) < 1e-3


def test_signals_for_ten_plus_stocks(universe_provider):
    signals = build_strategy(StrategyConfig()).generate_all(ALL, ctx(universe_provider))
    assert len(signals) >= 10
    assert all(s.ticker and s.action in Action and 0 <= s.confidence <= 1 and s.reason for s in signals)


def test_new_strategy_plugs_in_without_core_changes(universe_provider):
    @register_strategy("always_buy_test")
    class AlwaysBuy(Strategy):
        def score(self, ticker, c):
            return 1.0, "test", {}

    try:
        cfg = StrategyConfig(weights={"always_buy_test": 1.0})
        sig = build_strategy(cfg).generate("FLA", ctx(universe_provider))
        assert sig.action == Action.BUY and sig.confidence == 1.0
    finally:
        STRATEGY_REGISTRY.pop("always_buy_test")


def test_strategy_errors_are_contained(universe_provider):
    class Broken(Strategy):
        name = "broken"

        def score(self, ticker, c):
            raise RuntimeError("boom")

    assert Broken().generate_all(["UPA", "UPB"], ctx(universe_provider)) == []


def test_context_caps_history_at_as_of(universe_provider):
    c = StrategyContext(universe_provider, as_of=date(2026, 6, 1))
    assert c.history("UPA").index.max() <= pd.Timestamp("2026-06-01")
