"""End-to-end run: scrape -> signals -> orders -> validate/execute -> monitor."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .alerts import AlertManager, build_alert_manager
from .config import AppConfig
from .executor import TradeExecutor, TradeLog
from .lock import TraderLock
from .market_data import MarketDataProvider, make_provider
from .market_hours import market_status, now_et
from .models import Action, Portfolio, Signal, TradeStatus
from .monitor import LeaderboardMonitor, PerformanceTracker, alert_high_confidence
from .pit import PointInTimeProvider, PointInTimeStore, record_snapshot
from .planner import plan_orders
from .rules import RuleValidator
from .scraper import SMGScraper
from .strategies import Strategy, StrategyContext, build_strategy

log = logging.getLogger(__name__)


class LiveTradingDisabled(RuntimeError):
    pass


@dataclass
class RunOptions:
    live: bool = False              # actually submit orders
    offline: bool = False           # don't log in; use a paper portfolio file
    ignore_hours: bool = False      # skip the market-hours rule (dry runs only)
    check_leaderboard: bool | None = None  # None = on the configured weekday
    strategy: str | None = None     # run one strategy instead of the ensemble
    portfolio_file: str | None = None


class Pipeline:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        provider: MarketDataProvider | None = None,
        scraper_factory: Callable[[], SMGScraper] | None = None,
        alerts: AlertManager | None = None,
        clock: Callable[[], datetime] = now_et,
    ):
        self.cfg = cfg
        self._provider = provider
        self.scraper_factory = scraper_factory or (lambda: SMGScraper(cfg.smg))
        self.alerts = alerts or build_alert_manager(cfg.alerts)
        self.clock = clock
        data = cfg.data_path
        self.lock = TraderLock(data / "trader.lock")
        self.trade_log = TradeLog(data / "trades.csv")
        self.performance = PerformanceTracker(data / "portfolio_history.csv", cfg.starting_equity,
                                              self.alerts, cfg.alerts.drawdown_threshold)
        self.leaderboard = LeaderboardMonitor(data / "leaderboard.json", cfg.smg.team_name, self.alerts)
        self.pit_store = PointInTimeStore(data / "pit")
        self._exchanges: dict[str, str | None] = {}

    @property
    def provider(self) -> MarketDataProvider:
        if self._provider is None:
            base = make_provider(self.cfg.market_data, self.cfg.market_data_dir)
            self._provider = PointInTimeProvider(base, self.pit_store)
        return self._provider

    def exchange_of(self, ticker: str) -> str | None:
        if ticker not in self._exchanges:
            self._exchanges[ticker] = self.provider.exchange(ticker)
        return self._exchanges[ticker]

    # ------------------------------------------------------------------ pieces
    def paper_portfolio(self, path: str | None) -> Portfolio:
        file = Path(path) if path else self.cfg.data_path / "paper_portfolio.json"
        if file.exists():
            return Portfolio.from_dict(json.loads(file.read_text()))
        log.info("No %s; using an empty portfolio with $%s cash", file, f"{self.cfg.starting_equity:,.0f}")
        return Portfolio(cash=self.cfg.starting_equity, total_equity=self.cfg.starting_equity)

    def make_context(self) -> StrategyContext:
        return StrategyContext(self.provider, as_of=self.clock().date(),
                               lookback_days=self.cfg.strategy.lookback_days,
                               benchmark=self.cfg.strategy.benchmark, live=True)

    def universe(self, portfolio: Portfolio) -> list[str]:
        return list(dict.fromkeys([t.upper() for t in self.cfg.strategy.universe]
                                  + [h.ticker for h in portfolio.holdings]))

    def generate_signals(self, portfolio: Portfolio, strategy_name: str | None = None,
                         ctx: StrategyContext | None = None) -> list[Signal]:
        strategy: Strategy = build_strategy(self.cfg.strategy, only=strategy_name)
        ctx = ctx or self.make_context()
        signals = strategy.generate_all(self.universe(portfolio), ctx)
        for s in signals:
            if s.action != Action.HOLD:
                s.price = self.provider.latest_price(s.ticker) or s.price
        signals.sort(key=lambda s: (s.action == Action.HOLD, -s.confidence))
        self._save_signals(signals)
        return signals

    def _save_signals(self, signals: list[Signal]) -> None:
        path = self.cfg.data_path / "signals.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["timestamp", "ticker", "action", "confidence", "score", "price", "strategy", "reason"])
            for s in signals:
                w.writerow([s.timestamp.isoformat(timespec="seconds"), s.ticker, s.action.value,
                            f"{s.confidence:.3f}", f"{s.score:+.3f}", f"{s.price or 0:.2f}", s.strategy,
                            s.reason[:500]])

    def _leaderboard_due(self, opts: RunOptions) -> bool:
        if opts.check_leaderboard is not None:
            return opts.check_leaderboard
        return self.clock().strftime("%a").lower() == self.cfg.leaderboard_day.lower()[:3]

    # ------------------------------------------------------------------ run
    def run_once(self, opts: RunOptions | None = None) -> dict:
        opts = opts or RunOptions()
        if opts.live and not self.cfg.live_trading_enabled:
            raise LiveTradingDisabled(
                "Live mode needs both --live and live_trading_enabled: true (or SMG_LIVE_TRADING=1)."
            )
        if opts.live and opts.offline:
            raise ValueError("--live cannot be combined with --offline")
        if opts.live and opts.ignore_hours:
            raise ValueError("--ignore-hours is only allowed in dry-run mode")

        mode = "LIVE" if opts.live else "DRY-RUN"
        log.info("=== SMG bot run (%s%s) - market %s ===", mode, ", offline" if opts.offline else "",
                 market_status(self.clock()))
        scraper: SMGScraper | None = None
        self.lock.acquire()
        try:
            if opts.offline:
                portfolio = self.paper_portfolio(opts.portfolio_file)
            else:
                scraper = self.scraper_factory().start()
                portfolio = scraper.get_portfolio()
            perf = self.performance.record(portfolio)

            ctx = self.make_context()
            signals = self.generate_signals(portfolio, opts.strategy, ctx)
            if self.cfg.record_point_in_time:
                n = record_snapshot(self.pit_store, self.provider, self.universe(portfolio), ctx.as_of)
                log.debug("recorded %d point-in-time rows", n)
            for s in signals:
                if s.action != Action.HOLD:
                    log.info("SIGNAL %-4s %-5s conf %.2f @ $%.2f  %s", s.action.value, s.ticker,
                             s.confidence, s.price or 0, s.reason[:160])
            log.info("%d signals (%d actionable) for %d tickers", len(signals),
                     sum(s.action != Action.HOLD for s in signals), len(signals))
            alert_high_confidence(signals, self.alerts, self.cfg.alerts.high_confidence_threshold)

            plan = plan_orders(signals, portfolio, ctx, rules=self.cfg.rules, portfolio_cfg=self.cfg.portfolio,
                               pairs_cfg=self.cfg.pairs, min_confidence=self.cfg.strategy.min_confidence_to_trade)
            if plan.pair_signals:
                self._save_signals(plan.pair_signals)
            orders = plan.orders
            validator = RuleValidator(self.cfg.rules, self.exchange_of, lock=self.lock, clock=self.clock)
            executor = TradeExecutor(validator, self.trade_log, self.alerts, broker=scraper,
                                     dry_run=not opts.live, check_hours=not opts.ignore_hours)
            results, after = executor.execute(orders, portfolio)

            board = None
            if scraper is not None and self._leaderboard_due(opts):
                try:
                    entries = scraper.get_leaderboard(top_n=10)
                    board = self.leaderboard.update(entries)
                except Exception as exc:
                    log.error("leaderboard check failed: %s", exc)
                    self.alerts.send("error", "Leaderboard check failed", str(exc))

            summary = {
                "timestamp": self.clock().isoformat(timespec="seconds"),
                "mode": mode.lower(),
                "offline": opts.offline,
                "portfolio": portfolio.to_dict(),
                "performance": perf.__dict__,
                "signals": [s.to_dict() for s in signals],
                "pair_signals": [s.to_dict() for s in plan.pair_signals],
                "target_weights": plan.target_weights,
                "orders": [
                    {"ticker": r.order.ticker, "action": r.order.action.value, "shares": r.order.shares,
                     "price": r.order.price, "status": r.status.value, "message": r.message,
                     "group": r.order.group_id}
                    for r in results
                ],
                "portfolio_after": after.to_dict(),
                "leaderboard": board,
            }
            self._save_run(summary)
            n_ok = sum(r.status not in (TradeStatus.REJECTED, TradeStatus.FAILED) for r in results)
            log.info("=== done: %d orders, %d %s, %d rejected ===", len(results), n_ok,
                     "validated (dry run)" if not opts.live else "sent", len(results) - n_ok)
            return summary
        except Exception as exc:
            self.alerts.send("error", f"SMG bot run failed: {type(exc).__name__}", str(exc)[:1000])
            raise
        finally:
            if scraper is not None:
                scraper.close()
            self.lock.release()

    def check_leaderboard(self) -> dict:
        with self.scraper_factory() as scraper:
            entries = scraper.get_leaderboard(top_n=10)
        return self.leaderboard.update(entries)

    def _save_run(self, summary: dict) -> None:
        runs = self.cfg.data_path / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"{self.clock():%Y%m%d-%H%M%S}-{summary['mode']}.json"
        path.write_text(json.dumps(summary, indent=2, default=str))
