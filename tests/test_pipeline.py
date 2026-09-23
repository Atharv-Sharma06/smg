import json

import pytest

from smg_trader.alerts import AlertManager
from smg_trader.cli import main
from smg_trader.lock import TraderLockError
from smg_trader.pipeline import LiveTradingDisabled, Pipeline, RunOptions

from .conftest import MARKET_OPEN_TIME


def make_pipeline(cfg, provider, **kw):
    return Pipeline(cfg, provider=provider, alerts=AlertManager([]), clock=lambda: MARKET_OPEN_TIME, **kw)


def test_offline_dry_run_end_to_end(app_config, universe_provider):
    pipe = make_pipeline(app_config, universe_provider)
    summary = pipe.run_once(RunOptions(offline=True))
    assert summary["mode"] == "dry-run"
    assert len(summary["signals"]) == 12
    assert summary["orders"], "expected BUY orders for the uptrending names"
    assert all(o["status"] == "DRY_RUN" for o in summary["orders"])
    assert {o["ticker"] for o in summary["orders"]} <= {"UPA", "UPB", "UPC", "UPD", "UPE", "UPF"}
    data = app_config.data_path
    for name in ("trades.csv", "signals.csv", "portfolio_history.csv"):
        assert (data / name).exists(), name
    assert list((data / "runs").glob("*-dry-run.json"))
    assert not (data / "trader.lock").exists()  # released


def test_offline_uses_paper_portfolio_and_sells_losers(app_config, universe_provider):
    paper = app_config.data_path / "paper.json"
    paper.parent.mkdir(parents=True)
    paper.write_text(json.dumps({
        "cash": 5_000, "total_equity": 35_000,
        "holdings": [{"ticker": "DNA", "shares": 100, "current_price": 300, "market_value": 30_000}],
    }))
    summary = make_pipeline(app_config, universe_provider).run_once(
        RunOptions(offline=True, portfolio_file=str(paper)))
    assert summary["orders"][0]["ticker"] == "DNA" and summary["orders"][0]["action"] == "SELL"


def test_live_requires_second_key(app_config, universe_provider):
    with pytest.raises(LiveTradingDisabled):
        make_pipeline(app_config, universe_provider).run_once(RunOptions(live=True))
    app_config.live_trading_enabled = True
    with pytest.raises(ValueError):
        make_pipeline(app_config, universe_provider).run_once(RunOptions(live=True, ignore_hours=True))


def test_single_active_trader(app_config, universe_provider):
    pipe = make_pipeline(app_config, universe_provider)
    lock = app_config.data_path / "trader.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("1")  # pid 1 is alive -> another trader holds the lock
    with pytest.raises(TraderLockError):
        pipe.run_once(RunOptions(offline=True))


def test_market_closed_rejects_orders_unless_ignored(app_config, universe_provider):
    from datetime import datetime

    from smg_trader.market_hours import ET

    night = datetime(2026, 9, 23, 22, 0, tzinfo=ET)
    pipe = Pipeline(app_config, provider=universe_provider, alerts=AlertManager([]), clock=lambda: night)
    summary = pipe.run_once(RunOptions(offline=True))
    assert summary["orders"] and all(o["status"] == "REJECTED" for o in summary["orders"])
    summary = pipe.run_once(RunOptions(offline=True, ignore_hours=True))
    assert all(o["status"] == "DRY_RUN" for o in summary["orders"])


def test_cli_parses_and_reports_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("data_dir: data\nsmg:\n  username: ''\n")
    monkeypatch.delenv("SMG_USERNAME", raising=False)
    monkeypatch.delenv("SMG_LIVE_TRADING", raising=False)
    # `trade --live` without the config key is refused before anything else happens.
    assert main(["-c", str(cfg), "trade", "KO", "buy", "10", "--price", "60", "--live"]) == 1
    with pytest.raises(SystemExit):
        main(["-c", str(cfg), "nope"])


def test_example_config_loads():
    from smg_trader.config import load_config

    from .conftest import ROOT

    cfg = load_config(ROOT / "config.example.yaml", use_env=False)
    assert cfg.rules.min_shares_per_trade == 10 and cfg.rules.min_share_price == 3.0
    assert set(cfg.strategy.weights) == {"momentum", "technical", "fundamentals"}
    with pytest.raises(ValueError, match="Unknown config key"):
        from smg_trader.config import AppConfig, _merge_into

        _merge_into(AppConfig(), {"rulez": {}})
