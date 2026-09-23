"""Strategy interface and registry.

Adding a strategy takes one class and no changes to core code:

    from smg_trader.strategies import Strategy, register_strategy

    @register_strategy("my_idea")
    class MyIdea(Strategy):
        def score(self, ticker, ctx):
            ...
            return 0.6, "why", {"extra": "details"}

Then give it a weight in ``strategy.weights`` in config.yaml.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd

from ..market_data import MarketDataProvider
from ..models import Action, Signal

log = logging.getLogger(__name__)

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {}

# (score in [-1, 1], reason, details) or None when the strategy has no opinion
# (for example no fundamentals are available during a backtest).
ScoreResult = tuple[float, str, dict[str, Any]] | None


def register_strategy(name: str) -> Callable[[type[Strategy]], type[Strategy]]:
    def deco(cls: type[Strategy]) -> type[Strategy]:
        cls.name = name
        STRATEGY_REGISTRY[name] = cls
        return cls

    return deco


def create_strategy(name: str, **params: Any) -> Strategy:
    try:
        cls = STRATEGY_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown strategy {name!r}. Registered: {', '.join(sorted(STRATEGY_REGISTRY))}"
        ) from None
    return cls(**params)


@dataclass
class StrategyContext:
    """What a strategy is allowed to see.

    ``as_of`` caps every price lookup, so the backtester can replay history
    without look-ahead. ``live`` is False in backtests: sources that only have
    current data (news, analyst ratings, fundamentals) must then abstain.
    """

    provider: MarketDataProvider
    as_of: date = field(default_factory=date.today)
    lookback_days: int = 400
    benchmark: str = "SPY"
    live: bool = True
    _cache: dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)

    def history(self, ticker: str) -> pd.DataFrame:
        key = ticker.upper()
        if key not in self._cache:
            start = self.as_of - timedelta(days=self.lookback_days)
            self._cache[key] = self.provider.history(key, start, self.as_of)
        return self._cache[key]

    def closes(self, ticker: str) -> pd.Series:
        df = self.history(ticker)
        return df["Close"] if not df.empty else pd.Series(dtype=float)

    def last_price(self, ticker: str) -> float | None:
        closes = self.closes(ticker)
        return float(closes.iloc[-1]) if not closes.empty else None


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class Strategy(ABC):
    name: str = "base"

    def __init__(self, buy_threshold: float = 0.3, sell_threshold: float = 0.3, **_: Any):
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold

    @abstractmethod
    def score(self, ticker: str, ctx: StrategyContext) -> ScoreResult:
        """Return (score, reason, details) or None when there is not enough data."""

    def to_action(self, score: float, details: dict[str, Any]) -> Action:
        """Map a score to an action. Override for rule-based decisions."""
        if score >= self.buy_threshold:
            return Action.BUY
        if score <= -self.sell_threshold:
            return Action.SELL
        return Action.HOLD

    def generate(self, ticker: str, ctx: StrategyContext) -> Signal | None:
        try:
            result = self.score(ticker, ctx)
        except Exception as exc:  # one bad ticker must not kill the run
            log.warning("%s failed on %s: %s", self.name, ticker, exc)
            return None
        if result is None:
            return None
        score, reason, details = result
        score = clamp(float(score))
        return Signal(
            ticker=ticker.upper(),
            action=self.to_action(score, details),
            confidence=round(abs(score), 4),
            score=round(score, 4),
            reason=reason,
            strategy=self.name,
            price=ctx.last_price(ticker),
            details=details,
        )

    def generate_all(self, tickers: list[str], ctx: StrategyContext) -> list[Signal]:
        return [s for t in tickers if (s := self.generate(t, ctx)) is not None]
