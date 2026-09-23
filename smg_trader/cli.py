"""Command line interface: ``python -m smg_trader <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta

from .config import AppConfig, load_config

log = logging.getLogger("smg_trader")


def _setup_logging(cfg: AppConfig, verbose: bool) -> None:
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout),
                                       logging.FileHandler(cfg.data_path / "bot.log")]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, handlers=handlers)
    for noisy in ("urllib3", "selenium", "yfinance", "peewee", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def cmd_check(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Scraper smoke test: login, portfolio, cash, leaderboard."""
    from .scraper import SMGScraper

    report: dict = {}
    with SMGScraper(cfg.smg) as smg:
        smg.login()
        report["logged_in"] = smg.is_logged_in()
        portfolio = smg.get_portfolio()
        report["portfolio"] = portfolio.to_dict()
        report["types_ok"] = all(
            isinstance(h.ticker, str) and isinstance(h.shares, int) and isinstance(h.current_price, float)
            for h in portfolio.holdings
        ) and isinstance(portfolio.cash, float)
        if not args.skip_leaderboard:
            report["leaderboard"] = [e.to_dict() for e in smg.get_leaderboard(top_n=10)]
    _print_json(report)
    return 0 if report["logged_in"] and report["types_ok"] else 1


def cmd_inspect(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .scraper import SMGScraper

    cfg.smg.headless = not args.show_browser
    with SMGScraper(cfg.smg) as smg:
        for path in smg.inspect():
            print(f"saved {path}.html")
    print(f"\nOpen the files in {cfg.smg.debug_dir}/ and put selectors under smg.selectors in config.yaml.")
    return 0


def cmd_signals(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .pipeline import Pipeline

    pipeline = Pipeline(cfg)
    portfolio = pipeline.paper_portfolio(args.portfolio_file)
    signals = pipeline.generate_signals(portfolio, args.strategy)
    print(f"{'TICKER':<7}{'ACTION':<7}{'CONF':>6}{'SCORE':>8}{'PRICE':>10}  REASON")
    for s in signals:
        print(f"{s.ticker:<7}{s.action.value:<7}{s.confidence:>6.2f}{s.score:>+8.2f}{s.price or 0:>10.2f}  "
              f"{s.reason[:args.width]}")
    return 0


def _run_opts(args: argparse.Namespace):
    from .pipeline import RunOptions

    return RunOptions(
        live=getattr(args, "live", False),
        offline=getattr(args, "offline", False),
        ignore_hours=getattr(args, "ignore_hours", False),
        check_leaderboard=True if getattr(args, "leaderboard", False) else None,
        strategy=getattr(args, "strategy", None),
        portfolio_file=getattr(args, "portfolio_file", None),
    )


def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .pipeline import Pipeline

    summary = Pipeline(cfg).run_once(_run_opts(args))
    print(f"\n{'ORDER':<24}{'STATUS':<13}MESSAGE")
    for o in summary["orders"]:
        print(f"{o['action']:<5}{o['shares']:>5} {o['ticker']:<6}@{o['price']:>7.2f} {o['status']:<13}{o['message']}")
    if not summary["orders"]:
        print("(no orders)")
    return 0


def cmd_trade(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Single manual order through the same validation/execution path."""
    from .executor import TradeExecutor
    from .models import Action, TradeOrder
    from .pipeline import LiveTradingDisabled, Pipeline
    from .rules import RuleValidator
    from .scraper import SMGScraper

    if args.live and not cfg.live_trading_enabled:
        raise LiveTradingDisabled("Set live_trading_enabled: true (or SMG_LIVE_TRADING=1) to trade live.")
    pipeline = Pipeline(cfg)
    price = args.price or pipeline.provider.latest_price(args.ticker)
    if price is None:
        print(f"No price for {args.ticker}; pass --price")
        return 2
    order = TradeOrder(args.ticker.upper(), Action(args.action.upper()), args.shares, float(price),
                       confidence=1.0, reason="manual trade")
    with pipeline.lock:
        scraper = SMGScraper(cfg.smg).start() if not args.offline else None
        try:
            portfolio = scraper.get_portfolio() if scraper else pipeline.paper_portfolio(None)
            validator = RuleValidator(cfg.rules, pipeline.exchange_of, lock=pipeline.lock)
            executor = TradeExecutor(validator, pipeline.trade_log, pipeline.alerts, broker=scraper,
                                     dry_run=not args.live, check_hours=not args.ignore_hours)
            results, _ = executor.execute([order], portfolio)
        finally:
            if scraper:
                scraper.close()
    r = results[0]
    print(f"{r.status.value}: {r.message}")
    return 0 if r.status.value not in ("REJECTED", "FAILED") else 1


def cmd_backtest(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .backtest import Backtester
    from .market_data import CSVProvider, make_provider
    from .strategies import build_strategy

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=int(args.months * 30.4))
    provider = CSVProvider(args.data_dir) if args.data_dir else make_provider(cfg.market_data, cfg.market_data_dir)
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else cfg.strategy.universe
    bt = Backtester(
        build_strategy(cfg.strategy, only=args.strategy), provider, tickers, cfg.rules,
        initial_cash=cfg.starting_equity, min_confidence=cfg.strategy.min_confidence_to_trade,
        benchmark=cfg.strategy.benchmark, rebalance_every=args.rebalance,
        lookback_days=cfg.strategy.lookback_days,
    )
    result = bt.run(start, end)
    print(f"\nBacktest: {args.strategy or 'ensemble'} on {len(tickers)} tickers\n")
    print(result.summary())
    out = args.out or str(cfg.data_path / "backtest")
    result.save(out)
    print(f"\nEquity curve and trades saved to {out}/")
    return 0


def cmd_leaderboard(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .pipeline import Pipeline

    _print_json(Pipeline(cfg).check_leaderboard())
    return 0


def cmd_schedule(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .scheduler import run_scheduler

    run_scheduler(cfg, _run_opts(args))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smg_trader", description="Stock Market Game automated trader")
    p.add_argument("-c", "--config", help="YAML config (default: config.yaml / $SMG_CONFIG)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("check", help="scraper test: login, portfolio, cash, leaderboard")
    s.add_argument("--skip-leaderboard", action="store_true")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("inspect", help="save SMG pages (HTML + screenshots) for selector tuning")
    s.add_argument("--show-browser", action="store_true", help="run Chrome with a visible window")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("signals", help="print strategy signals for the universe (no login)")
    s.add_argument("--strategy", help="momentum | technical | fundamentals | ensemble (default)")
    s.add_argument("--portfolio-file", help="paper portfolio JSON (adds its holdings to the universe)")
    s.add_argument("--width", type=int, default=110, help="reason column width")
    s.set_defaults(func=cmd_signals)

    for name, helptext in (("run", "one full pipeline run"), ("schedule", "run daily via APScheduler")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--live", action="store_true", help="submit orders (also needs live_trading_enabled)")
        s.add_argument("--strategy", help="use one strategy instead of the ensemble")
        if name == "run":
            s.add_argument("--offline", action="store_true", help="don't log in; use a paper portfolio")
            s.add_argument("--portfolio-file", help="paper portfolio JSON for --offline")
            s.add_argument("--ignore-hours", action="store_true", help="skip market-hours rule (dry run only)")
            s.add_argument("--leaderboard", action="store_true", help="also check the leaderboard")
            s.set_defaults(func=cmd_run)
        else:
            s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("trade", help="one manual order, e.g. `trade KO buy 10`")
    s.add_argument("ticker")
    s.add_argument("action", choices=["buy", "sell", "BUY", "SELL"])
    s.add_argument("shares", type=int)
    s.add_argument("--price", type=float, help="override the price used for validation")
    s.add_argument("--live", action="store_true")
    s.add_argument("--offline", action="store_true", help="validate against the paper portfolio")
    s.add_argument("--ignore-hours", action="store_true")
    s.set_defaults(func=cmd_trade)

    s = sub.add_parser("backtest", help="walk-forward backtest")
    s.add_argument("--strategy", default="momentum", help="momentum (default) | technical | ensemble ...")
    s.add_argument("--start", help="YYYY-MM-DD (default: --months before --end)")
    s.add_argument("--end", help="YYYY-MM-DD (default: yesterday)")
    s.add_argument("--months", type=float, default=2.0)
    s.add_argument("--tickers", help="comma-separated (default: config universe)")
    s.add_argument("--data-dir", help="folder of <TICKER>.csv files instead of Yahoo Finance")
    s.add_argument("--rebalance", type=int, default=1, help="rebalance every N trading days")
    s.add_argument("--out", help="output folder (default: data/backtest)")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("leaderboard", help="check NC rankings now and alert on changes")
    s.set_defaults(func=cmd_leaderboard)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    _setup_logging(cfg, args.verbose)
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc, exc_info=args.verbose)
        return 1
