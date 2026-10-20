"""Portfolio construction: weighting methods, caps, and rebalancing to targets."""

import numpy as np
import pandas as pd
import pytest

from smg_trader.allocation import (
    book_weights,
    cap_weights,
    mean_variance_weights,
    plan_targets,
    risk_contributions,
    risk_parity_weights,
)
from smg_trader.config import PortfolioConfig
from smg_trader.market_data import InMemoryProvider
from smg_trader.models import Action, Holding, Portfolio, Signal
from smg_trader.strategies import StrategyContext

from .conftest import END_DATE, make_prices


@pytest.fixture
def vol_provider():
    """Three uptrending stocks with daily vol 0.5%, 1% and 2%."""
    prices = {"LOW": make_prices(0.001, seed=1, noise=0.005), "MID": make_prices(0.001, seed=2, noise=0.01),
              "HIGH": make_prices(0.001, seed=3, noise=0.02)}
    return InMemoryProvider(prices)


def ctx(provider):
    return StrategyContext(provider, as_of=END_DATE.date(), live=False)


def sigs(provider, conf=None, action=Action.BUY):
    conf = conf or {}
    return [Signal(t, action, conf.get(t, 0.6), conf.get(t, 0.6) * (1 if action == Action.BUY else -1), "r", "t",
                   price=provider.latest_price(t)) for t in ("LOW", "MID", "HIGH")]


def test_risk_parity_equalises_risk_contributions():
    rng = np.random.default_rng(0)
    a = rng.standard_normal((500, 4)) * [0.01, 0.02, 0.03, 0.015]
    a[:, 1] += 0.5 * a[:, 0]
    cov = np.cov(a.T)
    w = risk_parity_weights(cov)
    assert w.sum() == pytest.approx(1) and (w > 0).all()
    assert np.allclose(risk_contributions(w, cov), 0.25, atol=1e-4)


def test_mean_variance_long_only():
    cov = np.diag([0.0001, 0.0004, 0.0009])
    w = mean_variance_weights(cov, np.array([0.01, 0.01, 0.01]))
    assert w[0] > w[1] > w[2] > 0 and w.sum() == pytest.approx(1)
    # A name whose return is fully explained by others gets no weight, not a short.
    cov = np.array([[1.0, 0.99], [0.99, 1.0]]) * 1e-4
    w = mean_variance_weights(cov, np.array([0.02, 0.001]))
    assert w[1] == 0 and w[0] == pytest.approx(1)


@pytest.mark.parametrize("method", ["equal", "confidence", "inverse_vol", "risk_parity", "mean_variance"])
def test_book_weights_sum_to_one(vol_provider, method):
    w = book_weights(method, sigs(vol_provider, {"LOW": 0.5, "MID": 0.7, "HIGH": 0.9}), ctx(vol_provider),
                     PortfolioConfig())
    assert set(w) == {"LOW", "MID", "HIGH"} and sum(w.values()) == pytest.approx(1)
    if method == "inverse_vol" or method == "risk_parity":
        assert w["LOW"] > w["MID"] > w["HIGH"]
    if method == "confidence":
        assert w["HIGH"] > w["MID"] > w["LOW"]


def test_cov_methods_fall_back_without_history():
    short = {t: make_prices(0.001, n=10, seed=i) for i, t in enumerate(["A", "B"])}
    p = InMemoryProvider(short)
    s = [Signal(t, Action.BUY, 0.6, 0.6, "r", "t", price=10.0) for t in ("A", "B")]
    assert book_weights("risk_parity", s, ctx(p), PortfolioConfig()) == {"A": 0.5, "B": 0.5}


def test_cap_weights_redistributes():
    out = cap_weights({"A": 0.7, "B": 0.2, "C": 0.1}, exposure=0.9, cap=0.3)
    assert out["A"] == pytest.approx(0.3) and sum(out.values()) == pytest.approx(0.9)
    assert max(out.values()) <= 0.3 + 1e-9
    everyone_capped = cap_weights({"A": 0.5, "B": 0.5}, exposure=0.9, cap=0.2)
    assert everyone_capped == {"A": 0.2, "B": 0.2}  # rest stays in cash


def test_plan_targets_builds_book_and_trims(vol_provider, rules):
    rules.max_position_pct = 0.40
    cfg = PortfolioConfig(method="equal", long_exposure=0.9)
    px = {t: vol_provider.latest_price(t) for t in ("LOW", "MID", "HIGH")}
    # LOW has grown to 60% of equity: it should be trimmed to ~30%.
    low_shares = int(60_000 / px["LOW"])
    p = Portfolio(cash=40_000, total_equity=40_000 + low_shares * px["LOW"],
                  holdings=[Holding("LOW", low_shares, px["LOW"], low_shares * px["LOW"])])
    plan = plan_targets(sigs(vol_provider), p, ctx(vol_provider), cfg, rules, min_confidence=0.4)
    assert plan.weights == pytest.approx({"LOW": 0.3, "MID": 0.3, "HIGH": 0.3})
    actions = {(o.ticker, o.action) for o in plan.orders}
    assert actions == {("LOW", Action.SELL), ("MID", Action.BUY), ("HIGH", Action.BUY)}
    assert plan.orders[0].action == Action.SELL  # trims come first


def test_plan_targets_closes_sells_and_skips_small_drift(vol_provider, rules):
    rules.max_position_pct = 0.5
    cfg = PortfolioConfig(method="equal", long_exposure=0.9, rebalance_threshold=0.05)
    px = {t: vol_provider.latest_price(t) for t in ("LOW", "MID", "HIGH")}
    eq = 100_000
    held = {t: int(0.30 * eq / px[t]) for t in ("LOW", "MID")}
    held["HIGH"] = int(0.10 * eq / px["HIGH"])
    p = Portfolio(cash=eq - sum(held[t] * px[t] for t in held), total_equity=eq,
                  holdings=[Holding(t, n, px[t], n * px[t]) for t, n in held.items()])
    signals = sigs(vol_provider)
    signals[2] = Signal("HIGH", Action.SELL, 0.7, -0.7, "r", "t", price=px["HIGH"])
    plan = plan_targets(signals, p, ctx(vol_provider), cfg, rules, min_confidence=0.4)
    # HIGH is closed; LOW/MID go 30% -> 45%, a drift above the 5% threshold.
    assert plan.weights["HIGH"] == 0 and plan.shares["HIGH"] == 0
    assert {(o.ticker, o.action) for o in plan.orders} >= {("HIGH", Action.SELL), ("LOW", Action.BUY)}
    # Now nearly on target: nothing to do.
    cfg.long_exposure = 0.62
    plan = plan_targets(sigs(vol_provider)[:2], p, ctx(vol_provider), cfg, rules, min_confidence=0.4)
    assert [o for o in plan.orders if o.ticker in ("LOW", "MID")] == []


def test_plan_targets_short_book(vol_provider):
    from smg_trader.config import RulesConfig

    rules = RulesConfig(allow_short_selling=True, max_trades_per_run=20)
    cfg = PortfolioConfig(method="inverse_vol", long_exposure=0.5, short_exposure=0.3, max_short_positions=2)
    p = Portfolio(cash=100_000, total_equity=100_000)
    plan = plan_targets(sigs(vol_provider, action=Action.SELL), p, ctx(vol_provider), cfg, rules,
                        min_confidence=0.4)
    shorts = {t: w for t, w in plan.weights.items() if w < 0}
    assert len(shorts) == 2 and sum(shorts.values()) == pytest.approx(-0.3)
    assert all(o.action == Action.SHORT for o in plan.orders)
    # Shorting off: no short book.
    rules.allow_short_selling = False
    plan = plan_targets(sigs(vol_provider, action=Action.SELL), p, ctx(vol_provider), cfg, rules,
                        min_confidence=0.4)
    assert plan.orders == []


def test_unknown_method(vol_provider, rules):
    with pytest.raises(ValueError, match="Unknown portfolio method"):
        book_weights("magic", sigs(vol_provider), ctx(vol_provider), PortfolioConfig())


def test_returns_alignment_ignores_missing_days():
    idx = pd.bdate_range(end=END_DATE, periods=80)
    a = make_prices(0.001, n=80, seed=1)
    b = make_prices(0.001, n=80, seed=2).drop(idx[10:15])
    p = InMemoryProvider({"A": a, "B": b})
    s = [Signal(t, Action.BUY, 0.6, 0.6, "r", "t", price=10.0) for t in ("A", "B")]
    w = book_weights("risk_parity", s, ctx(p), PortfolioConfig())
    assert sum(w.values()) == pytest.approx(1)
