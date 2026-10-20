"""Configuration loading.

Settings come from three places, later ones overriding earlier ones:

1. Defaults defined in the dataclasses below.
2. A YAML file (``config.yaml`` by default, see ``config.example.yaml``).
3. Environment variables (and a ``.env`` file) for secrets:
   ``SMG_USERNAME``, ``SMG_PASSWORD``, ``SLACK_WEBHOOK_URL``, ``SMTP_*``,
   ``SMG_LIVE_TRADING``.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "V",
    "JNJ", "XOM", "WMT", "PG", "KO", "COST", "AMD", "NFLX",
]


@dataclass
class SMGConfig:
    """Connection and scraping settings for stockmarketgame.org."""

    username: str = ""
    password: str = ""
    base_url: str = "https://www.stockmarketgame.org"
    login_url: str = "https://www.stockmarketgame.org/login.html"
    portfolio_url: str = ""          # holdings page; blank = use nav link text
    account_summary_url: str = ""    # cash / equity page; blank = portfolio page
    trade_url: str = ""              # "Enter a Trade" page
    pending_orders_url: str = ""     # pending / open orders page
    rankings_url: str = ""           # rankings / leaderboard page
    team_name: str = ""              # our team as shown on the leaderboard
    region_filter: str = "North Carolina"
    headless: bool = True
    chrome_binary: str = ""
    chromedriver_path: str = ""
    page_load_timeout: int = 30
    element_timeout: int = 15
    min_seconds_between_requests: float = 2.0
    login_retries: int = 3
    debug_dir: str = "data/debug"
    # Per-site CSS/XPath overrides; merged over smg_trader.selectors.DEFAULT_SELECTORS.
    selectors: dict[str, Any] = field(default_factory=dict)


@dataclass
class RulesConfig:
    """SMG game rules plus the bot's own risk limits."""

    allowed_exchanges: list[str] = field(default_factory=lambda: ["NYSE", "NASDAQ"])
    min_share_price: float = 3.00
    min_shares_per_trade: int = 10
    commission_rate: float = 0.005          # SMG charges a broker fee per trade
    use_margin: bool = False
    margin_limit: float = 50_000.0
    margin_weekly_interest_rate: float = 0.07
    enforce_market_hours: bool = True
    allow_short_selling: bool = False
    min_short_price: float = 5.00           # check your SMG session's short-sale minimum
    short_margin_requirement: float = 0.50  # collateral held against shorts, fraction of short value
    max_position_pct: float = 0.20          # bot risk limit, not an SMG rule
    max_gross_exposure: float = 1.5         # (longs + shorts) / equity
    max_trades_per_run: int = 5
    min_seconds_between_trades: float = 5.0
    cash_reserve: float = 1_000.0           # never spend the last $X


@dataclass
class StrategyConfig:
    universe: list[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    # name -> weight; any strategy registered in smg_trader.strategies works here.
    weights: dict[str, float] = field(
        default_factory=lambda: {"momentum": 0.5, "technical": 0.3, "fundamentals": 0.2}
    )
    buy_threshold: float = 0.35     # ensemble score needed to BUY
    sell_threshold: float = 0.35    # -score needed to SELL
    min_confidence_to_trade: float = 0.4
    momentum_min_signals: int = 3   # N sources must align for a momentum BUY/SELL
    benchmark: str = "SPY"
    lookback_days: int = 400        # history fetched for indicators
    # Strategy-specific keyword args, e.g. {"technical": {"rsi_oversold": 25}}
    params: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class PortfolioConfig:
    """How signals become positions.

    ``method: signal`` sizes each signal on its own (buy = confidence x
    max_position_pct, sell = close). Any other method builds a target
    portfolio and rebalances to it, trimming positions that grew too big:

    * ``equal``         equal weights
    * ``confidence``    weights proportional to signal confidence
    * ``inverse_vol``   weights proportional to 1 / volatility
    * ``risk_parity``   equal risk contribution (uses the covariance matrix)
    * ``mean_variance`` max risk-adjusted return, expected return from signal score
    """

    method: str = "signal"
    max_positions: int = 10           # long book size
    max_short_positions: int = 5
    long_exposure: float = 0.90       # long book as a fraction of equity
    short_exposure: float = 0.0       # short book; > 0 needs rules.allow_short_selling
    rebalance_threshold: float = 0.02  # skip trades that move a weight by less than this
    min_trade_value: float = 500.0
    cov_lookback_days: int = 60       # trading days of returns for volatility / covariance
    cov_shrinkage: float = 0.3        # blend sample covariance toward its diagonal


@dataclass
class PairsConfig:
    """Statistical-arbitrage pairs: short the rich leg, buy the cheap leg."""

    enabled: bool = False
    pairs: list[list[str]] = field(default_factory=list)  # e.g. [[KO, PEP], [XOM, CVX]]
    capital_pct: float = 0.20     # gross capital (both legs, all pairs) as a fraction of equity
    lookback_days: int = 60       # trading days for hedge ratio and z-score
    entry_z: float = 2.0
    exit_z: float = 0.5
    stop_z: float = 4.0           # close if the spread keeps widening; never enter beyond this
    min_correlation: float = 0.7
    max_half_life: float = 30.0   # days; spread must mean-revert faster than this


@dataclass
class AlertConfig:
    slack_webhook_url: str = ""
    email_to: str = ""
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    high_confidence_threshold: float = 0.8
    drawdown_threshold: float = 0.10  # alert when equity falls 10% below its peak


@dataclass
class AppConfig:
    smg: SMGConfig = field(default_factory=SMGConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    pairs: PairsConfig = field(default_factory=PairsConfig)
    data_dir: str = "data"
    starting_equity: float = 100_000.0
    live_trading_enabled: bool = False   # second key for live mode, besides --live
    schedule_time: str = "10:00"         # America/New_York
    leaderboard_day: str = "fri"         # weekly leaderboard check
    leaderboard_time: str = "16:30"
    market_data: str = "yfinance"        # "yfinance" or "csv"
    market_data_dir: str = "data/prices"  # used when market_data == "csv"
    # Save fundamentals/news each live run to data/pit/ so they can be backtested later.
    record_point_in_time: bool = True

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)


def _merge_into(obj: Any, values: dict[str, Any]) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"Unknown config key: {type(obj).__name__}.{key}")
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge_into(current, value)
        else:
            setattr(obj, key, copy.deepcopy(value))


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def apply_env(cfg: AppConfig) -> AppConfig:
    env = os.environ
    cfg.smg.username = env.get("SMG_USERNAME", cfg.smg.username)
    cfg.smg.password = env.get("SMG_PASSWORD", cfg.smg.password)
    cfg.smg.chrome_binary = env.get("CHROME_BINARY", cfg.smg.chrome_binary)
    cfg.smg.chromedriver_path = env.get("CHROMEDRIVER_PATH", cfg.smg.chromedriver_path)
    cfg.alerts.slack_webhook_url = env.get("SLACK_WEBHOOK_URL", cfg.alerts.slack_webhook_url)
    cfg.alerts.smtp_host = env.get("SMTP_HOST", cfg.alerts.smtp_host)
    if env.get("SMTP_PORT"):
        cfg.alerts.smtp_port = int(env["SMTP_PORT"])
    cfg.alerts.smtp_username = env.get("SMTP_USERNAME", cfg.alerts.smtp_username)
    cfg.alerts.smtp_password = env.get("SMTP_PASSWORD", cfg.alerts.smtp_password)
    cfg.alerts.email_to = env.get("ALERT_EMAIL_TO", cfg.alerts.email_to)
    cfg.alerts.email_from = env.get("ALERT_EMAIL_FROM", cfg.alerts.email_from)
    live = _env_bool("SMG_LIVE_TRADING")
    if live is not None:
        cfg.live_trading_enabled = live
    return cfg


def load_config(path: str | os.PathLike[str] | None = None, *, use_env: bool = True) -> AppConfig:
    """Load config from YAML (if present) and the environment."""
    if use_env:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:  # pragma: no cover - optional dependency
            pass

    cfg = AppConfig()
    candidate = Path(path) if path else Path(os.environ.get("SMG_CONFIG", "config.yaml"))
    if candidate.exists():
        with candidate.open() as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{candidate} must contain a YAML mapping")
        _merge_into(cfg, raw)
    elif path:
        raise FileNotFoundError(candidate)

    return apply_env(cfg) if use_env else cfg
