"""Pairs trading: spread statistics, entry/exit/stop logic, linked orders."""

import math

import numpy as np
import pandas as pd
import pytest

from smg_trader.config import PairsConfig, RulesConfig
from smg_trader.market_data import InMemoryProvider
from smg_trader.models import Action, Holding, Portfolio
from smg_trader.strategies import PairsTrader, StrategyContext, spread_stats

from .conftest import END_DATE


def make_pair(n=250, seed=0, beta=1.0, shock=0.0, half_life=4.0, noise=0.004, end=END_DATE, last=None):
    """A = exp(0.5 + beta * log B + OU noise); ``shock`` is added to the spread on the last day
    (or ``last`` replaces it)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=end, periods=n)
    lb = math.log(50) + np.cumsum(0.01 * rng.standard_normal(n))
    phi = math.exp(-math.log(2) / half_life)
    e = np.zeros(n)
    for t in range(1, n):
        e[t] = phi * e[t - 1] + noise * rng.standard_normal()
    e[-1] = last if last is not None else e[-1] + shock
    la = 0.5 + beta * lb + e

    def frame(logp):
        close = np.exp(logp)
        return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1e6}, index=idx)

    return frame(la), frame(lb)


def provider(**kw):
    a, b = make_pair(**kw)
    return InMemoryProvider({"AAA": a, "BBB": b})


def ctx(p):
    return StrategyContext(p, as_of=END_DATE.date(), live=False)


@pytest.fixture
def trader():
    return PairsTrader(PairsConfig(enabled=True, pairs=[["AAA", "BBB"]], capital_pct=0.4),
                       RulesConfig(allow_short_selling=True, max_trades_per_run=10, max_position_pct=0.5))


def test_spread_stats_recovers_hedge_ratio_and_half_life():
    a, b = make_pair(beta=1.5, n=400)
    st = spread_stats(a["Close"], b["Close"], lookback=300)
    assert st.hedge_ratio == pytest.approx(1.5, abs=0.15)
    assert 1 < st.half_life < 15
    assert st.correlation > 0.8
    assert spread_stats(a["Close"].iloc[:10], b["Close"].iloc[:10], 60) is None


def test_entry_shorts_rich_leg_and_buys_cheap_leg(trader):
    p = provider(shock=0.025)
    portfolio = Portfolio(cash=100_000, total_equity=100_000)
    orders, signals, states = trader.plan(ctx(p), portfolio)
    st = states[0]
    assert st.tradable and trader.cfg.entry_z <= st.stats.zscore < trader.cfg.stop_z
    assert (st.current, st.target) == (0, -1)
    assert [(o.ticker, o.action) for o in orders] == [("AAA", Action.SHORT), ("BBB", Action.BUY)]
    assert {o.group_id for o in orders} == {"pair:AAA/BBB"}
    beta = st.stats.hedge_ratio
    a_val, b_val = orders[0].notional, orders[1].notional
    assert a_val + b_val == pytest.approx(40_000, rel=0.02)
    assert b_val / a_val == pytest.approx(beta, rel=0.05)
    assert {s.ticker: s.action for s in signals} == {"AAA": Action.SELL, "BBB": Action.BUY}


def test_negative_z_buys_a_shorts_b(trader):
    orders, _, states = trader.plan(ctx(provider(shock=-0.025)), Portfolio(100_000, 100_000))
    assert states[0].target == 1
    assert [(o.ticker, o.action) for o in orders] == [("AAA", Action.BUY), ("BBB", Action.SHORT)]


def open_short_spread(p):
    pa, pb = p.latest_price("AAA"), p.latest_price("BBB")
    return Portfolio(cash=120_000, total_equity=100_000, holdings=[
        Holding("AAA", 200, pa, 200 * pa, position="SHORT"), Holding("BBB", 300, pb, 300 * pb)])


def test_exit_when_spread_reverts(trader):
    p = provider(last=0.0)
    orders, _, states = trader.plan(ctx(p), open_short_spread(p))
    st = states[0]
    assert st.current == -1 and st.stats.zscore <= trader.cfg.exit_z
    assert st.target == 0 and "exit" in st.reason
    assert [(o.ticker, o.action, o.shares) for o in orders] == [("AAA", Action.COVER, 200),
                                                                ("BBB", Action.SELL, 300)]
    assert {o.group_id for o in orders} == {"pair:AAA/BBB"}


def test_hold_while_spread_still_wide(trader):
    p = provider(shock=0.025)
    orders, _, states = trader.plan(ctx(p), open_short_spread(p))
    assert states[0].target == -1 and "hold" in states[0].reason
    assert orders == []  # open pairs are not resized


def test_stop_loss_when_spread_blows_out(trader):
    p = provider(shock=0.15)
    orders, _, states = trader.plan(ctx(p), open_short_spread(p))
    assert states[0].stats.zscore >= trader.cfg.stop_z
    assert states[0].target == 0 and "stop" in states[0].reason
    assert {o.action for o in orders} == {Action.COVER, Action.SELL}
    # ...and never enters that far out.
    orders, _, states = trader.plan(ctx(p), Portfolio(100_000, 100_000))
    assert states[0].target == 0 and orders == []


def test_unrelated_stocks_are_not_traded(trader):
    rng = np.random.default_rng(5)
    idx = pd.bdate_range(end=END_DATE, periods=250)
    walks = {t: 50 * np.exp(np.cumsum(0.01 * rng.standard_normal(250))) for t in ("AAA", "BBB")}
    walks["AAA"][-1] *= 1.1
    p = InMemoryProvider({t: pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1e6}, index=idx)
                          for t, c in walks.items()})
    orders, _, states = trader.plan(ctx(p), Portfolio(100_000, 100_000))
    assert not states[0].tradable and "not mean-reverting" in states[0].reason
    assert orders == []


def test_needs_short_selling(trader):
    trader.rules.allow_short_selling = False
    assert trader.plan(ctx(provider(shock=0.025)), Portfolio(100_000, 100_000)) == ([], [], [])


def test_repairs_a_half_open_pair(trader):
    p = provider(shock=0.025)
    pa = p.latest_price("AAA")
    # Only the short leg is on (e.g. the buy leg failed and wasn't unwound).
    half = Portfolio(cash=100_000, total_equity=100_000,
                     holdings=[Holding("AAA", 200, pa, 200 * pa, position="SHORT")])
    orders, _, states = trader.plan(ctx(p), half)
    assert states[0].current == -1 and states[0].target == -1
    assert ("BBB", Action.BUY) in {(o.ticker, o.action) for o in orders}


def test_legs_scaled_to_position_limit(trader):
    trader.rules.max_position_pct = 0.10
    orders, _, _ = trader.plan(ctx(provider(shock=0.025)), Portfolio(100_000, 100_000))
    assert len(orders) == 2 and max(o.notional for o in orders) <= 10_000


def test_planner_holds_back_pair_capital(universe_provider):
    """Directional buys leave pairs.capital_pct free while the pair is flat."""
    from smg_trader.config import PortfolioConfig
    from smg_trader.models import Signal
    from smg_trader.planner import plan_orders

    a, b = make_pair(n=320, last=0.0)  # flat spread: no pair entry today
    universe_provider.prices.update({"AAA": a, "BBB": b})
    rules = RulesConfig(allow_short_selling=True, max_trades_per_run=20, max_position_pct=0.5)
    pairs = PairsConfig(enabled=True, pairs=[["AAA", "BBB"]], capital_pct=0.3)
    sigs = [Signal(t, Action.BUY, 1.0, 1.0, "r", "t", price=universe_provider.latest_price(t))
            for t in ("UPA", "UPB", "UPC")]
    p = Portfolio(cash=100_000, total_equity=100_000)
    c = StrategyContext(universe_provider, as_of=END_DATE.date(), live=False)
    plan = plan_orders(sigs, p, c, rules=rules, portfolio_cfg=PortfolioConfig(method="equal", long_exposure=1.0),
                       pairs_cfg=pairs, min_confidence=0.4)
    spent = sum(o.notional for o in plan.orders)
    assert plan.pair_states[0].target == 0
    assert 60_000 < spent <= 100_000 - 1_000 - 30_000  # cash reserve + pair reserve kept free
