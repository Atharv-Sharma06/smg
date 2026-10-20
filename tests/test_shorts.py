"""Short selling, linked (all-or-nothing) orders and unwinding."""

import pytest

from smg_trader.alerts import AlertManager
from smg_trader.executor import TradeExecutor, TradeLog, apply_order, group_orders
from smg_trader.models import Action, Holding, Portfolio, Signal, TradeOrder, TradeStatus
from smg_trader.rules import RuleValidator, spendable_cash
from smg_trader.sizing import build_orders, orders_for_targets

from .conftest import MARKET_OPEN_TIME
from .test_executor import FakeBroker


@pytest.fixture
def short_rules(rules):
    rules.allow_short_selling = True
    return rules


def validator(rules):
    return RuleValidator(rules, lambda t: "NYSE", clock=lambda: MARKET_OPEN_TIME)


def short_portfolio():
    return Portfolio(cash=60_000, total_equity=50_000,
                     holdings=[Holding("XOM", 100, 100.0, 10_000.0, purchase_price=110.0, position="SHORT")])


def test_short_accounting():
    p = Portfolio(cash=50_000, total_equity=50_000)
    p = apply_order(p, TradeOrder("XOM", Action.SHORT, 100, 110.0), 0.0)
    assert p.cash == 61_000 and p.short_value == 11_000 and p.total_equity == 50_000
    assert p.shares_of("XOM", "SHORT") == 100 and p.shares_of("XOM") == 0 and p.net_shares("XOM") == -100
    # Price falls to 100: cover for a $1,000 profit.
    p.holdings[0].current_price, p.holdings[0].market_value = 100.0, 10_000.0
    p.total_equity = p.cash + p.invested_value
    assert p.total_equity == 51_000
    p = apply_order(p, TradeOrder("XOM", Action.COVER, 100, 100.0), 0.0)
    assert p.holdings == [] and p.cash == 51_000 and p.total_equity == 51_000


def test_spendable_cash_holds_back_short_collateral(short_rules):
    p = short_portfolio()
    # 60k cash - 1k reserve - 10k short value x (1 + 50% margin)
    assert spendable_cash(p, short_rules) == pytest.approx(44_000)
    p.buying_power = 30_000  # SMG's own number is lower: trust it
    assert spendable_cash(p, short_rules) == pytest.approx(29_000)


def test_short_rules(rules, short_rules):
    p = Portfolio(cash=50_000, total_equity=50_000, holdings=[Holding("KO", 50, 60.0, 3_000.0)])
    ok = validator(short_rules).validate(TradeOrder("XOM", Action.SHORT, 20, 110.0), p)
    assert ok.ok, ok.errors
    assert any("short collateral" in c for c in ok.checks)

    def errors(order, r=short_rules, portfolio=p):
        return " | ".join(validator(r).validate(order, portfolio).errors)

    rules_off = type(short_rules)(**{**short_rules.__dict__, "allow_short_selling": False})
    assert "short selling is disabled" in errors(TradeOrder("XOM", Action.SHORT, 20, 110.0), rules_off)
    assert "short-sale minimum" in errors(TradeOrder("CHEAP", Action.SHORT, 100, 4.0))
    assert "sell it before shorting" in errors(TradeOrder("KO", Action.SHORT, 20, 60.0))
    assert "collateral" in errors(TradeOrder("XOM", Action.SHORT, 900, 110.0))
    assert "use SHORT" in errors(TradeOrder("XOM", Action.SELL, 20, 110.0))


def test_cover_rules(short_rules):
    p = short_portfolio()
    assert validator(short_rules).validate(TradeOrder("XOM", Action.COVER, 100, 100.0), p).ok
    v = validator(short_rules).validate(TradeOrder("XOM", Action.COVER, 150, 100.0), p)
    assert any("only 100 held short" in e for e in v.errors)
    v = validator(short_rules).validate(TradeOrder("XOM", Action.BUY, 10, 100.0), p)
    assert any("cover it before buying" in e for e in v.errors)


def test_gross_exposure_limit(short_rules):
    short_rules.max_position_pct = 1.0
    short_rules.max_gross_exposure = 1.2
    p = short_portfolio()  # gross 10k on 50k equity
    v = validator(short_rules).validate(TradeOrder("KO", Action.SHORT, 900, 60.0), p)  # +54k gross
    assert any("gross exposure" in e for e in v.errors)


def sig(ticker, action, conf, price):
    return Signal(ticker, action, conf, conf if action == Action.BUY else -conf, "r", "t", price=price)


def test_signal_sizing_shorts_and_covers(short_rules):
    p = short_portfolio()
    signals = [sig("XOM", Action.BUY, 0.9, 100.0),   # held short -> cover
               sig("NFLX", Action.SELL, 0.8, 500.0),  # not held -> short
               sig("KO", Action.SELL, 0.8, 60.0)]
    orders = build_orders(signals, p, short_rules, min_confidence=0.4, short_exposure=0.3)
    assert [(o.ticker, o.action) for o in orders] == [("XOM", Action.COVER), ("NFLX", Action.SHORT),
                                                        ("KO", Action.SHORT)]
    assert orders[0].shares == 100
    for o in orders[1:]:
        assert o.notional <= 50_000 * short_rules.max_position_pct * 0.8 + 1e-6
    # Without a short book, SELL signals on unheld names do nothing.
    assert [o.action for o in build_orders(signals, p, short_rules, min_confidence=0.4)] == [Action.COVER]


def test_orders_for_targets_flips_and_minimums(short_rules):
    p = Portfolio(cash=10_000, total_equity=20_000, holdings=[Holding("KO", 50, 60.0, 3_000.0)])
    orders = orders_for_targets({"KO": -30, "XOM": 15, "AAPL": 5}, p, {"KO": 60.0, "XOM": 100.0, "AAPL": 190.0},
                                short_rules)
    assert [(o.ticker, o.action, o.shares) for o in orders] == [
        ("KO", Action.SELL, 50), ("KO", Action.SHORT, 30), ("XOM", Action.BUY, 15)]  # AAPL 5 < 10 minimum
    # A small closing order is fine; a too-small leg drops its whole group.
    small = Portfolio(cash=10_000, total_equity=10_000, holdings=[Holding("KO", 5, 60.0, 300.0)])
    assert [o.shares for o in orders_for_targets({"KO": 0}, small, {"KO": 60.0}, short_rules)] == [5]
    grouped = orders_for_targets({"A": -40, "B": 5}, Portfolio(10_000, 10_000), {"A": 50.0, "B": 50.0},
                                 short_rules, group_ids={"A": "pair:A/B", "B": "pair:A/B"})
    assert grouped == []


def pair_legs(gid="pair:XOM/CVX"):
    return [TradeOrder("XOM", Action.SHORT, 20, 110.0, group_id=gid),
            TradeOrder("CVX", Action.BUY, 15, 150.0, group_id=gid)]


def make_executor(rules, tmp_path, broker=None, dry_run=True):
    alerts = AlertManager([])
    ex = TradeExecutor(validator(rules), TradeLog(tmp_path / "trades.csv"), alerts, broker=broker,
                       dry_run=dry_run, confirm_delay=0, sleep=lambda s: None)
    return ex, alerts


def test_group_orders_keeps_legs_together():
    a, b = pair_legs()
    solo = TradeOrder("KO", Action.BUY, 10, 60.0)
    assert group_orders([a, solo, b]) == [[a, b], [solo]]


def test_linked_orders_all_or_nothing(short_rules, tmp_path):
    p = Portfolio(cash=50_000, total_equity=50_000)
    ex, _ = make_executor(short_rules, tmp_path)
    results, after = ex.execute(pair_legs(), p)
    assert [r.status for r in results] == [TradeStatus.DRY_RUN, TradeStatus.DRY_RUN]
    assert after.net_shares("XOM") == -20 and after.net_shares("CVX") == 15

    # One bad leg (CVX under the share minimum) rejects both.
    legs = pair_legs()
    legs[1].shares = 5
    results, after = ex.execute(legs, p)
    assert [r.status for r in results] == [TradeStatus.REJECTED, TradeStatus.REJECTED]
    assert "other leg failed" in results[0].message
    assert after.holdings == []


def test_live_short_confirmed(short_rules, tmp_path):
    p = Portfolio(cash=50_000, total_equity=50_000)
    ex, _ = make_executor(short_rules, tmp_path, broker=FakeBroker(p), dry_run=False)
    results, after = ex.execute([TradeOrder("XOM", Action.SHORT, 20, 110.0)], p)
    assert results[0].status == TradeStatus.CONFIRMED and "short 0 -> 20" in results[0].message
    assert after.shares_of("XOM", "SHORT") == 20


class FailSecondBroker(FakeBroker):
    def place_order(self, order):
        if len(self.placed) == 1 and order.action != Action.COVER:
            raise RuntimeError("SMG timeout")
        return super().place_order(order)


def test_failed_leg_unwinds_the_filled_one(short_rules, tmp_path):
    p = Portfolio(cash=50_000, total_equity=50_000)
    broker = FailSecondBroker(p)
    ex, alerts = make_executor(short_rules, tmp_path, broker=broker, dry_run=False)
    results, after = ex.execute(pair_legs(), p)
    assert [(r.order.ticker, r.order.action, r.status) for r in results] == [
        ("XOM", Action.SHORT, TradeStatus.CONFIRMED),
        ("CVX", Action.BUY, TradeStatus.FAILED),
        ("XOM", Action.COVER, TradeStatus.CONFIRMED),  # unwind
    ]
    assert after.holdings == [] and broker.portfolio.holdings == []
    assert any("broken" in a.title for a in alerts.sent)


def test_rest_of_group_not_sent_after_failure(short_rules, tmp_path):
    class FailFirst(FakeBroker):
        def place_order(self, order):
            raise RuntimeError("down")

    p = Portfolio(cash=50_000, total_equity=50_000)
    broker = FailFirst(p)
    ex, _ = make_executor(short_rules, tmp_path, broker=broker, dry_run=False)
    results, _ = ex.execute(pair_legs(), p)
    assert [r.status for r in results] == [TradeStatus.FAILED, TradeStatus.REJECTED]
    assert "not sent" in results[1].message and broker.placed == []


def test_fit_to_budget_resizes_or_drops(short_rules):
    from smg_trader.sizing import fit_to_budget

    p = Portfolio(cash=11_000, total_equity=11_000)  # 10k spendable after the 1k reserve
    orders = [TradeOrder("A", Action.BUY, 60, 100.0), TradeOrder("B", Action.BUY, 50, 100.0)]
    out = fit_to_budget(orders, p, short_rules)
    assert [(o.ticker, o.shares) for o in out] == [("A", 60), ("B", 39)]  # B shrunk to what's left
    out = fit_to_budget([TradeOrder("A", Action.BUY, 60, 100.0)], p, short_rules, reserve=5_000)
    assert [(o.ticker, o.shares) for o in out] == [("A", 49)]
    # Linked legs are never resized: the whole group goes.
    legs = [TradeOrder("A", Action.SHORT, 100, 100.0, group_id="g"), TradeOrder("B", Action.BUY, 100, 100.0,
                                                                                group_id="g")]
    assert fit_to_budget(legs, p, short_rules) == []
