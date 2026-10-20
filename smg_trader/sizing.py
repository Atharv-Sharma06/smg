"""Turn signals or target positions into concrete, whole-share orders."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable

from .config import RulesConfig
from .executor import apply_order
from .models import Action, Portfolio, Signal, TradeOrder
from .rules import spendable_cash

log = logging.getLogger(__name__)


def _apply(p: Portfolio, o: TradeOrder, rules: RulesConfig) -> Portfolio:
    return apply_order(p, o, rules.commission_rate, short_margin_requirement=rules.short_margin_requirement)


def build_orders(
    signals: list[Signal],
    portfolio: Portfolio,
    rules: RulesConfig,
    *,
    min_confidence: float,
    short_exposure: float = 0.0,
    exclude: Iterable[str] = (),
) -> list[TradeOrder]:
    """Signal-by-signal sizing (``portfolio.method: signal``).

    * SELL signal on a long position: sell it all. BUY signal on a short: cover it all.
    * BUY: target ``max_position_pct * confidence`` of equity, topped up from the
      current position, capped by spendable cash.
    * SELL signal on a ticker not held, when short selling is enabled and
      ``short_exposure > 0``: short ``max_position_pct * confidence`` of equity,
      within the short book and the collateral available.

    Closing orders come first (they free up cash), then new positions by
    descending confidence. Tickers in ``exclude`` (e.g. pair legs) are skipped.
    """
    skip = {t.upper() for t in exclude}
    actionable = [s for s in signals if s.confidence >= min_confidence and s.price and s.ticker not in skip]
    actionable.sort(key=lambda s: -s.confidence)

    orders: list[TradeOrder] = []
    for sig in actionable:
        if sig.action == Action.SELL and (held := portfolio.shares_of(sig.ticker, "LONG")):
            orders.append(TradeOrder(sig.ticker, Action.SELL, held, float(sig.price), sig.confidence, sig.reason))
        elif sig.action == Action.BUY and (short := portfolio.shares_of(sig.ticker, "SHORT")):
            orders.append(TradeOrder(sig.ticker, Action.COVER, short, float(sig.price), sig.confidence, sig.reason))

    sim = portfolio
    for o in orders:
        sim = _apply(sim, o, rules)
    budget = spendable_cash(sim, rules)
    closed = {o.ticker for o in orders}

    equity = portfolio.total_equity
    can_short = rules.allow_short_selling and short_exposure > 0
    short_room = equity * short_exposure - sim.short_value
    fee = rules.commission_rate
    for sig in actionable:
        if sig.ticker in closed:
            continue
        price = float(sig.price)
        target_value = equity * rules.max_position_pct * min(1.0, sig.confidence)
        if sig.action == Action.BUY:
            if price < rules.min_share_price:
                log.info("skip BUY %s: price $%.2f below SMG minimum", sig.ticker, price)
                continue
            current = sim.holding(sig.ticker, "LONG")
            room = target_value - (current.market_value if current else 0.0)
            shares = math.floor(min(room, budget) / (price * (1 + fee)))
            action = Action.BUY
            cost_per_share = price * (1 + fee)
        elif sig.action == Action.SELL and can_short and not sim.shares_of(sig.ticker, "LONG"):
            if price < rules.min_short_price:
                log.info("skip SHORT %s: price $%.2f below short minimum", sig.ticker, price)
                continue
            current = sim.holding(sig.ticker, "SHORT")
            room = min(target_value - (current.market_value if current else 0.0), short_room)
            cost_per_share = price * (rules.short_margin_requirement + fee)  # collateral per share
            shares = math.floor(min(room / price, budget / cost_per_share)) if room > 0 else 0
            action = Action.SHORT
        else:
            continue
        if shares < rules.min_shares_per_trade:
            log.info("skip %s %s: only %d shares fit (min %d)", action.value, sig.ticker, max(shares, 0),
                     rules.min_shares_per_trade)
            continue
        orders.append(TradeOrder(sig.ticker, action, shares, price, sig.confidence, sig.reason))
        budget -= shares * cost_per_share
        if action == Action.SHORT:
            short_room -= shares * price

    return orders[: max(0, rules.max_trades_per_run)]


def orders_for_targets(
    targets: dict[str, int],
    portfolio: Portfolio,
    prices: dict[str, float],
    rules: RulesConfig,
    *,
    reasons: dict[str, str] | None = None,
    confidences: dict[str, float] | None = None,
    group_ids: dict[str, str] | None = None,
) -> list[TradeOrder]:
    """Orders that move each ticker from its current position to a target.

    ``targets`` holds signed share counts (negative = short). A flip from long
    to short becomes SELL then SHORT. Orders smaller than the SMG minimum are
    dropped unless they close a position; if one leg of a linked group is
    dropped, the whole group is.
    """
    reasons, confidences, group_ids = reasons or {}, confidences or {}, group_ids or {}
    closing: list[TradeOrder] = []
    opening: list[TradeOrder] = []
    dropped_groups: set[str] = set()
    for ticker, target in targets.items():
        cur_long = portfolio.shares_of(ticker, "LONG")
        cur_short = portfolio.shares_of(ticker, "SHORT")
        price = prices.get(ticker)
        if price is None:
            h = portfolio.holding(ticker, "LONG") or portfolio.holding(ticker, "SHORT")
            price = h.current_price if h else None
        if not price:
            log.warning("no price for %s; leaving it alone", ticker)
            continue
        tgt_long, tgt_short = max(target, 0), max(-target, 0)
        gid = group_ids.get(ticker, "")

        def make(action: Action, n: int) -> TradeOrder:
            return TradeOrder(ticker, action, n, float(price), confidences.get(ticker, 0.0),
                              reasons.get(ticker, "rebalance to target"), group_id=gid)

        legs = []
        if cur_long > tgt_long:
            legs.append((closing, make(Action.SELL, cur_long - tgt_long), cur_long))
        if cur_short > tgt_short:
            legs.append((closing, make(Action.COVER, cur_short - tgt_short), cur_short))
        if tgt_long > cur_long:
            legs.append((opening, make(Action.BUY, tgt_long - cur_long), 0))
        if tgt_short > cur_short:
            legs.append((opening, make(Action.SHORT, tgt_short - cur_short), 0))
        for bucket, order, held in legs:
            closes_all = not order.action.opens and order.shares == held
            if order.shares < rules.min_shares_per_trade and not closes_all:
                log.info("skip %s %d %s: below the %d-share minimum", order.action.value, order.shares,
                         ticker, rules.min_shares_per_trade)
                if gid:
                    dropped_groups.add(gid)
                continue
            bucket.append(order)
    out = closing + opening
    if dropped_groups:
        log.info("dropping linked orders %s: a leg was too small", ", ".join(sorted(dropped_groups)))
        out = [o for o in out if o.group_id not in dropped_groups]
    return out


def fit_to_budget(orders: list[TradeOrder], portfolio: Portfolio, rules: RulesConfig, *,
                  reserve: float = 0.0) -> list[TradeOrder]:
    """Shrink or drop opening orders that don't fit in spendable cash minus ``reserve``.

    Closing orders always pass (and their proceeds count). BUYs cost their
    notional plus fee; SHORTs cost their collateral plus fee. A linked order
    that doesn't fit is dropped with its whole group rather than resized.
    """
    sim = portfolio
    for o in orders:
        if not o.action.opens:
            sim = _apply(sim, o, rules)
    budget = spendable_cash(sim, rules) - reserve
    fee = rules.commission_rate
    out: list[TradeOrder] = []
    dropped: set[str] = set()
    for o in orders:
        if not o.action.opens:
            out.append(o)
            continue
        per_share = o.price * ((1 + fee) if o.action == Action.BUY else (rules.short_margin_requirement + fee))
        cost = o.shares * per_share
        if cost <= budget:
            out.append(o)
            budget -= cost
            continue
        fit = int(max(0.0, budget) // per_share)
        if o.group_id or fit < rules.min_shares_per_trade:
            log.info("skip %s %d %s: needs $%.0f, $%.0f left after reserving $%.0f", o.action.value, o.shares,
                     o.ticker, cost, max(0.0, budget), reserve)
            if o.group_id:
                dropped.add(o.group_id)
            continue
        log.info("resize %s %s: %d -> %d shares to fit cash", o.action.value, o.ticker, o.shares, fit)
        o.shares = fit
        out.append(o)
        budget -= fit * per_share
    return [o for o in out if o.group_id not in dropped]
