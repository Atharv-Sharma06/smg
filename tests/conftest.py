from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smg_trader.config import AppConfig, RulesConfig  # noqa: E402
from smg_trader.market_data import Fundamentals, InMemoryProvider  # noqa: E402
from smg_trader.market_hours import ET  # noqa: E402

# Wednesday 2026-09-23 11:00 ET: market open.
MARKET_OPEN_TIME = datetime(2026, 9, 23, 11, 0, tzinfo=ET)
END_DATE = pd.Timestamp("2026-09-22")


def make_prices(drift: float, *, n: int = 320, start_price: float = 100.0, seed: int = 0,
                noise: float = 0.01, volume_surge: bool = False, end: pd.Timestamp = END_DATE) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=end, periods=n)
    rets = drift + noise * rng.standard_normal(n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = close * (1 - 0.002 * rng.standard_normal(n))
    vol = np.full(n, 1_000_000.0) * (1 + 0.1 * rng.random(n))
    if volume_surge:
        vol[-10:] *= 1.8
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.005,
                         "Low": np.minimum(open_, close) * 0.995, "Close": close, "Volume": vol}, index=idx)


@pytest.fixture
def universe_provider() -> InMemoryProvider:
    """12 stocks: 6 strong uptrends, 4 downtrends, 2 flat; plus SPY and sector ETFs."""
    prices, fundamentals = {}, {}
    for i, t in enumerate(["UPA", "UPB", "UPC", "UPD", "UPE", "UPF"]):
        prices[t] = make_prices(0.004, seed=i, volume_surge=True, start_price=50 + i)
        fundamentals[t] = Fundamentals(pe_ratio=18, eps_growth=0.25, dividend_yield=0.01,
                                       sector="Technology", exchange="NASDAQ")
    for i, t in enumerate(["DNA", "DNB", "DNC", "DND"]):
        prices[t] = make_prices(-0.004, seed=10 + i, volume_surge=True, start_price=300)
        fundamentals[t] = Fundamentals(pe_ratio=-5, eps_growth=-0.3, dividend_yield=0.0,
                                       sector="Energy", exchange="NYSE")
    for i, t in enumerate(["FLA", "FLB"]):
        prices[t] = make_prices(0.0, seed=20 + i, noise=0.002, start_price=40)
        fundamentals[t] = Fundamentals(pe_ratio=30, eps_growth=0.0, dividend_yield=0.02,
                                       sector="Utilities", exchange="NYSE")
    prices["SPY"] = make_prices(0.0005, seed=99, start_price=500)
    prices["XLK"] = make_prices(0.002, seed=98)
    prices["XLE"] = make_prices(-0.002, seed=97)
    prices["XLU"] = make_prices(0.0, seed=96, noise=0.002)
    return InMemoryProvider(prices, fundamentals)


@pytest.fixture
def rules() -> RulesConfig:
    return RulesConfig(max_trades_per_run=10, min_seconds_between_trades=0)


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig(data_dir=str(tmp_path / "data"))
    cfg.rules.min_seconds_between_trades = 0
    cfg.strategy.universe = ["UPA", "UPB", "UPC", "UPD", "UPE", "UPF", "DNA", "DNB", "DNC", "DND", "FLA", "FLB"]
    return cfg


def chrome_paths() -> tuple[str, str]:
    return os.environ.get("CHROME_BINARY", ""), os.environ.get("CHROMEDRIVER_PATH", "")
