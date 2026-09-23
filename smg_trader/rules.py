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
        """Cash the bot may spend on buys (keeps the reserve, adds margin if enabled)."""
        spend = portfolio.cash - self.cfg.cash_reserve
        if self.cfg.use_margin:
            spend += self.cfg.margin_limit
        return max(0.0, spend)

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

        is_buy = order.action == Action.BUY
        held = portfolio.shares_of(order.ticker)

        check(order.action in (Action.BUY, Action.SELL), f"action {order.action.value}",
              f"unsupported action {order.action.value}")

        # Whole shares only.
        check(isinstance(order.shares, int) and not isinstance(order.shares, bool) and order.shares > 0,
              f"whole shares ({order.shares})", f"shares must be a positive whole number, got {order.shares!r}")

        # Exchange must be NYSE or NASDAQ.
        exchange = order.exchange or self.exchange_lookup(order.ticker)
        order.exchange = exchange
        allowed = {e.upper() for e in cfg.allowed_exchanges}
        if exchange is None:
            check(False, "", f"could not verify exchange for {order.ticker} (allowed: {', '.join(sorted(allowed))})")
        else:
            check(exchange.upper() in allowed, f"exchange {exchange}",
                  f"{order.ticker} trades on {exchange}; SMG allows only {', '.join(sorted(allowed))}")

        # $3 minimum applies to purchases; selling a position that fell below $3 is allowed.
        if is_buy:
            check(order.price >= cfg.min_share_price, f"price ${order.price:.2f} >= ${cfg.min_share_price:.2f}",
                  f"price ${order.price:.2f} is below the ${cfg.min_share_price:.2f} minimum")
        elif order.price < cfg.min_share_price:
            warn(f"price ${order.price:.2f} is below ${cfg.min_share_price:.2f} (sell allowed)")

        # 10-share minimum. A sell that closes out a smaller position is allowed.
        if isinstance(order.shares, int) and order.shares < cfg.min_shares_per_trade:
            if not is_buy and order.shares == held:
                warn(f"{order.shares} shares < {cfg.min_shares_per_trade} minimum, but it closes the position")
            else:
                check(False, "", f"{order.shares} shares is below the {cfg.min_shares_per_trade}-share minimum")
        else:
            res.checks.append(f"PASS shares {order.shares} >= {cfg.min_shares_per_trade}")

        if is_buy:
            cost = order.notional * (1 + cfg.commission_rate)
            budget = self.max_spend(portfolio)
            check(cost <= budget,
                  f"cost ${cost:,.2f} <= available ${budget:,.2f}",
                  f"cost ${cost:,.2f} (incl. {cfg.commission_rate:.2%} fee) exceeds available ${budget:,.2f}"
                  + (" incl. margin" if cfg.use_margin else ""))
            spend_from_cash = portfolio.cash - cfg.cash_reserve
            if cfg.use_margin and cost > spend_from_cash:
                borrowed = cost - max(0.0, spend_from_cash)
                warn(f"uses ~${borrowed:,.2f} margin at {cfg.margin_weekly_interest_rate:.0%} weekly "
                     f"(~${borrowed * cfg.margin_weekly_interest_rate:,.2f}/week)")

            if portfolio.total_equity > 0:
                current = (portfolio.holding(order.ticker).market_value if held else 0.0)
                pct = (current + order.notional) / portfolio.total_equity
                check(pct <= cfg.max_position_pct + 1e-9,
                      f"position {pct:.1%} of equity <= {cfg.max_position_pct:.0%}",
                      f"position would be {pct:.1%} of equity (limit {cfg.max_position_pct:.0%})")
        else:
            if cfg.allow_short_selling:
                if order.shares > held:
                    warn(f"sells {order.shares - held} more shares than held (short sale)")
            else:
                check(isinstance(order.shares, int) and order.shares <= held,
                      f"selling {order.shares} of {held} held",
                      f"cannot sell {order.shares} {order.ticker}: only {held} held (short selling disabled)")

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
