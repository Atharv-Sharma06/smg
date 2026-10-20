"""Trade executor: validate -> (submit) -> confirm -> log -> alert."""

from __future__ import annotations

import copy
import csv
import logging
import time
from pathlib import Path
from typing import Callable, Protocol

from .alerts import AlertManager
from .models import Action, Holding, Portfolio, TradeOrder, TradeResult, TradeStatus, ValidationResult
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


def _model_spend(p: Portfolio, short_margin_requirement: float) -> float:
    return p.cash - p.short_value * (1 + short_margin_requirement)


def apply_order(portfolio: Portfolio, order: TradeOrder, commission_rate: float, *,
                short_margin_requirement: float = 0.5) -> Portfolio:
    """Return a copy of ``portfolio`` as if ``order`` filled at ``order.price``.

    Shorts use standard broker accounting: the sale proceeds go into cash and
    the position is a liability, so equity = cash + longs - shorts.
    """
    p = copy.deepcopy(portfolio)
    spend_before = _model_spend(p, short_margin_requirement)
    fee = order.notional * commission_rate
    side = order.action.position
    h = p.holding(order.ticker, side)

    if order.action in (Action.BUY, Action.COVER):
        p.cash -= order.notional + fee
    else:
        p.cash += order.notional - fee

    if order.action.opens:
        if h:
            cost = (h.purchase_price or order.price) * h.shares + order.notional
            h.shares += order.shares
            h.purchase_price = cost / h.shares
            h.current_price = order.price
            h.market_value = h.shares * order.price
        else:
            p.holdings.append(Holding(order.ticker, order.shares, order.price, order.notional,
                                      purchase_price=order.price, position=side))
    elif h:
        h.shares -= order.shares
        h.current_price = order.price
        h.market_value = h.shares * order.price
        if h.shares <= 0:
            p.holdings.remove(h)

    p.total_equity = p.cash + p.invested_value
    if p.buying_power is not None:  # keep SMG's scraped number moving with simulated fills
        p.buying_power += _model_spend(p, short_margin_requirement) - spend_before
    return p


REVERSE = {Action.BUY: Action.SELL, Action.SELL: Action.BUY,
           Action.SHORT: Action.COVER, Action.COVER: Action.SHORT}


def group_orders(orders: list[TradeOrder]) -> list[list[TradeOrder]]:
    """Group linked orders (same ``group_id``) together, keeping first-seen order."""
    out: list[list[TradeOrder]] = []
    by_id: dict[str, list[TradeOrder]] = {}
    for o in orders:
        if not o.group_id:
            out.append([o])
        elif o.group_id in by_id:
            by_id[o.group_id].append(o)
        else:
            by_id[o.group_id] = [o]
            out.append(by_id[o.group_id])
    return out


def validate_group(validator: RuleValidator, legs: list[TradeOrder], portfolio: Portfolio, *,
                   trades_this_run: int, check_hours: bool) -> list[ValidationResult]:
    """Validate legs in sequence, each against the portfolio after the previous legs.

    For linked orders, one failing leg fails them all.
    """
    cfg = validator.cfg
    sim = portfolio
    results = []
    for i, leg in enumerate(legs):
        v = validator.validate(leg, sim, trades_this_run=trades_this_run + i, check_hours=check_hours)
        results.append(v)
        if v.ok:
            sim = apply_order(sim, leg, cfg.commission_rate, short_margin_requirement=cfg.short_margin_requirement)
    if len(legs) > 1 and not all(v.ok for v in results):
        failed = ", ".join(f"{v.order.action.value} {v.order.ticker}" for v in results if not v.ok)
        for v in results:
            if v.ok:
                msg = f"linked order {v.order.group_id}: other leg failed ({failed})"
                v.errors.append(msg)
                v.checks.append(f"FAIL {msg}")
    return results


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

    def _apply(self, portfolio: Portfolio, order: TradeOrder) -> Portfolio:
        cfg = self.validator.cfg
        return apply_order(portfolio, order, cfg.commission_rate,
                           short_margin_requirement=cfg.short_margin_requirement)

    def _record(self, result: TradeResult, results: list[TradeResult]) -> None:
        order = result.order
        log.info("  -> %s %s %s: %s", order.action.value, order.ticker, result.status.value, result.message)
        self.trade_log.append(result, self.mode)
        results.append(result)
        if not self.dry_run and result.status != TradeStatus.REJECTED:
            self.alerts.send(
                "trade" if result.status != TradeStatus.FAILED else "error",
                f"{result.status.value}: {order.action.value} {order.shares} {order.ticker} @ ~${order.price:.2f}",
                f"confidence {order.confidence:.2f}; {result.message}\n{order.reason[:300]}",
            )

    def execute(self, orders: list[TradeOrder], portfolio: Portfolio) -> tuple[list[TradeResult], Portfolio]:
        """Execute orders in sequence; linked orders (same group_id) all-or-nothing.

        Returns results and the (simulated or re-scraped) portfolio.
        """
        results: list[TradeResult] = []
        accepted = 0
        for legs in group_orders(orders):
            vals = validate_group(self.validator, legs, portfolio, trades_this_run=accepted,
                                  check_hours=self.check_hours)
            for v in vals:
                o = v.order
                log.info("Validating %s %d %s @ $%.2f (confidence %.2f)%s", o.action.value, o.shares, o.ticker,
                         o.price, o.confidence, f" [{o.group_id}]" if o.group_id else "")
                for line in v.checks:
                    log.info("    %s", line)

            if not all(v.ok for v in vals):
                for v in vals:
                    self._record(TradeResult(v.order, TradeStatus.REJECTED, "; ".join(v.errors)), results)
            elif self.dry_run:
                for leg in legs:
                    portfolio = self._apply(portfolio, leg)
                    self._record(TradeResult(leg, TradeStatus.DRY_RUN, "validated; not submitted (dry run)"),
                                 results)
                accepted += len(legs)
            else:
                portfolio, sent = self._submit_group(legs, portfolio, results)
                accepted += sent
        return results, portfolio

    def _submit_group(self, legs: list[TradeOrder], portfolio: Portfolio,
                      results: list[TradeResult]) -> tuple[Portfolio, int]:
        done: list[TradeResult] = []
        for i, leg in enumerate(legs):
            result, portfolio = self._submit(leg, portfolio)
            self._record(result, results)
            if result.status != TradeStatus.FAILED:
                done.append(result)
            ok = result.status in (TradeStatus.CONFIRMED, TradeStatus.PENDING, TradeStatus.SUBMITTED)
            if ok or len(legs) == 1:
                continue
            # A linked leg failed: don't send the rest, and undo legs that already filled.
            for rest in legs[i + 1:]:
                self._record(TradeResult(rest, TradeStatus.REJECTED,
                                         f"not sent: linked leg {leg.action.value} {leg.ticker} {result.status.value}"),
                             results)
            portfolio = self._unwind(leg, result, [d for d in done if d is not result], portfolio, results)
            break
        return portfolio, len(done)

    def _unwind(self, failed_leg: TradeOrder, failed: TradeResult, done: list[TradeResult],
                portfolio: Portfolio, results: list[TradeResult]) -> Portfolio:
        notes = []
        for prev in reversed(done):
            o = prev.order
            if prev.status != TradeStatus.CONFIRMED:
                notes.append(f"{o.action.value} {o.shares} {o.ticker} is {prev.status.value}: cancel it in SMG")
                continue
            undo = TradeOrder(o.ticker, REVERSE[o.action], o.shares, o.price, o.confidence,
                              f"unwind linked leg after {failed_leg.ticker} {failed.status.value}",
                              o.exchange)
            v = self.validator.validate(undo, portfolio, trades_this_run=0, check_hours=self.check_hours)
            if not v.ok:
                notes.append(f"could not unwind {o.ticker}: {'; '.join(v.errors)}")
                self._record(TradeResult(undo, TradeStatus.REJECTED, "; ".join(v.errors)), results)
                continue
            result, portfolio = self._submit(undo, portfolio)
            self._record(result, results)
            notes.append(f"unwound {o.ticker}: {result.status.value}")
        if failed.status == TradeStatus.UNCONFIRMED:
            notes.append(f"{failed_leg.ticker} may still fill: check SMG")
        self.alerts.send("error", f"Linked order {failed_leg.group_id} broken: {failed_leg.ticker} "
                                  f"{failed.status.value}", "\n".join(notes) or failed.message)
        return portfolio

    def _throttle(self) -> None:
        gap = self.validator.cfg.min_seconds_between_trades
        wait = self._last_trade_at + gap - time.monotonic()
        if self._last_trade_at and wait > 0:
            self.sleep(wait)
        self._last_trade_at = time.monotonic()

    def _submit(self, order: TradeOrder, portfolio: Portfolio) -> tuple[TradeResult, Portfolio]:
        assert self.broker is not None
        self._throttle()
        side = order.action.position
        before = portfolio.shares_of(order.ticker, side)
        try:
            confirmation = self.broker.place_order(order)
        except Exception as exc:
            log.exception("order submission failed")
            return TradeResult(order, TradeStatus.FAILED, f"submit error: {exc}"), portfolio

        if not self.confirm:
            return TradeResult(order, TradeStatus.SUBMITTED, "submitted", confirmation), self._apply(portfolio, order)

        self.sleep(self.confirm_delay)
        try:
            fresh = self.broker.get_portfolio()
        except Exception as exc:
            log.warning("could not re-scrape portfolio to confirm: %s", exc)
            return (TradeResult(order, TradeStatus.SUBMITTED, f"submitted; confirmation check failed: {exc}",
                                confirmation), self._apply(portfolio, order))

        expected = before + order.shares if order.action.opens else before - order.shares
        after = fresh.shares_of(order.ticker, side)
        label = "short" if side == "SHORT" else "holdings"
        if after == expected:
            status, msg = TradeStatus.CONFIRMED, f"{label} {before} -> {after}"
        elif any(p.ticker.upper() == order.ticker.upper() for p in fresh.pending_orders):
            status, msg = TradeStatus.PENDING, "listed in pending orders"
        else:
            status, msg = TradeStatus.UNCONFIRMED, f"{label} {before} -> {after}, expected {expected}; not pending"
        return TradeResult(order, status, msg, confirmation), fresh
