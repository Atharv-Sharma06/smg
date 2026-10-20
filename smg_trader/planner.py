"""One place that turns signals into orders, shared by live runs and backtests."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .allocation import plan_targets
from .config import PairsConfig, PortfolioConfig, RulesConfig
from .executor import apply_order
from .models import Portfolio, Signal, TradeOrder
from .sizing import build_orders, fit_to_budget
from .strategies.base import StrategyContext
from .strategies.pairs import PairsTrader, PairState

log = logging.getLogger(__name__)


@dataclass
class Plan:
    orders: list[TradeOrder] = field(default_factory=list)
    pair_signals: list[Signal] = field(default_factory=list)
    pair_states: list[PairState] = field(default_factory=list)
    target_weights: dict[str, float] | None = None


def plan_orders(
    signals: list[Signal],
    portfolio: Portfolio,
    ctx: StrategyContext,
    *,
    rules: RulesConfig,
    portfolio_cfg: PortfolioConfig,
    pairs_cfg: PairsConfig,
    min_confidence: float,
) -> Plan:
    """Pair trades first, then the directional book sized on what's left.

    Pair tickers are kept out of the directional book so the two never fight
    over the same position, and the part of ``pairs.capital_pct`` not yet in
    use is held back from directional buys so a pair can still open later.
    Orders come out as: directional closes, pair trades, directional opens.
    """
    plan = Plan()
    pair_tickers: set[str] = set()
    sim = portfolio
    if pairs_cfg.enabled and pairs_cfg.pairs:
        trader = PairsTrader(pairs_cfg, rules)
        pair_tickers = trader.tickers
        pair_orders, plan.pair_signals, plan.pair_states = trader.plan(ctx, portfolio)
        for o in pair_orders:
            sim = apply_order(sim, o, rules.commission_rate, short_margin_requirement=rules.short_margin_requirement)
    else:
        pair_orders = []

    reserve = 0.0
    if pair_tickers:
        deployed = sum(h.market_value for h in sim.holdings if h.ticker in pair_tickers)
        reserve = max(0.0, pairs_cfg.capital_pct * sim.total_equity - deployed)

    if portfolio_cfg.method == "signal":
        directional = build_orders(signals, sim, rules, min_confidence=min_confidence,
                                   short_exposure=portfolio_cfg.short_exposure, exclude=pair_tickers)
    else:
        target = plan_targets(signals, sim, ctx, portfolio_cfg, rules, min_confidence=min_confidence,
                              exclude=pair_tickers)
        directional = target.orders
        plan.target_weights = target.weights
    directional = fit_to_budget(directional, sim, rules, reserve=reserve)

    closes = [o for o in directional if not o.action.opens]
    opens = [o for o in directional if o.action.opens]
    plan.orders = closes + pair_orders + opens
    return plan
