"""Turn strategy signals into concrete, whole-share orders."""

from __future__ import annotations

import logging
import math

from .config import RulesConfig
from .models import Action, Portfolio, Signal, TradeOrder

log = logging.getLogger(__name__)


def build_orders(
    signals: list[Signal],
    portfolio: Portfolio,
    rules: RulesConfig,
    *,
    min_confidence: float,
) -> list[TradeOrder]:
    """Sells first (they free up cash), then buys by descending confidence.

    * SELL a held ticker: sell the whole position.
    * BUY: target ``max_position_pct * confidence`` of equity, topped up from the
      current position, capped by remaining cash; skipped if that is fewer than
      the minimum shares.
    """
    orders: list[TradeOrder] = []
    actionable = [s for s in signals if s.confidence >= min_confidence and s.price]

    for sig in sorted((s for s in actionable if s.action == Action.SELL), key=lambda s: -s.confidence):
        held = portfolio.shares_of(sig.ticker)
        if held > 0:
            orders.append(TradeOrder(sig.ticker, Action.SELL, held, float(sig.price),
                                     sig.confidence, sig.reason))

    cash = portfolio.cash - rules.cash_reserve
    if rules.use_margin:
        cash += rules.margin_limit
    # Proceeds from the sells above are available for buys in SMG.
    cash += sum(o.notional * (1 - rules.commission_rate) for o in orders)

    equity = portfolio.total_equity
    for sig in sorted((s for s in actionable if s.action == Action.BUY), key=lambda s: -s.confidence):
        price = float(sig.price)
        if price < rules.min_share_price:
            log.info("skip BUY %s: price $%.2f below SMG minimum", sig.ticker, price)
            continue
        target_value = equity * rules.max_position_pct * min(1.0, sig.confidence)
        current = portfolio.holding(sig.ticker)
        room = target_value - (current.market_value if current else 0.0)
        budget = min(room, cash)
        shares = math.floor(budget / (price * (1 + rules.commission_rate)))
        if shares < rules.min_shares_per_trade:
            log.info("skip BUY %s: only %d shares fit (min %d)", sig.ticker, max(shares, 0),
                     rules.min_shares_per_trade)
            continue
        orders.append(TradeOrder(sig.ticker, Action.BUY, shares, price, sig.confidence, sig.reason))
        cash -= shares * price * (1 + rules.commission_rate)

    return orders[: max(0, rules.max_trades_per_run)]
