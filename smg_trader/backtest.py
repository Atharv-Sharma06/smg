"""Walk-forward backtester.

On each rebalance day the strategy only sees data up to that day's close
(``StrategyContext.as_of``) with ``live=False``, so news, analyst and
fundamental inputs abstain. Orders go through the same sizing and SMG rule
validation as live trading (no market-hours check) and fill at the **next
day's open**, with the SMG commission.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import RulesConfig
from .executor import apply_order
from .market_data import SECTOR_ETFS, InMemoryProvider, MarketDataProvider
from .models import Action, Portfolio
from .rules import RuleValidator
from .sizing import build_orders
from .strategies import Strategy, StrategyContext

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    metrics: dict[str, float]
    signals_generated: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        m = self.metrics
        lines = [
            f"Period            {m['start']} -> {m['end']} ({m['days']} trading days)",
            f"Final equity      ${m['final_equity']:,.2f}",
            f"Total return      {m['total_return']:+.2%}",
            f"Benchmark return  {m['benchmark_return']:+.2%}" if not math.isnan(m["benchmark_return"]) else
            "Benchmark return  n/a",
            f"Equal-wt universe {m['equal_weight_return']:+.2%}",
            f"Max drawdown      {m['max_drawdown']:.2%}",
            f"Sharpe (daily)    {m['sharpe']:.2f}",
            f"Trades            {int(m['trades'])} ({int(m['buys'])} buys / {int(m['sells'])} sells)",
            f"Closed-trade wins {m['win_rate']:.0%} of {int(m['closed_trades'])}" if m["closed_trades"] else
            "Closed-trade wins n/a (no round trips)",
            f"Rejected orders   {int(m['rejected'])}",
        ]
        return "\n".join(lines + [f"Note: {n}" for n in self.notes])

    def save(self, directory: str | Path) -> None:
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        self.equity.rename("equity").to_csv(out / "backtest_equity.csv", index_label="date")
        self.trades.to_csv(out / "backtest_trades.csv", index=False)
        (out / "backtest_summary.txt").write_text(self.summary() + "\n")


def preload(provider: MarketDataProvider, tickers: list[str], start: date, end: date) -> InMemoryProvider:
    """Fetch everything once so the day-by-day loop never hits the network."""
    fundamentals = {}
    for t in tickers:
        f = provider.fundamentals(t)
        if f is not None:
            fundamentals[t] = f
    etfs = {SECTOR_ETFS[f.sector] for f in fundamentals.values() if f.sector in SECTOR_ETFS}
    prices = {}
    for t in sorted(set(tickers) | etfs):
        df = provider.history(t, start, end)
        if not df.empty:
            prices[t] = df
    return InMemoryProvider(prices, fundamentals)


class Backtester:
    def __init__(
        self,
        strategy: Strategy,
        provider: MarketDataProvider,
        tickers: list[str],
        rules: RulesConfig,
        *,
        initial_cash: float = 100_000.0,
        min_confidence: float = 0.4,
        benchmark: str = "SPY",
        rebalance_every: int = 1,
        lookback_days: int = 400,
    ):
        self.strategy = strategy
        self.tickers = [t.upper() for t in tickers]
        self.rules = rules
        self.initial_cash = initial_cash
        self.min_confidence = min_confidence
        self.benchmark = benchmark.upper()
        self.rebalance_every = max(1, rebalance_every)
        self.lookback_days = lookback_days
        self.source = provider

    def run(self, start: date, end: date) -> BacktestResult:
        data = preload(self.source, self.tickers + [self.benchmark],
                       start - timedelta(days=self.lookback_days), end + timedelta(days=7))
        notes = []
        missing = [t for t in self.tickers if t not in data.prices]
        if missing:
            notes.append(f"no price data for {', '.join(missing)}")
        tickers = [t for t in self.tickers if t in data.prices]
        if not tickers:
            raise ValueError("No price data for any ticker in the universe")

        calendar_src = data.prices.get(self.benchmark)
        if calendar_src is None:
            calendar_src = data.prices[tickers[0]]
        all_days = calendar_src.index
        days = all_days[(all_days >= pd.Timestamp(start)) & (all_days <= pd.Timestamp(end))]
        if len(days) < 2:
            raise ValueError(f"Not enough trading days between {start} and {end}")

        closes = pd.DataFrame({t: data.prices[t]["Close"] for t in tickers}).reindex(all_days).ffill()
        opens = pd.DataFrame({t: data.prices[t]["Open"] for t in tickers}).reindex(all_days)
        opens = opens.fillna(closes)

        # Backtest can't look up exchanges historically; universe is assumed pre-screened.
        validator = RuleValidator(self.rules, lambda t: self.rules.allowed_exchanges[0])
        portfolio = Portfolio(cash=self.initial_cash, total_equity=self.initial_cash)
        equity_points: dict[pd.Timestamp, float] = {}
        trades: list[dict] = []
        cost_basis: dict[str, float] = {}
        closed_pnls: list[float] = []
        rejected = 0
        n_signals = 0

        for i, day in enumerate(days):
            self._mark(portfolio, closes.loc[day])
            equity_points[day] = portfolio.total_equity
            if i % self.rebalance_every or i == len(days) - 1:
                continue
            next_day = all_days[all_days.get_loc(day) + 1]

            ctx = StrategyContext(data, as_of=day.date(), lookback_days=self.lookback_days,
                                  benchmark=self.benchmark, live=False)
            signals = self.strategy.generate_all(tickers, ctx)
            n_signals += len(signals)
            orders = build_orders(signals, portfolio, self.rules, min_confidence=self.min_confidence)
            accepted = 0
            for order in orders:
                fill = float(opens.at[next_day, order.ticker])
                if math.isnan(fill):
                    continue
                if order.action == Action.BUY and fill > order.price:
                    # Re-size so a gap up doesn't overspend.
                    budget = order.shares * order.price
                    order.shares = int(budget // (fill * (1 + self.rules.commission_rate)))
                order.price = fill
                v = validator.validate(order, portfolio, trades_this_run=accepted, check_hours=False)
                if not v.ok:
                    rejected += 1
                    log.debug("%s rejected %s: %s", day.date(), order.ticker, v.errors)
                    continue
                accepted += 1
                held_before = portfolio.shares_of(order.ticker)
                if order.action == Action.SELL and order.ticker in cost_basis:
                    closed_pnls.append((fill * (1 - self.rules.commission_rate) - cost_basis[order.ticker])
                                       * order.shares)
                portfolio = apply_order(portfolio, order, self.rules.commission_rate)
                if order.action == Action.BUY:
                    old = cost_basis.get(order.ticker, 0.0) * held_before
                    cost_basis[order.ticker] = (old + order.notional * (1 + self.rules.commission_rate)) / (
                        held_before + order.shares)
                elif portfolio.shares_of(order.ticker) == 0:
                    cost_basis.pop(order.ticker, None)
                trades.append({
                    "signal_date": day.date().isoformat(), "fill_date": next_day.date().isoformat(),
                    "ticker": order.ticker, "action": order.action.value, "shares": order.shares,
                    "price": round(fill, 4), "confidence": order.confidence, "reason": order.reason[:300],
                })

        equity = pd.Series(equity_points, dtype=float)
        trades_df = pd.DataFrame(trades, columns=["signal_date", "fill_date", "ticker", "action", "shares",
                                                  "price", "confidence", "reason"])
        metrics = self._metrics(equity, trades_df, closed_pnls, rejected, data, days, closes[tickers])
        return BacktestResult(equity, trades_df, metrics, n_signals, notes)

    @staticmethod
    def _mark(portfolio: Portfolio, prices: pd.Series) -> None:
        for h in portfolio.holdings:
            px = prices.get(h.ticker)
            if px is not None and not math.isnan(px):
                h.current_price = float(px)
                h.market_value = h.shares * float(px)
        portfolio.total_equity = portfolio.cash + portfolio.invested_value

    def _metrics(self, equity: pd.Series, trades: pd.DataFrame, closed: list[float], rejected: int,
                 data: InMemoryProvider, days: pd.DatetimeIndex, closes: pd.DataFrame) -> dict[str, float]:
        rets = equity.pct_change().dropna()
        peak = equity.cummax()
        bench = data.prices.get(self.benchmark)
        bench_ret = float("nan")
        if bench is not None:
            b = bench["Close"].reindex(days).ffill().dropna()
            if len(b) >= 2:
                bench_ret = float(b.iloc[-1] / b.iloc[0] - 1)
        window = closes.loc[days[0]: days[-1]]
        ew = (window.iloc[-1] / window.iloc[0] - 1).mean()
        return {
            "start": days[0].date().isoformat(),
            "end": days[-1].date().isoformat(),
            "days": len(days),
            "final_equity": float(equity.iloc[-1]),
            "total_return": float(equity.iloc[-1] / self.initial_cash - 1),
            "benchmark_return": bench_ret,
            "equal_weight_return": float(ew),
            "max_drawdown": float((1 - equity / peak).max()),
            "sharpe": float(rets.mean() / rets.std() * np.sqrt(252)) if len(rets) > 1 and rets.std() > 0 else 0.0,
            "trades": len(trades),
            "buys": int((trades["action"] == "BUY").sum()) if len(trades) else 0,
            "sells": int((trades["action"] == "SELL").sum()) if len(trades) else 0,
            "closed_trades": len(closed),
            "win_rate": sum(p > 0 for p in closed) / len(closed) if closed else 0.0,
            "rejected": rejected,
        }
