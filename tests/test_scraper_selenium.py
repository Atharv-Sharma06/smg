"""Real-browser tests of the SMG scraper against tests/mock_smg/server.py.

Needs Chrome/Chromium + a matching chromedriver. Set CHROME_BINARY /
CHROMEDRIVER_PATH if they are not on PATH; the tests skip if no browser starts.
"""

from __future__ import annotations

import pytest

from smg_trader.config import SMGConfig
from smg_trader.models import Action, TradeOrder, TradeStatus
from smg_trader.scraper import LoginError, OrderError, SMGScraper, make_chrome_driver

from .conftest import chrome_paths
from .mock_smg.server import MockSMGServer, MockSMGState


@pytest.fixture(scope="module")
def driver():
    binary, drv = chrome_paths()
    cfg = SMGConfig(chrome_binary=binary, chromedriver_path=drv)
    try:
        d = make_chrome_driver(cfg)
    except Exception as exc:  # no browser in this environment
        pytest.skip(f"Chrome not available: {str(exc).splitlines()[0]}")
    yield d
    d.quit()


@pytest.fixture
def server():
    with MockSMGServer(MockSMGState()) as srv:
        yield srv


@pytest.fixture
def smg(server, driver, tmp_path):
    driver.delete_all_cookies()
    cfg = SMGConfig(
        username="student1", password="secret", login_url=f"{server.url}/login.html",
        min_seconds_between_requests=0, element_timeout=3, login_retries=2,
        debug_dir=str(tmp_path / "debug"), team_name="Tar Heel Traders",
    )
    scraper = SMGScraper(cfg, driver_factory=lambda c: driver)
    yield scraper
    scraper.driver = None  # the module-scoped driver is shared; don't quit it


def test_login_and_stays_authenticated(smg, server):
    smg.login()
    assert smg.is_logged_in()
    smg.get_holdings()
    smg.get_holdings()
    assert server.state.login_attempts == 1  # session reused


def test_bad_credentials_fail_fast_without_retry(smg, server, tmp_path):
    smg.cfg.password = "wrong"
    with pytest.raises(LoginError, match="Invalid user name or password"):
        smg.login()
    assert server.state.login_attempts == 1
    assert list((tmp_path / "debug").glob("*login_failed.html"))  # page saved for debugging


def test_missing_credentials(smg):
    smg.cfg.username = ""
    with pytest.raises(LoginError, match="not set"):
        smg.login()


def test_relogin_after_session_expiry(smg, server):
    smg.login()
    server.state.sessions.clear()  # SMG session timed out
    holdings = smg.get_holdings()
    assert {h.ticker for h in holdings} == {"AAPL", "KO"}
    assert server.state.login_attempts == 2


def test_portfolio_scrape_types(smg):
    p = smg.get_portfolio()
    assert p.cash == 50_000.0 and isinstance(p.cash, float)
    assert p.buying_power == 100_000.0
    assert p.total_equity == 50_000 + 100 * 190 + 50 * 60
    aapl = p.holding("AAPL")
    assert (aapl.shares, aapl.purchase_price, aapl.current_price, aapl.market_value, aapl.gain_loss) == (
        100, 170.0, 190.0, 19_000.0, 2_000.0)
    assert isinstance(aapl.shares, int) and isinstance(aapl.current_price, float)
    assert p.holding("KO").position == "LONG"
    assert len(p.holdings) == 2  # "Total" row ignored
    assert p.pending_orders == []


def test_cash_balance(smg):
    assert smg.get_cash() == 50_000.0


def test_empty_portfolio(smg, server):
    server.state.holdings.clear()
    assert smg.get_holdings() == []


def test_leaderboard_nc_filter(smg):
    board = smg.get_leaderboard(top_n=10)
    assert [e.team for e in board] == ["Tar Heel Traders", "Bull Durham", "Queen City Quants", "Wolfpack Wealth"]
    assert [e.rank for e in board] == [1, 2, 3, 4]
    assert board[0].total_equity == 112_400.0 and board[0].pct_return == 12.4


def test_place_buy_order_and_confirm(smg, server):
    conf = smg.place_order(TradeOrder("XOM", Action.BUY, 10, 110.0))
    assert conf == server.state.orders[-1]["confirmation"]
    assert server.state.orders[-1] == {"ticker": "XOM", "action": "Buy", "shares": 10, "confirmation": conf}
    assert smg.get_portfolio().shares_of("XOM") == 10


def test_sell_picks_sell_not_short_sell(smg, server):
    smg.place_order(TradeOrder("KO", Action.SELL, 10, 60.0))
    assert server.state.orders[-1]["action"] == "Sell"
    assert server.state.holdings["KO"]["shares"] == 40


def test_order_rejected_by_smg(smg):
    with pytest.raises(OrderError, match="not found"):
        smg.place_order(TradeOrder("ZZZZ", Action.BUY, 10, 10.0))


def test_pending_orders_scraped(smg, server):
    server.state.hold_orders_pending = True
    smg.place_order(TradeOrder("MSFT", Action.BUY, 10, 410.0))
    p = smg.get_portfolio()
    assert p.shares_of("MSFT") == 0
    assert [(o.ticker, o.action, o.shares) for o in p.pending_orders] == [("MSFT", "Buy", 10)]


def test_executor_live_against_mock(smg, server, rules, tmp_path):
    from smg_trader.alerts import AlertManager
    from smg_trader.executor import TradeExecutor, TradeLog
    from smg_trader.rules import RuleValidator

    from .conftest import MARKET_OPEN_TIME

    validator = RuleValidator(rules, lambda t: "NYSE", clock=lambda: MARKET_OPEN_TIME)
    ex = TradeExecutor(validator, TradeLog(tmp_path / "trades.csv"), AlertManager([]), broker=smg,
                       dry_run=False, confirm_delay=0)
    portfolio = smg.get_portfolio()
    results, after = ex.execute([TradeOrder("XOM", Action.BUY, 10, 110.0), TradeOrder("PENNY", Action.BUY, 10, 2.5),
                                 TradeOrder("KO", Action.SELL, 50, 60.0)], portfolio)
    assert [r.status for r in results] == [TradeStatus.CONFIRMED, TradeStatus.REJECTED, TradeStatus.CONFIRMED]
    assert after.shares_of("XOM") == 10 and after.shares_of("KO") == 0
    assert len(server.state.orders) == 2  # the $2.50 stock never reached SMG
    rows = TradeLog(tmp_path / "trades.csv").read()
    assert [r["status"] for r in rows] == ["CONFIRMED", "REJECTED", "CONFIRMED"]


def test_full_pipeline_live_against_mock(smg, server, app_config):
    """Scraper -> strategy -> executor -> confirmation -> leaderboard, in LIVE mode on the mock site."""
    from smg_trader.alerts import AlertManager
    from smg_trader.market_data import Fundamentals, InMemoryProvider
    from smg_trader.pipeline import Pipeline, RunOptions

    from .conftest import MARKET_OPEN_TIME, make_prices

    prices = {"XOM": make_prices(0.004, seed=1, volume_surge=True, start_price=60),
              "KO": make_prices(-0.004, seed=2, volume_surge=True, start_price=150),
              "AAPL": make_prices(0.0, seed=3, noise=0.002, start_price=190),
              "SPY": make_prices(0.0005, seed=4, start_price=500)}
    # Align the latest synthetic closes with the mock's quotes.
    for t, px in {"XOM": 110.0, "KO": 60.0, "AAPL": 190.0}.items():
        prices[t] = prices[t] * (px / prices[t]["Close"].iloc[-1])
    fund = {t: Fundamentals(exchange="NYSE" if t != "AAPL" else "NASDAQ") for t in ("XOM", "KO", "AAPL")}
    provider = InMemoryProvider(prices, fund)

    app_config.live_trading_enabled = True
    app_config.strategy.universe = ["XOM", "KO", "AAPL"]
    app_config.strategy.weights = {"momentum": 1.0}
    app_config.smg = smg.cfg
    alerts = AlertManager([])
    pipe = Pipeline(app_config, provider=provider, alerts=alerts, clock=lambda: MARKET_OPEN_TIME,
                    scraper_factory=lambda: smg)
    summary = pipe.run_once(RunOptions(live=True, check_leaderboard=True))

    by_ticker = {o["ticker"]: o for o in summary["orders"]}
    assert by_ticker["KO"]["action"] == "SELL" and by_ticker["KO"]["status"] == "CONFIRMED"
    assert by_ticker["XOM"]["action"] == "BUY" and by_ticker["XOM"]["status"] == "CONFIRMED"
    assert "KO" not in server.state.holdings and server.state.holdings["XOM"]["shares"] > 0
    assert summary["leaderboard"]["our_rank"] == 1
    assert {a.kind for a in alerts.sent} >= {"trade"}
