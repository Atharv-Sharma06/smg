"""Walk-forward backtester.

On each rebalance day the strategy only sees data up to that day's close
(``StrategyContext.as_of``, ``live=False``). Fundamentals, analyst actions and
news come from the provider's point-in-time data, so they only count when
they were actually known on that date (``metrics['pit_coverage']`` shows how
often). Orders go through the same planner as live runs (signal sizing or
target-weight rebalancing, pairs, shorts), the same SMG rule validation (no
market-hours check), linked legs all-or-nothing, and fill at the **next
day's open** with the SMG commission.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .config import PairsConfig, PortfolioConfig, RulesConfig
from .executor import apply_order, group_orders, validate_group
from .market_data import SECTOR_ETFS, AnalystActions, Fundamentals, InMemoryProvider, MarketDataProvider
from .models import Action, Portfolio
from .planner import plan_orders
from .rules import RuleValidator
from .strategies import PairsTrader, Strategy, StrategyContext

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
            f"Trades            {int(m['trades'])} ({int(m['buys'])} buys / {int(m['sells'])} sells / "
            f"{int(m['shorts'])} shorts / {int(m['covers'])} covers)",
            f"Pair trades       {int(m['pair_trades'])} linked orders",
            f"Closed-trade wins {m['win_rate']:.0%} of {int(m['closed_trades'])}" if m["closed_trades"] else
            "Closed-trade wins n/a (no round trips)",
            f"Rejected orders   {int(m['rejected'])}",
            "Point-in-time     " + ", ".join(f"{k} {v:.0%}" for k, v in m["pit_coverage"].items())
            + " of ticker-days had data",
        ]
        return "\n".join(lines + [f"Note: {n}" for n in self.notes])

    def save(self, directory: str | Path) -> None:
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        self.equity.rename("equity").to_csv(out / "backtest_equity.csv", index_label="date")
        self.trades.to_csv(out / "backtest_trades.csv", index=False)
        (out / "backtest_summary.txt").write_text(self.summary() + "\n")


class PreloadedProvider(InMemoryProvider):
    """Prices in memory; point-in-time lookups delegated (and memoised) to the source."""

    def __init__(self, prices: dict[str, pd.DataFrame], fundamentals: dict[str, Fundamentals],
                 source: MarketDataProvider):
        super().__init__(prices, fundamentals)
        self.source = source
        self._memo: dict[tuple, Any] = {}

    def _memoised(self, key: tuple, fetch: Callable[[], Any]) -> Any:
        if key not in self._memo:
            try:
                self._memo[key] = fetch()
            except Exception as exc:
                log.debug("point-in-time lookup %s failed: %s", key, exc)
                self._memo[key] = None
        return self._memo[key]

    def fundamentals_at(self, ticker: str, as_of: date) -> Fundamentals | None:
        return self._memoised(("f", ticker, as_of), lambda: self.source.fundamentals_at(ticker, as_of))

    def analyst_actions_at(self, ticker: str, start: date, end: date) -> AnalystActions | None:
        return self._memoised(("a", ticker, start, end), lambda: self.source.analyst_actions_at(ticker, start, end))

    def news_at(self, ticker: str, start: date, end: date) -> list[str] | None:
        return self._memoised(("n", ticker, start, end), lambda: self.source.news_at(ticker, start, end))


def preload(provider: MarketDataProvider, tickers: list[str], start: date, end: date) -> PreloadedProvider:
    """Fetch prices once so the day-by-day loop never re-downloads them."""
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
    return PreloadedProvider(prices, fundamentals, provider)


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
        portfolio_cfg: PortfolioConfig | None = None,
        pairs_cfg: PairsConfig | None = None,
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
        self.portfolio_cfg = portfolio_cfg or PortfolioConfig()
        self.pairs_cfg = pairs_cfg or PairsConfig()

    def run(self, start: date, end: date) -> BacktestResult:
        pair_tickers = sorted(PairsTrader(self.pairs_cfg, self.rules).tickers) if self.pairs_cfg.enabled else []
        universe = list(dict.fromkeys(self.tickers + pair_tickers))
        data = preload(self.source, universe + [self.benchmark],
                       start - timedelta(days=self.lookback_days), end + timedelta(days=7))
        notes = []
        missing = [t for t in universe if t not in data.prices]
        if missing:
            notes.append(f"no price data for {', '.join(missing)}")
        tickers = [t for t in self.tickers if t in data.prices]
        traded = [t for t in universe if t in data.prices]
        if not traded:
            raise ValueError("No price data for any ticker in the universe")

        calendar_src = data.prices.get(self.benchmark)
        if calendar_src is None:
            calendar_src = data.prices[traded[0]]
        all_days = calendar_src.index
        days = all_days[(all_days >= pd.Timestamp(start)) & (all_days <= pd.Timestamp(end))]
        if len(days) < 2:
            raise ValueError(f"Not enough trading days between {start} and {end}")

        closes = pd.DataFrame({t: data.prices[t]["Close"] for t in traded}).reindex(all_days).ffill()
        opens = pd.DataFrame({t: data.prices[t]["Open"] for t in traded}).reindex(all_days)
        opens = opens.fillna(closes)

        rules = self.rules
        # Backtest can't look up exchanges historically; universe is assumed pre-screened.
        validator = RuleValidator(rules, lambda t: rules.allowed_exchanges[0])
        portfolio = Portfolio(cash=self.initial_cash, total_equity=self.initial_cash)
        equity_points: dict[pd.Timestamp, float] = {}
        trades: list[dict] = []
        basis: dict[tuple[str, str], float] = {}   # (ticker, side) -> per-share cost / net proceeds
        closed_pnls: list[float] = []
        rejected = 0
        n_signals = 0
        coverage = {"fundamentals": [0, 0], "analyst": [0, 0], "news": [0, 0]}
        fee = rules.commission_rate

        for i, day in enumerate(days):
            self._mark(portfolio, closes.loc[day])
            equity_points[day] = portfolio.total_equity
            if i % self.rebalance_every or i == len(days) - 1:
                continue
            next_day = all_days[all_days.get_loc(day) + 1]

            ctx = StrategyContext(data, as_of=day.date(), lookback_days=self.lookback_days,
                                  benchmark=self.benchmark, live=False)
            for t in tickers:
                for kind, value in (("fundamentals", ctx.fundamentals(t)), ("analyst", ctx.analyst_actions(t)),
                                    ("news", ctx.news(t))):
                    coverage[kind][0] += value is not None
                    coverage[kind][1] += 1
            signals = self.strategy.generate_all(tickers, ctx)
            n_signals += len(signals)
            plan = plan_orders(signals, portfolio, ctx, rules=rules, portfolio_cfg=self.portfolio_cfg,
                               pairs_cfg=self.pairs_cfg, min_confidence=self.min_confidence)
            accepted = 0
            for legs in group_orders(plan.orders):
                fills = [float(opens.at[next_day, o.ticker]) for o in legs]
                if any(math.isnan(f) for f in fills):
                    continue
                for order, fill in zip(legs, fills):
                    if not order.group_id and order.action == Action.BUY and fill > order.price:
                        # Re-size so a gap up doesn't overspend.
                        order.shares = int(order.shares * order.price // fill)
                    order.price = fill
                vals = validate_group(validator, legs, portfolio, trades_this_run=accepted, check_hours=False)
                if not all(v.ok for v in vals):
                    rejected += len(legs)
                    log.debug("%s rejected %s: %s", day.date(), [o.ticker for o in legs],
                              [v.errors for v in vals])
                    continue
                for order in legs:
                    closed_pnls.extend(self._track(order, portfolio, basis, fee))
                    portfolio = apply_order(portfolio, order, fee,
                                            short_margin_requirement=rules.short_margin_requirement)
                    trades.append({
                        "signal_date": day.date().isoformat(), "fill_date": next_day.date().isoformat(),
                        "ticker": order.ticker, "action": order.action.value, "shares": order.shares,
                        "price": round(order.price, 4), "confidence": order.confidence,
                        "group": order.group_id, "reason": order.reason[:300],
                    })
                accepted += len(legs)

        equity = pd.Series(equity_points, dtype=float)
        trades_df = pd.DataFrame(trades, columns=["signal_date", "fill_date", "ticker", "action", "shares",
                                                  "price", "confidence", "group", "reason"])
        metrics = self._metrics(equity, trades_df, closed_pnls, rejected, data, days, closes[tickers or traded])
        metrics["pit_coverage"] = {k: (hit / total if total else 0.0) for k, (hit, total) in coverage.items()}
        return BacktestResult(equity, trades_df, metrics, n_signals, notes)

    @staticmethod
    def _track(order, portfolio: Portfolio, basis: dict[tuple[str, str], float], fee: float) -> list[float]:
        """Update per-share basis; return realised P&L for closing orders."""
        key = (order.ticker, order.action.position)
        held = portfolio.shares_of(order.ticker, order.action.position)
        if order.action == Action.BUY:
            per_share = order.price * (1 + fee)
        elif order.action == Action.SHORT:
            per_share = order.price * (1 - fee)
        else:
            pnl = []
            if key in basis:
                if order.action == Action.SELL:
                    pnl.append((order.price * (1 - fee) - basis[key]) * order.shares)
                else:  # COVER
                    pnl.append((basis[key] - order.price * (1 + fee)) * order.shares)
            if order.shares >= held:
                basis.pop(key, None)
            return pnl
        basis[key] = (basis.get(key, 0.0) * held + per_share * order.shares) / (held + order.shares)
        return []

    @staticmethod
    def _mark(portfolio: Portfolio, prices: pd.Series) -> None:
        for h in portfolio.holdings:
            px = prices.get(h.ticker)
            if px is not None and not math.isnan(px):
                h.current_price = float(px)
                h.market_value = h.shares * float(px)
        portfolio.total_equity = portfolio.cash + portfolio.invested_value

    def _metrics(self, equity: pd.Series, trades: pd.DataFrame, closed: list[float], rejected: int,
                 data: InMemoryProvider, days: pd.DatetimeIndex, closes: pd.DataFrame) -> dict[str, Any]:
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
        count = (lambda a: int((trades["action"] == a).sum())) if len(trades) else (lambda a: 0)
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
            "buys": count("BUY"),
            "sells": count("SELL"),
            "shorts": count("SHORT"),
            "covers": count("COVER"),
            "pair_trades": int((trades["group"] != "").sum()) if len(trades) else 0,
            "closed_trades": len(closed),
            "win_rate": sum(p > 0 for p in closed) / len(closed) if closed else 0.0,
            "rejected": rejected,
        }
