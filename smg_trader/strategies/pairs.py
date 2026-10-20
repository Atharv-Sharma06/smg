"""Pairs trading (statistical arbitrage) on configured ticker pairs.

For a pair (A, B) over the last ``lookback_days``:

* hedge ratio  beta = OLS slope of log(A) on log(B)
* spread       s = log(A) - beta * log(B)
* z-score      (s_today - mean(s)) / std(s)

When z >= ``entry_z`` A is rich relative to B: **short A, buy B** (short the
spread). When z <= -``entry_z``: **buy A, short B**. The position is closed
when |z| falls back under ``exit_z``, or as a stop when |z| reaches ``stop_z``.

A pair is only traded while it looks mean-reverting: daily-return
correlation >= ``min_correlation`` and spread half-life <= ``max_half_life``
days (from an AR(1) fit of the spread). Both legs go out as linked orders, so
the executor sends both or neither.

Sizing is roughly dollar-neutral: each pair gets ``capital_pct * equity /
n_pairs`` of gross capital, split A : B = 1 : beta. Open pairs are not resized
day to day; they're only traded when the state changes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..config import PairsConfig, RulesConfig
from ..models import Action, Portfolio, Signal, TradeOrder
from ..sizing import orders_for_targets
from .base import StrategyContext

log = logging.getLogger(__name__)


@dataclass
class SpreadStats:
    hedge_ratio: float
    zscore: float
    correlation: float
    half_life: float


@dataclass
class PairState:
    a: str
    b: str
    current: int = 0   # +1 long spread (long A, short B), -1 short spread, 0 flat
    target: int = 0
    stats: SpreadStats | None = None
    tradable: bool = False
    reason: str = ""

    @property
    def group_id(self) -> str:
        return f"pair:{self.a}/{self.b}"


def spread_stats(a: pd.Series, b: pd.Series, lookback: int) -> SpreadStats | None:
    df = pd.concat({"a": a, "b": b}, axis=1).dropna()
    df = df[(df > 0).all(axis=1)].iloc[-(lookback + 1):]
    if len(df) < 20:
        return None
    la, lb = np.log(df["a"]), np.log(df["b"])
    var_b = float(lb.var())
    if var_b <= 0:
        return None
    beta = float(((la - la.mean()) * (lb - lb.mean())).sum() / ((len(lb) - 1) * var_b))
    spread = la - beta * lb
    std = float(spread.std())
    if not std or math.isnan(std):
        return None
    z = float((spread.iloc[-1] - spread.mean()) / std)
    corr = float(df["a"].pct_change().corr(df["b"].pct_change()))
    # AR(1) on the spread: ds_t = lam * (s_{t-1} - mean) + e  ->  half-life = -ln 2 / lam
    lagged = (spread.shift(1) - spread.mean()).iloc[1:]
    ds = spread.diff().iloc[1:]
    lam = float((lagged * ds).sum() / (lagged ** 2).sum()) if (lagged ** 2).sum() else 0.0
    half_life = -math.log(2) / lam if lam < 0 else math.inf
    return SpreadStats(beta, z, corr if not math.isnan(corr) else 0.0, half_life)


class PairsTrader:
    def __init__(self, cfg: PairsConfig, rules: RulesConfig):
        self.cfg = cfg
        self.rules = rules
        self.pairs = [(a.upper(), b.upper()) for a, b in (p[:2] for p in cfg.pairs)]

    @property
    def tickers(self) -> set[str]:
        return {t for pair in self.pairs for t in pair}

    @staticmethod
    def current_state(a: str, b: str, portfolio: Portfolio) -> int:
        na, nb = portfolio.net_shares(a), portfolio.net_shares(b)
        if na < 0 or (na == 0 and nb > 0):
            return -1
        if na > 0 or (na == 0 and nb < 0):
            return 1
        return 0

    def evaluate(self, a: str, b: str, ctx: StrategyContext, portfolio: Portfolio) -> PairState:
        cfg = self.cfg
        st = PairState(a, b, current=self.current_state(a, b, portfolio))
        stats = spread_stats(ctx.closes(a), ctx.closes(b), cfg.lookback_days)
        st.stats = stats
        if stats is None:
            st.target = st.current  # don't trade blind
            st.reason = "not enough price history"
            return st
        st.tradable = (stats.hedge_ratio > 0 and stats.correlation >= cfg.min_correlation
                       and stats.half_life <= cfg.max_half_life)
        z = stats.zscore
        desc = (f"z {z:+.2f}, beta {stats.hedge_ratio:.2f}, corr {stats.correlation:.2f}, "
                f"half-life {stats.half_life:.1f}d")

        if st.current == 0:
            if not st.tradable:
                st.reason = f"flat, not mean-reverting ({desc})"
            elif cfg.entry_z <= z < cfg.stop_z:
                st.target, st.reason = -1, f"enter: {a} rich vs {b} ({desc})"
            elif -cfg.stop_z < z <= -cfg.entry_z:
                st.target, st.reason = 1, f"enter: {a} cheap vs {b} ({desc})"
            else:
                st.reason = f"flat, no entry ({desc})"
            return st

        spread_dir = -st.current  # a short spread (-1) profits when z falls
        if abs(z) >= cfg.stop_z:
            st.target, st.reason = 0, f"stop: spread kept widening ({desc})"
        elif not st.tradable:
            st.target, st.reason = 0, f"close: pair stopped looking mean-reverting ({desc})"
        elif z * spread_dir <= cfg.exit_z:
            st.target, st.reason = 0, f"exit: spread reverted ({desc})"
        else:
            st.target, st.reason = st.current, f"hold ({desc})"
        return st

    def plan(self, ctx: StrategyContext, portfolio: Portfolio) -> tuple[list[TradeOrder], list[Signal], list[PairState]]:
        """Orders (linked per pair), one Signal per leg for logging, and the pair states."""
        if not self.pairs:
            return [], [], []
        if not self.rules.allow_short_selling:
            log.warning("pairs trading needs rules.allow_short_selling: true; skipping %d pairs", len(self.pairs))
            return [], [], []

        orders: list[TradeOrder] = []
        signals: list[Signal] = []
        states: list[PairState] = []
        capital = portfolio.total_equity * self.cfg.capital_pct / len(self.pairs)
        for a, b in self.pairs:
            st = self.evaluate(a, b, ctx, portfolio)
            states.append(st)
            log.info("PAIR %s/%s: %+d -> %+d  %s", a, b, st.current, st.target, st.reason)
            signals.extend(self._leg_signals(st, ctx))
            pa, pb = ctx.last_price(a), ctx.last_price(b)
            if pa is None or pb is None:
                continue
            legs_ok = self._legs_consistent(st, portfolio)
            if st.target == st.current and legs_ok:
                continue
            if st.target == 0:
                targets = {a: 0, b: 0}
            else:
                beta = st.stats.hedge_ratio if st.stats else 1.0
                leg_a, leg_b = capital / (1 + beta), capital * beta / (1 + beta)
                # Keep each leg inside the per-position limit, or the validator rejects the pair.
                cap = self.rules.max_position_pct * portfolio.total_equity
                scale = min(1.0, cap / max(leg_a, leg_b))
                if scale < 1:
                    log.info("PAIR %s/%s: scaled to %.0f%% to stay under max_position_pct", a, b, scale * 100)
                na = int(leg_a * scale / pa)
                nb = int(leg_b * scale / pb)
                if min(na, nb) < self.rules.min_shares_per_trade:
                    log.info("PAIR %s/%s: $%.0f is too small for %d-share legs", a, b, capital,
                             self.rules.min_shares_per_trade)
                    continue
                targets = {a: na * st.target, b: -nb * st.target}
            reason = f"pairs {a}/{b}: {st.reason}"
            conf = min(1.0, abs(st.stats.zscore) / self.cfg.stop_z) if st.stats else 0.0
            orders += orders_for_targets(targets, portfolio, {a: pa, b: pb}, self.rules,
                                         reasons={a: reason, b: reason}, confidences={a: conf, b: conf},
                                         group_ids={a: st.group_id, b: st.group_id})
        return orders, signals, states

    @staticmethod
    def _legs_consistent(st: PairState, portfolio: Portfolio) -> bool:
        na, nb = portfolio.net_shares(st.a), portfolio.net_shares(st.b)
        if st.current == 0:
            return na == 0 and nb == 0
        return na * st.current > 0 and nb * st.current < 0

    def _leg_signals(self, st: PairState, ctx: StrategyContext) -> list[Signal]:
        z = st.stats.zscore if st.stats else 0.0
        conf = round(min(1.0, abs(z) / self.cfg.stop_z), 4)
        out = []
        for ticker, sign in ((st.a, 1), (st.b, -1)):
            want = st.target * sign  # +1 long this leg, -1 short it
            action = Action.HOLD if st.target == st.current else (
                Action.BUY if want > 0 else Action.SELL if want < 0 else Action.HOLD)
            out.append(Signal(ticker, action, conf, round(max(-1.0, min(1.0, -z * sign / self.cfg.stop_z)), 4),
                              f"{st.group_id} {st.current:+d}->{st.target:+d}: {st.reason}", "pairs",
                              price=ctx.last_price(ticker), details={"pair": st.group_id, "zscore": z}))
        return out
