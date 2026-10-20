"""SMG rule validation for a single order.

Every check appends a PASS/FAIL/WARN line to ``ValidationResult.checks`` so
the executor can print a full validation log, not just the first error.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from .config import RulesConfig
from .lock import TraderLock
from .market_hours import is_market_open, market_status, now_et
from .models import Action, Portfolio, TradeOrder, ValidationResult

log = logging.getLogger(__name__)


def spendable_cash(portfolio: Portfolio, cfg: RulesConfig) -> float:
    """Cash available for new buys or new short collateral.

    Short-sale proceeds sit in cash but can't be spent, and SMG holds
    ``short_margin_requirement`` of the short value on top as collateral.
    When SMG reports its own buying power, never go above it.
    """
    spend = (portfolio.cash - cfg.cash_reserve
             - portfolio.short_value * (1 + cfg.short_margin_requirement))
    if cfg.use_margin:
        spend += cfg.margin_limit
    if portfolio.buying_power is not None:
        spend = min(spend, portfolio.buying_power - cfg.cash_reserve)
    return max(0.0, spend)


class RuleValidator:
    def __init__(
        self,
        cfg: RulesConfig,
        exchange_lookup: Callable[[str], str | None],
        *,
        lock: TraderLock | None = None,
        clock: Callable[[], datetime] = now_et,
    ):
        self.cfg = cfg
        self.exchange_lookup = exchange_lookup
        self.lock = lock
        self.clock = clock

    def max_spend(self, portfolio: Portfolio) -> float:
        return spendable_cash(portfolio, self.cfg)

    def validate(
        self,
        order: TradeOrder,
        portfolio: Portfolio,
        *,
        trades_this_run: int = 0,
        check_hours: bool = True,
    ) -> ValidationResult:
        res = ValidationResult(order=order)
        cfg = self.cfg

        def check(ok: bool, passed: str, failed: str) -> None:
            if ok:
                res.checks.append(f"PASS {passed}")
            else:
                res.checks.append(f"FAIL {failed}")
                res.errors.append(failed)

        def warn(msg: str) -> None:
            res.checks.append(f"WARN {msg}")
            res.warnings.append(msg)

        action = order.action
        long_held = portfolio.shares_of(order.ticker, "LONG")
        short_held = portfolio.shares_of(order.ticker, "SHORT")
        held = short_held if action.position == "SHORT" else long_held
        whole = isinstance(order.shares, int) and not isinstance(order.shares, bool)

        check(action in (Action.BUY, Action.SELL, Action.SHORT, Action.COVER), f"action {action.value}",
              f"unsupported action {action.value}")

        # Whole shares only.
        check(whole and order.shares > 0, f"whole shares ({order.shares})",
              f"shares must be a positive whole number, got {order.shares!r}")

        # Exchange must be NYSE or NASDAQ.
        exchange = order.exchange or self.exchange_lookup(order.ticker)
        order.exchange = exchange
        allowed = {e.upper() for e in cfg.allowed_exchanges}
        if exchange is None:
            check(False, "", f"could not verify exchange for {order.ticker} (allowed: {', '.join(sorted(allowed))})")
        else:
            check(exchange.upper() in allowed, f"exchange {exchange}",
                  f"{order.ticker} trades on {exchange}; SMG allows only {', '.join(sorted(allowed))}")

        # Price minimums apply when opening a position; closing one is always allowed.
        if action == Action.BUY:
            check(order.price >= cfg.min_share_price, f"price ${order.price:.2f} >= ${cfg.min_share_price:.2f}",
                  f"price ${order.price:.2f} is below the ${cfg.min_share_price:.2f} minimum")
        elif action == Action.SHORT:
            check(order.price >= cfg.min_short_price,
                  f"price ${order.price:.2f} >= ${cfg.min_short_price:.2f} short minimum",
                  f"price ${order.price:.2f} is below the ${cfg.min_short_price:.2f} short-sale minimum")
        elif order.price < cfg.min_share_price:
            warn(f"price ${order.price:.2f} is below ${cfg.min_share_price:.2f} (closing is allowed)")

        # 10-share minimum. Closing out a smaller position is allowed.
        if whole and order.shares < cfg.min_shares_per_trade:
            if not action.opens and order.shares == held:
                warn(f"{order.shares} shares < {cfg.min_shares_per_trade} minimum, but it closes the position")
            else:
                check(False, "", f"{order.shares} shares is below the {cfg.min_shares_per_trade}-share minimum")
        else:
            res.checks.append(f"PASS shares {order.shares} >= {cfg.min_shares_per_trade}")

        budget = self.max_spend(portfolio)
        fee = order.notional * cfg.commission_rate
        if action == Action.BUY:
            check(short_held == 0, "no short position in ticker",
                  f"{order.ticker} is held short ({short_held}); cover it before buying")
            cost = order.notional + fee
            check(cost <= budget,
                  f"cost ${cost:,.2f} <= available ${budget:,.2f}",
                  f"cost ${cost:,.2f} (incl. {cfg.commission_rate:.2%} fee) exceeds available ${budget:,.2f}"
                  + (" incl. margin" if cfg.use_margin else ""))
            unmargined = budget - (cfg.margin_limit if cfg.use_margin else 0.0)
            if cfg.use_margin and cost > unmargined:
                borrowed = cost - max(0.0, unmargined)
                warn(f"uses ~${borrowed:,.2f} margin at {cfg.margin_weekly_interest_rate:.0%} weekly "
                     f"(~${borrowed * cfg.margin_weekly_interest_rate:,.2f}/week)")
        elif action == Action.SHORT:
            check(cfg.allow_short_selling, "short selling enabled",
                  "short selling is disabled (rules.allow_short_selling)")
            check(long_held == 0, "no long position in ticker",
                  f"{order.ticker} is held long ({long_held}); sell it before shorting")
            collateral = order.notional * cfg.short_margin_requirement + fee
            check(collateral <= budget,
                  f"short collateral ${collateral:,.2f} <= available ${budget:,.2f}",
                  f"short needs ${collateral:,.2f} collateral ({cfg.short_margin_requirement:.0%} + fee); "
                  f"only ${budget:,.2f} available")
        elif action == Action.SELL:
            check(whole and order.shares <= long_held, f"selling {order.shares} of {long_held} held",
                  f"cannot sell {order.shares} {order.ticker}: only {long_held} held"
                  + (" (use SHORT to sell short)" if cfg.allow_short_selling else ""))
        elif action == Action.COVER:
            check(whole and order.shares <= short_held, f"covering {order.shares} of {short_held} short",
                  f"cannot cover {order.shares} {order.ticker}: only {short_held} held short")
            # Covering reduces risk, so the cash reserve may be used for it.
            cash = portfolio.cash + (cfg.margin_limit if cfg.use_margin else 0.0)
            check(order.notional + fee <= cash, f"cash ${cash:,.2f} covers the buy-back",
                  f"buy-back costs ${order.notional + fee:,.2f}; only ${cash:,.2f} cash")

        if action.opens and portfolio.total_equity > 0:
            h = portfolio.holding(order.ticker, action.position)
            current = h.market_value if h else 0.0
            pct = (current + order.notional) / portfolio.total_equity
            check(pct <= cfg.max_position_pct + 1e-9,
                  f"position {pct:.1%} of equity <= {cfg.max_position_pct:.0%}",
                  f"position would be {pct:.1%} of equity (limit {cfg.max_position_pct:.0%})")
            gross = (portfolio.gross_exposure + order.notional) / portfolio.total_equity
            check(gross <= cfg.max_gross_exposure + 1e-9,
                  f"gross exposure {gross:.0%} <= {cfg.max_gross_exposure:.0%}",
                  f"gross exposure would be {gross:.0%} of equity (limit {cfg.max_gross_exposure:.0%})")

        dup = [p for p in portfolio.pending_orders if p.ticker.upper() == order.ticker.upper()]
        check(not dup, "no pending order for ticker",
              f"{order.ticker} already has a pending order ({dup[0].action} {dup[0].shares})" if dup else "")

        check(trades_this_run < cfg.max_trades_per_run,
              f"trade {trades_this_run + 1} of max {cfg.max_trades_per_run} this run",
              f"max {cfg.max_trades_per_run} trades per run reached")

        if self.lock is not None:
            check(self.lock.held, "single active trader lock held",
                  "trader lock not held; another trader may be active")

        if check_hours and cfg.enforce_market_hours:
            now = self.clock()
            check(is_market_open(now), f"market {market_status(now)}",
                  f"market is {market_status(now)}; SMG trades 9:30-16:00 ET Mon-Fri")

        return res
