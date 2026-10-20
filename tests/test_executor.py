import copy

import pytest

from smg_trader.alerts import AlertManager
from smg_trader.executor import TradeExecutor, TradeLog, apply_order
from smg_trader.lock import TraderLock, TraderLockError
from smg_trader.models import Action, Holding, PendingOrder, Portfolio, Signal, TradeOrder, TradeStatus
from smg_trader.rules import RuleValidator
from smg_trader.sizing import build_orders

from .conftest import MARKET_OPEN_TIME


class FakeBroker:
    """In-memory SMG: fills immediately, or leaves orders pending."""

    def __init__(self, portfolio: Portfolio, mode: str = "fill"):
        self.portfolio = copy.deepcopy(portfolio)
        self.mode = mode
        self.placed: list[TradeOrder] = []

    def place_order(self, order):
        if self.mode == "error":
            raise RuntimeError("SMG says no")
        self.placed.append(order)
        if self.mode == "fill":
            self.portfolio = apply_order(self.portfolio, order, 0.005)
        elif self.mode == "pending":
            self.portfolio.pending_orders.append(PendingOrder(order.ticker, order.action.value, order.shares))
        return f"CONF{len(self.placed):04d}"

    def get_portfolio(self):
        return copy.deepcopy(self.portfolio)


@pytest.fixture
def portfolio():
    return Portfolio(cash=30_000, total_equity=100_000,
                     holdings=[Holding("KO", 50, 60.0, 3_000.0, purchase_price=58.0)])


def make_executor(rules, tmp_path, *, broker=None, dry_run=True):
    validator = RuleValidator(rules, lambda t: "NYSE", clock=lambda: MARKET_OPEN_TIME)
    alerts = AlertManager([])
    ex = TradeExecutor(validator, TradeLog(tmp_path / "trades.csv"), alerts, broker=broker,
                       dry_run=dry_run, confirm_delay=0, sleep=lambda s: None)
    return ex, alerts


def sig(ticker, action, conf, price):
    return Signal(ticker, action, conf, conf if action == Action.BUY else -conf, "test", "test", price=price)


def test_sizing_sells_first_then_buys_whole_shares(rules, portfolio):
    signals = [sig("XOM", Action.BUY, 0.9, 110.0), sig("KO", Action.SELL, 0.6, 60.0),
               sig("AAPL", Action.BUY, 0.5, 190.0), sig("LOW", Action.BUY, 0.3, 50.0),
               sig("NOPE", Action.SELL, 0.9, 10.0)]  # not held: ignored
    orders = build_orders(signals, portfolio, rules, min_confidence=0.4)
    assert [(o.ticker, o.action) for o in orders] == [("KO", Action.SELL), ("XOM", Action.BUY),
                                                        ("AAPL", Action.BUY)]
    assert orders[0].shares == 50
    xom = orders[1]
    assert isinstance(xom.shares, int) and xom.shares >= rules.min_shares_per_trade
    assert xom.notional <= 100_000 * rules.max_position_pct * 0.9 + 1e-6


def test_sizing_respects_cash_and_minimums(rules):
    poor = Portfolio(cash=1_500, total_equity=100_000)
    assert build_orders([sig("XOM", Action.BUY, 0.9, 110.0)], poor, rules, min_confidence=0.4) == []
    assert build_orders([sig("PENNY", Action.BUY, 0.9, 2.5)], Portfolio(50_000, 100_000), rules,
                        min_confidence=0.4) == []


def test_dry_run_validates_logs_and_never_submits(rules, portfolio, tmp_path):
    broker = FakeBroker(portfolio)
    ex, alerts = make_executor(rules, tmp_path, broker=broker, dry_run=True)
    orders = [TradeOrder("XOM", Action.BUY, 20, 110.0, 0.9, "why"), TradeOrder("PENNY", Action.BUY, 100, 2.5)]
    results, after = ex.execute(orders, portfolio)
    assert [r.status for r in results] == [TradeStatus.DRY_RUN, TradeStatus.REJECTED]
    assert broker.placed == []
    assert after.shares_of("XOM") == 20 and after.cash < portfolio.cash  # simulated
    rows = TradeLog(tmp_path / "trades.csv").read()
    assert [(r["ticker"], r["action"], r["shares"], r["status"], r["mode"]) for r in rows] == [
        ("XOM", "BUY", "20", "DRY_RUN", "dry-run"), ("PENNY", "BUY", "100", "REJECTED", "dry-run")]
    assert rows[0]["timestamp"] and rows[0]["price"] == "110.00"
    assert alerts.sent == []  # no trade alerts in dry run


def test_live_execution_confirms_via_portfolio(rules, portfolio, tmp_path):
    broker = FakeBroker(portfolio)
    ex, alerts = make_executor(rules, tmp_path, broker=broker, dry_run=False)
    results, after = ex.execute([TradeOrder("XOM", Action.BUY, 20, 110.0, 0.9),
                                 TradeOrder("KO", Action.SELL, 50, 60.0, 0.7)], portfolio)
    assert [r.status for r in results] == [TradeStatus.CONFIRMED, TradeStatus.CONFIRMED]
    assert results[0].confirmation_id == "CONF0001"
    assert after.shares_of("XOM") == 20 and after.shares_of("KO") == 0
    assert [a.kind for a in alerts.sent] == ["trade", "trade"]


def test_live_pending_and_failed(rules, portfolio, tmp_path):
    ex, _ = make_executor(rules, tmp_path, broker=FakeBroker(portfolio, "pending"), dry_run=False)
    results, _ = ex.execute([TradeOrder("XOM", Action.BUY, 20, 110.0)], portfolio)
    assert results[0].status == TradeStatus.PENDING

    ex, alerts = make_executor(rules, tmp_path, broker=FakeBroker(portfolio, "error"), dry_run=False)
    results, _ = ex.execute([TradeOrder("XOM", Action.BUY, 20, 110.0)], portfolio)
    assert results[0].status == TradeStatus.FAILED and "SMG says no" in results[0].message
    assert alerts.sent[-1].kind == "error"


def test_live_mode_requires_broker(rules, tmp_path):
    with pytest.raises(ValueError):
        make_executor(rules, tmp_path, broker=None, dry_run=False)


def test_executor_updates_cash_between_orders(rules, tmp_path):
    rules.max_position_pct = 1.0
    p = Portfolio(cash=5_000, total_equity=5_000)
    ex, _ = make_executor(rules, tmp_path, dry_run=True)
    results, _ = ex.execute([TradeOrder("XOM", Action.BUY, 30, 110.0), TradeOrder("KO", Action.BUY, 30, 60.0)], p)
    # Second order no longer fits once the first one spent the cash.
    assert [r.status for r in results] == [TradeStatus.DRY_RUN, TradeStatus.REJECTED]


def test_trader_lock_is_exclusive(tmp_path):
    path = tmp_path / "trader.lock"
    path.write_text("1")  # pid 1 is always alive
    with pytest.raises(TraderLockError):
        TraderLock(path).acquire()
    path.write_text("999999999")  # dead pid: stale lock is reclaimed
    with TraderLock(path) as lock:
        assert lock.held
    assert not path.exists()
