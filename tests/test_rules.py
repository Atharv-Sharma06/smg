from datetime import datetime

import pytest

from smg_trader.lock import TraderLock
from smg_trader.market_hours import ET, is_market_open, market_status
from smg_trader.models import Action, Holding, PendingOrder, Portfolio, TradeOrder
from smg_trader.rules import RuleValidator

from .conftest import MARKET_OPEN_TIME


def validator(rules, exchange="NYSE", clock=MARKET_OPEN_TIME, lock=None):
    return RuleValidator(rules, lambda t: exchange, lock=lock, clock=lambda: clock)


@pytest.fixture
def portfolio():
    return Portfolio(cash=20_000, total_equity=100_000,
                     holdings=[Holding("KO", 50, 60.0, 3_000.0, purchase_price=58.0)])


def test_valid_buy_passes_and_logs_every_check(rules, portfolio):
    v = validator(rules).validate(TradeOrder("XOM", Action.BUY, 20, 110.0), portfolio)
    assert v.ok, v.errors
    joined = "\n".join(v.checks)
    for fragment in ("whole shares", "exchange NYSE", "price $110.00", "shares 20 >= 10", "cost", "market open"):
        assert fragment in joined


@pytest.mark.parametrize("order,error", [
    (TradeOrder("PENNY", Action.BUY, 100, 2.50), "below the $3.00 minimum"),
    (TradeOrder("XOM", Action.BUY, 5, 110.0), "below the 10-share minimum"),
    (TradeOrder("XOM", Action.BUY, 1000, 110.0), "exceeds available"),
    (TradeOrder("KO", Action.SELL, 60, 60.0), "only 50 held"),
])
def test_rule_violations(rules, portfolio, order, error):
    v = validator(rules).validate(order, portfolio)
    assert not v.ok
    assert any(error in e for e in v.errors), v.errors


def test_fractional_shares_rejected(rules, portfolio):
    v = validator(rules).validate(TradeOrder("XOM", Action.BUY, 10.5, 110.0), portfolio)  # type: ignore[arg-type]
    assert any("whole number" in e for e in v.errors)


def test_exchange_rules(rules, portfolio):
    order = TradeOrder("XYZ", Action.BUY, 10, 20.0)
    assert any("SMG allows only" in e for e in validator(rules, exchange="OTC").validate(order, portfolio).errors)
    order = TradeOrder("XYZ", Action.BUY, 10, 20.0)
    assert any("could not verify" in e for e in validator(rules, exchange=None).validate(order, portfolio).errors)
    order = TradeOrder("XYZ", Action.BUY, 10, 20.0)
    assert validator(rules, exchange="NASDAQ").validate(order, portfolio).ok


def test_market_hours(rules, portfolio):
    saturday = datetime(2026, 9, 26, 11, 0, tzinfo=ET)
    v = validator(rules, clock=saturday).validate(TradeOrder("XOM", Action.BUY, 10, 110.0), portfolio)
    assert any("weekend" in e for e in v.errors)
    v = validator(rules, clock=saturday).validate(TradeOrder("XOM", Action.BUY, 10, 110.0), portfolio,
                                                  check_hours=False)
    assert v.ok


def test_margin_extends_budget_with_warning(rules, portfolio):
    rules.use_margin = True
    rules.max_position_pct = 1.0
    v = validator(rules).validate(TradeOrder("XOM", Action.BUY, 300, 110.0), portfolio)
    assert v.ok, v.errors
    assert any("margin" in w for w in v.warnings)


def test_small_sell_that_closes_position_allowed(rules):
    p = Portfolio(cash=0, total_equity=500, holdings=[Holding("KO", 5, 60.0, 300.0)])
    v = validator(rules).validate(TradeOrder("KO", Action.SELL, 5, 60.0), p)
    assert v.ok and v.warnings


def test_position_limit_and_pending_and_trade_cap(rules, portfolio):
    rules.max_position_pct = 0.05
    v = validator(rules).validate(TradeOrder("XOM", Action.BUY, 50, 110.0), portfolio)
    assert any("limit 5%" in e for e in v.errors)

    rules.max_position_pct = 0.5
    portfolio.pending_orders.append(PendingOrder("XOM", "Buy", 10))
    v = validator(rules).validate(TradeOrder("XOM", Action.BUY, 10, 110.0), portfolio)
    assert any("pending order" in e for e in v.errors)

    v = validator(rules).validate(TradeOrder("KO", Action.SELL, 10, 60.0), portfolio,
                                  trades_this_run=rules.max_trades_per_run)
    assert any("max" in e for e in v.errors)


def test_lock_rule(rules, portfolio, tmp_path):
    lock = TraderLock(tmp_path / "t.lock")
    order = TradeOrder("XOM", Action.BUY, 10, 110.0)
    assert any("lock" in e for e in validator(rules, lock=lock).validate(order, portfolio).errors)
    with lock:
        assert validator(rules, lock=lock).validate(order, portfolio).ok


def test_market_status_strings():
    assert is_market_open(datetime(2026, 9, 23, 9, 30, tzinfo=ET))
    assert not is_market_open(datetime(2026, 9, 23, 16, 0, tzinfo=ET))
    assert not is_market_open(datetime(2026, 11, 26, 12, 0, tzinfo=ET))  # Thanksgiving
    assert "pre-market" in market_status(datetime(2026, 9, 23, 8, 0, tzinfo=ET))
    assert "holiday" in market_status(datetime(2026, 12, 25, 12, 0, tzinfo=ET))
