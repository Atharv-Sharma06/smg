from datetime import date

import pytest

from smg_trader.backtest import Backtester
from smg_trader.config import StrategyConfig
from smg_trader.market_data import CSVProvider, InMemoryProvider
from smg_trader.models import Action
from smg_trader.strategies import Strategy, build_strategy

from .test_strategies import ALL, UP

START, END = date(2026, 7, 22), date(2026, 9, 22)  # two months


def test_momentum_backtest_two_months(universe_provider, rules, tmp_path):
    bt = Backtester(build_strategy(StrategyConfig(), only="momentum"), universe_provider, ALL, rules,
                    initial_cash=100_000, min_confidence=0.4)
    result = bt.run(START, END)
    m = result.metrics
    assert m["days"] > 40
    assert m["trades"] > 0 and m["buys"] > 0
    bought = set(result.trades.loc[result.trades.action == "BUY", "ticker"])
    assert bought <= set(UP)  # only buys uptrending names
    assert m["total_return"] > 0
    assert (result.trades["shares"] >= rules.min_shares_per_trade).all()
    assert (result.trades["fill_date"] > result.trades["signal_date"]).all()  # fills next day
    assert "Total return" in result.summary()
    result.save(tmp_path)
    assert (tmp_path / "backtest_trades.csv").exists() and (tmp_path / "backtest_equity.csv").exists()


def test_backtest_has_no_lookahead(universe_provider, rules):
    seen = []

    class Spy(Strategy):
        name = "spy"

        def score(self, ticker, ctx):
            seen.append((ctx.as_of, ctx.history(ticker).index.max().date(), ctx.live))
            return 0.0, "", {}

    Backtester(Spy(), universe_provider, ["UPA"], rules).run(START, END)
    assert seen and all(last <= as_of and not live for as_of, last, live in seen)


def test_backtest_sells_on_signal(universe_provider, rules):
    class Flip(Strategy):
        name = "flip"

        def score(self, ticker, ctx):
            return (1.0 if ctx.as_of.day % 2 else -1.0), "flip", {}

    result = Backtester(Flip(), universe_provider, ["UPA"], rules).run(START, END)
    assert {Action.BUY.value, Action.SELL.value} <= set(result.trades.action)
    assert result.metrics["closed_trades"] > 0


def test_csv_provider_backtest(universe_provider, rules, tmp_path):
    for t, df in universe_provider.prices.items():
        df.to_csv(tmp_path / f"{t}.csv", index_label="Date")
    provider = CSVProvider(tmp_path)
    assert isinstance(provider, InMemoryProvider) and "UPA" in provider.prices
    result = Backtester(build_strategy(StrategyConfig(), only="momentum"), provider, UP, rules).run(START, END)
    assert result.metrics["trades"] > 0


def test_backtest_without_data_errors(rules):
    with pytest.raises(ValueError):
        Backtester(build_strategy(StrategyConfig(), only="momentum"), InMemoryProvider({}), ["X"], rules).run(
            START, END)


def test_pairs_backtest_trades_linked_legs(rules):
    from smg_trader.config import PairsConfig

    from .test_pairs import make_pair

    a, b = make_pair(n=320, seed=7, half_life=4.0, noise=0.006)
    provider = InMemoryProvider({"AAA": a, "BBB": b})
    rules.allow_short_selling = True
    pairs = PairsConfig(enabled=True, pairs=[["AAA", "BBB"]], capital_pct=0.5, min_correlation=0.5)
    result = Backtester(build_strategy(StrategyConfig(), only="momentum"), provider, [], rules,
                        pairs_cfg=pairs).run(START, END)
    t = result.trades
    assert {"SHORT", "COVER"} <= set(t.action)
    assert (t.group == "pair:AAA/BBB").all()
    assert (t.groupby(["signal_date", "group"]).size() == 2).all()  # legs always together
    assert result.metrics["closed_trades"] > 0 and result.metrics["pair_trades"] == len(t)
    assert "covers" in result.summary()


@pytest.mark.parametrize("method", ["equal", "inverse_vol", "risk_parity", "mean_variance"])
def test_target_weight_backtest(universe_provider, rules, method):
    from smg_trader.config import PortfolioConfig

    cfg = PortfolioConfig(method=method, max_positions=4, long_exposure=0.8)
    result = Backtester(build_strategy(StrategyConfig(), only="momentum"), universe_provider, ALL, rules,
                        portfolio_cfg=cfg, rebalance_every=5).run(START, END)
    buys = result.trades[result.trades.action == "BUY"]
    assert len(buys) > 0 and set(buys.ticker) <= set(UP)
    assert result.metrics["total_return"] > 0
