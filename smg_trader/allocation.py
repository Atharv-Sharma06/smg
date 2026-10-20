"""Portfolio construction: signals -> target weights -> rebalancing orders.

Used when ``portfolio.method`` is not ``signal``. The flow is:

1. **Select** a long book (BUY signals plus held longs that aren't SELL) and,
   if shorting is enabled, a short book (SELL signals plus held shorts that
   aren't BUY), each ranked by score and capped at ``max_positions``.
2. **Weight** each book with one of the methods below.
3. **Scale** the books to ``long_exposure`` / ``short_exposure`` of equity and
   cap every name at ``rules.max_position_pct`` (the excess is spread over the
   uncapped names).
4. **Rebalance**: convert weights to whole shares and diff against current
   holdings. Positions that drifted above target get trimmed, names that left
   the book get closed, and drifts under ``rebalance_threshold`` are left alone.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import PortfolioConfig, RulesConfig
from .models import Action, Portfolio, Signal, TradeOrder
from .sizing import orders_for_targets
from .strategies.base import StrategyContext

log = logging.getLogger(__name__)

METHODS = ("equal", "confidence", "inverse_vol", "risk_parity", "mean_variance")
MIN_OBSERVATIONS = 20


@dataclass
class TargetPlan:
    weights: dict[str, float] = field(default_factory=dict)   # signed fraction of equity
    shares: dict[str, int] = field(default_factory=dict)      # signed whole shares
    orders: list[TradeOrder] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- statistics
def returns_frame(ctx: StrategyContext, tickers: list[str], lookback: int) -> pd.DataFrame:
    """Aligned daily returns over the last ``lookback`` trading days."""
    closes = pd.DataFrame({t: ctx.closes(t) for t in tickers})
    rets = closes.pct_change(fill_method=None).iloc[-lookback:]
    return rets.dropna(axis=1, thresh=MIN_OBSERVATIONS).dropna()


def shrunk_covariance(rets: pd.DataFrame, shrinkage: float) -> np.ndarray:
    sample = rets.cov().to_numpy()
    target = np.diag(np.diag(sample))
    cov = (1 - shrinkage) * sample + shrinkage * target
    return cov + np.eye(len(cov)) * 1e-10


def risk_contributions(w: np.ndarray, cov: np.ndarray) -> np.ndarray:
    port_var = float(w @ cov @ w)
    return w * (cov @ w) / port_var if port_var > 0 else np.full(len(w), 1 / len(w))


def risk_parity_weights(cov: np.ndarray, iterations: int = 2000, tol: float = 1e-8) -> np.ndarray:
    """Equal risk contribution weights (long-only, sum to 1)."""
    n = len(cov)
    w = 1 / np.sqrt(np.diag(cov))
    w /= w.sum()
    for _ in range(iterations):
        rc = w * (cov @ w)
        if np.max(np.abs(rc / rc.sum() - 1 / n)) < tol:
            break
        w = w * np.sqrt(rc.mean() / rc)
        w /= w.sum()
    return w


def mean_variance_weights(cov: np.ndarray, mu: np.ndarray) -> np.ndarray:
    """Long-only maximum-Sharpe direction (w ~ cov^-1 mu), via an active set."""
    active = np.ones(len(mu), dtype=bool)
    w = np.zeros(len(mu))
    for _ in range(len(mu)):
        idx = np.flatnonzero(active)
        if not len(idx):
            break
        sol = np.linalg.solve(cov[np.ix_(idx, idx)], mu[idx])
        if (sol > 0).all():
            w[:] = 0
            w[idx] = sol
            break
        active[idx[sol <= 0]] = False
    if w.sum() <= 0:  # nothing has positive expected return after hedging; fall back
        w = np.clip(mu, 0, None)
    return w / w.sum() if w.sum() > 0 else np.full(len(mu), 1 / len(mu))


def book_weights(method: str, sigs: list[Signal], ctx: StrategyContext, cfg: PortfolioConfig) -> dict[str, float]:
    """Weights (non-negative, summing to 1) for one book."""
    if not sigs:
        return {}
    tickers = [s.ticker for s in sigs]
    if method == "equal":
        return {t: 1 / len(tickers) for t in tickers}
    if method == "confidence":
        conf = np.array([max(s.confidence, 1e-6) for s in sigs])
        return dict(zip(tickers, conf / conf.sum()))

    rets = returns_frame(ctx, tickers, cfg.cov_lookback_days)
    usable = [t for t in tickers if t in rets.columns]
    if len(rets) < MIN_OBSERVATIONS or not usable:
        log.info("%s: not enough return history; using equal weights", method)
        return {t: 1 / len(tickers) for t in tickers}
    if len(usable) < len(tickers):
        log.info("%s: no return history for %s; leaving them out", method,
                 ", ".join(sorted(set(tickers) - set(usable))))
    rets = rets[usable]
    cov = shrunk_covariance(rets, cfg.cov_shrinkage)
    vol = np.sqrt(np.diag(cov))
    if method == "inverse_vol":
        w = 1 / vol
    elif method == "risk_parity":
        w = risk_parity_weights(cov)
    elif method == "mean_variance":
        score = {s.ticker: abs(s.score) for s in sigs}
        # Expected daily return proxy: |score| x volatility (a fixed information ratio per unit score).
        mu = np.array([score[t] for t in usable]) * vol
        w = mean_variance_weights(cov, mu)
    else:
        raise ValueError(f"Unknown portfolio method {method!r}; choose from signal, {', '.join(METHODS)}")
    w = w / w.sum()
    return dict(zip(usable, w))


def cap_weights(weights: dict[str, float], exposure: float, cap: float) -> dict[str, float]:
    """Scale to ``exposure`` with no name above ``cap``; redistribute the excess."""
    out = {t: w * exposure for t, w in weights.items()}
    for _ in range(len(out) + 1):
        over = {t for t, w in out.items() if w > cap + 1e-12}
        if not over:
            break
        excess = sum(out[t] - cap for t in over)
        for t in over:
            out[t] = cap
        free = {t: w for t, w in out.items() if w < cap - 1e-12}
        total_free = sum(free.values())
        if not free or total_free <= 0:
            break  # everyone is capped; the rest stays in cash
        for t, w in free.items():
            out[t] = w + excess * w / total_free
    return out


# ---------------------------------------------------------------- selection
def select_books(
    signals: list[Signal],
    portfolio: Portfolio,
    cfg: PortfolioConfig,
    rules: RulesConfig,
    *,
    min_confidence: float,
    exclude: Iterable[str] = (),
) -> tuple[list[Signal], list[Signal], set[str]]:
    """Returns (long book, short book, tickers that have a signal and are eligible to be closed)."""
    skip = {t.upper() for t in exclude}
    by_ticker = {s.ticker: s for s in signals if s.ticker not in skip and s.price}
    held_long = {h.ticker for h in portfolio.holdings if h.position != "SHORT"} - skip
    held_short = {h.ticker for h in portfolio.holdings if h.position == "SHORT"} - skip

    longs = [s for s in by_ticker.values()
             if (s.action == Action.BUY and s.confidence >= min_confidence and s.price >= rules.min_share_price)
             or (s.ticker in held_long and s.action != Action.SELL)]
    longs.sort(key=lambda s: -s.score)
    longs = longs[: cfg.max_positions]

    shorts: list[Signal] = []
    if rules.allow_short_selling and cfg.short_exposure > 0:
        long_set = {s.ticker for s in longs}
        shorts = [s for s in by_ticker.values() if s.ticker not in long_set and (
            (s.action == Action.SELL and s.confidence >= min_confidence and s.price >= rules.min_short_price)
            or (s.ticker in held_short and s.action != Action.BUY))]
        shorts.sort(key=lambda s: s.score)
        shorts = shorts[: cfg.max_short_positions]
    elif cfg.short_exposure > 0:
        log.warning("portfolio.short_exposure > 0 but rules.allow_short_selling is false; no shorts")
    return longs, shorts, (held_long | held_short) & set(by_ticker)


def plan_targets(
    signals: list[Signal],
    portfolio: Portfolio,
    ctx: StrategyContext,
    cfg: PortfolioConfig,
    rules: RulesConfig,
    *,
    min_confidence: float,
    exclude: Iterable[str] = (),
) -> TargetPlan:
    plan = TargetPlan()
    longs, shorts, closable = select_books(signals, portfolio, cfg, rules,
                                           min_confidence=min_confidence, exclude=exclude)
    equity = portfolio.total_equity
    if equity <= 0:
        plan.notes.append("no equity")
        return plan

    long_w = cap_weights(book_weights(cfg.method, longs, ctx, cfg), cfg.long_exposure, rules.max_position_pct)
    short_w = cap_weights(book_weights(cfg.method, shorts, ctx, cfg), cfg.short_exposure, rules.max_position_pct)
    plan.weights = {**long_w, **{t: -w for t, w in short_w.items()}}

    sig_by = {s.ticker: s for s in signals}
    prices = {t: float(s.price) for t, s in sig_by.items() if s.price}
    for t in closable:
        plan.weights.setdefault(t, 0.0)

    for t, w in plan.weights.items():
        price = prices.get(t)
        if not price:
            continue
        target = int(math.floor(abs(w) * equity / price)) * (1 if w >= 0 else -1)
        current = portfolio.net_shares(t)
        same_side = (target > 0 and current > 0) or (target < 0 and current < 0)
        drift = abs(target - current) * price / equity
        if same_side and drift < cfg.rebalance_threshold:
            target = current  # close enough; don't churn
        elif target != 0 and abs(target - current) * price < cfg.min_trade_value:
            target = current
        plan.shares[t] = target

    reasons = {t: f"{cfg.method} target {plan.weights[t]:+.1%} of equity: {sig_by[t].reason[:200]}"
               for t in plan.shares if t in sig_by}
    conf = {t: sig_by[t].confidence for t in plan.shares if t in sig_by}
    plan.orders = orders_for_targets(plan.shares, portfolio, prices, rules, reasons=reasons, confidences=conf)
    log.info("Target portfolio (%s): %s", cfg.method,
             ", ".join(f"{t} {w:+.1%}" for t, w in sorted(plan.weights.items(), key=lambda kv: -kv[1]) if w)
             or "all cash")
    return plan
