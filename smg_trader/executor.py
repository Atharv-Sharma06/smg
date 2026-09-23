"""Trade executor: validate -> (submit) -> confirm -> log -> alert."""

from __future__ import annotations

import copy
import csv
import logging
import time
from pathlib import Path
from typing import Callable, Protocol

from .alerts import AlertManager
from .models import Action, Holding, Portfolio, TradeOrder, TradeResult, TradeStatus
from .rules import RuleValidator

log = logging.getLogger(__name__)


class Broker(Protocol):
    """What the executor needs from SMG. ``SMGScraper`` implements it."""

    def place_order(self, order: TradeOrder) -> str: ...

    def get_portfolio(self) -> Portfolio: ...


class TradeLog:
    FIELDS = ["timestamp", "mode", "ticker", "action", "shares", "price", "status",
              "confidence", "confirmation_id", "message", "reason"]

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, result: TradeResult, mode: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        o = result.order
        with self.path.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.FIELDS)
            if new:
                writer.writeheader()
            writer.writerow({
                "timestamp": result.timestamp.isoformat(timespec="seconds"),
                "mode": mode,
                "ticker": o.ticker,
                "action": o.action.value,
                "shares": o.shares,
                "price": f"{o.price:.2f}",
                "status": result.status.value,
                "confidence": f"{o.confidence:.3f}",
                "confirmation_id": result.confirmation_id,
                "message": result.message,
                "reason": o.reason[:500],
            })

    def read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(newline="") as fh:
            return list(csv.DictReader(fh))


def apply_order(portfolio: Portfolio, order: TradeOrder, commission_rate: float) -> Portfolio:
    """Return a copy of ``portfolio`` as if ``order`` filled at ``order.price``."""
    p = copy.deepcopy(portfolio)
    fee = order.notional * commission_rate
    h = p.holding(order.ticker)
    if order.action == Action.BUY:
        p.cash -= order.notional + fee
        if h:
            cost = (h.purchase_price or order.price) * h.shares + order.notional
            h.shares += order.shares
            h.purchase_price = cost / h.shares
            h.current_price = order.price
            h.market_value = h.shares * order.price
        else:
            p.holdings.append(Holding(order.ticker, order.shares, order.price,
                                      order.notional, purchase_price=order.price))
    else:
        p.cash += order.notional - fee
        if h:
            h.shares -= order.shares
            h.current_price = order.price
            h.market_value = h.shares * order.price
            if h.shares <= 0:
                p.holdings.remove(h)
    p.total_equity = p.cash + p.invested_value
    return p


class TradeExecutor:
    def __init__(
        self,
        validator: RuleValidator,
        trade_log: TradeLog,
        alerts: AlertManager,
        *,
        broker: Broker | None = None,
        dry_run: bool = True,
        check_hours: bool = True,
        confirm: bool = True,
        confirm_delay: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not dry_run and broker is None:
            raise ValueError("Live execution needs a broker")
        self.validator = validator
        self.trade_log = trade_log
        self.alerts = alerts
        self.broker = broker
        self.dry_run = dry_run
        self.check_hours = check_hours
        self.confirm = confirm
        self.confirm_delay = confirm_delay
        self.sleep = sleep
        self._last_trade_at = 0.0

    @property
    def mode(self) -> str:
        return "dry-run" if self.dry_run else "live"

    def execute(self, orders: list[TradeOrder], portfolio: Portfolio) -> tuple[list[TradeResult], Portfolio]:
        """Execute orders in sequence. Returns results and the (simulated or re-scraped) portfolio."""
        results: list[TradeResult] = []
        accepted = 0
        commission = self.validator.cfg.commission_rate
        for order in orders:
            v = self.validator.validate(order, portfolio, trades_this_run=accepted,
                                        check_hours=self.check_hours)
            log.info("Validating %s %d %s @ $%.2f (confidence %.2f)",
                     order.action.value, order.shares, order.ticker, order.price, order.confidence)
            for line in v.checks:
                log.info("    %s", line)

            if not v.ok:
                result = TradeResult(order, TradeStatus.REJECTED, "; ".join(v.errors))
            elif self.dry_run:
                result = TradeResult(order, TradeStatus.DRY_RUN, "validated; not submitted (dry run)")
                portfolio = apply_order(portfolio, order, commission)
                accepted += 1
            else:
                result, portfolio = self._submit(order, portfolio)
                if result.status not in (TradeStatus.FAILED,):
                    accepted += 1

            log.info("  -> %s %s", result.status.value, result.message)
            self.trade_log.append(result, self.mode)
            results.append(result)
            if not self.dry_run and result.status != TradeStatus.REJECTED:
                self.alerts.send(
                    "trade" if result.status != TradeStatus.FAILED else "error",
                    f"{result.status.value}: {order.action.value} {order.shares} {order.ticker} @ ~${order.price:.2f}",
                    f"confidence {order.confidence:.2f}; {result.message}\n{order.reason[:300]}",
                )
        return results, portfolio

    def _throttle(self) -> None:
        gap = self.validator.cfg.min_seconds_between_trades
        wait = self._last_trade_at + gap - time.monotonic()
        if self._last_trade_at and wait > 0:
            self.sleep(wait)
        self._last_trade_at = time.monotonic()

    def _submit(self, order: TradeOrder, portfolio: Portfolio) -> tuple[TradeResult, Portfolio]:
        assert self.broker is not None
        self._throttle()
        before = portfolio.shares_of(order.ticker)
        try:
            confirmation = self.broker.place_order(order)
        except Exception as exc:
            log.exception("order submission failed")
            return TradeResult(order, TradeStatus.FAILED, f"submit error: {exc}"), portfolio

        if not self.confirm:
            return (TradeResult(order, TradeStatus.SUBMITTED, "submitted", confirmation),
                    apply_order(portfolio, order, self.validator.cfg.commission_rate))

        self.sleep(self.confirm_delay)
        try:
            fresh = self.broker.get_portfolio()
        except Exception as exc:
            log.warning("could not re-scrape portfolio to confirm: %s", exc)
            return (TradeResult(order, TradeStatus.SUBMITTED, f"submitted; confirmation check failed: {exc}",
                                confirmation),
                    apply_order(portfolio, order, self.validator.cfg.commission_rate))

        expected = before + order.shares if order.action == Action.BUY else before - order.shares
        after = fresh.shares_of(order.ticker)
        if after == expected:
            status, msg = TradeStatus.CONFIRMED, f"holdings {before} -> {after}"
        elif any(p.ticker.upper() == order.ticker.upper() for p in fresh.pending_orders):
            status, msg = TradeStatus.PENDING, "listed in pending orders"
        else:
            status, msg = TradeStatus.UNCONFIRMED, f"holdings {before} -> {after}, expected {expected}; not pending"
        return TradeResult(order, status, msg, confirmation), fresh
