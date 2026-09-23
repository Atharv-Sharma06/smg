"""Strategy 3: technical (50-day moving average crossover + RSI oversold/overbought)."""

from __future__ import annotations

from typing import Any

from .base import ScoreResult, Strategy, StrategyContext, register_strategy
from .indicators import crossed_above, crossed_below, rsi, sma


@register_strategy("technical")
class TechnicalStrategy(Strategy):
    def __init__(
        self,
        ma_window: int = 50,
        long_ma_window: int = 200,
        cross_within: int = 5,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.ma_window = ma_window
        self.long_ma_window = long_ma_window
        self.cross_within = cross_within
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought

    def score(self, ticker: str, ctx: StrategyContext) -> ScoreResult:
        closes = ctx.closes(ticker)
        if len(closes) < self.ma_window + 2:
            return None

        parts: dict[str, tuple[float, float]] = {}  # name -> (score, weight)
        notes = []

        ma = sma(closes, self.ma_window)
        price, ma_now = float(closes.iloc[-1]), float(ma.iloc[-1])
        if crossed_above(closes, ma, self.cross_within):
            parts["ma_cross"] = (1.0, 0.5)
            notes.append(f"price crossed above {self.ma_window}d MA")
        elif crossed_below(closes, ma, self.cross_within):
            parts["ma_cross"] = (-1.0, 0.5)
            notes.append(f"price crossed below {self.ma_window}d MA")
        else:
            side = 0.3 if price > ma_now else -0.3
            parts["ma_cross"] = (side, 0.5)
            notes.append(f"price {'above' if side > 0 else 'below'} {self.ma_window}d MA ({price / ma_now - 1:+.1%})")

        if len(closes) >= self.long_ma_window + 2:
            long_ma = sma(closes, self.long_ma_window)
            if crossed_above(ma, long_ma, self.cross_within * 2):
                parts["golden_cross"] = (1.0, 0.15)
                notes.append("golden cross")
            elif crossed_below(ma, long_ma, self.cross_within * 2):
                parts["golden_cross"] = (-1.0, 0.15)
                notes.append("death cross")

        r = float(rsi(closes, self.rsi_period).iloc[-1])
        if r < self.rsi_oversold:
            parts["rsi"] = (1.0, 0.35)
            notes.append(f"RSI {r:.0f} oversold")
        elif r > self.rsi_overbought:
            parts["rsi"] = (-1.0, 0.35)
            notes.append(f"RSI {r:.0f} overbought")
        else:
            parts["rsi"] = (0.0, 0.35)
            notes.append(f"RSI {r:.0f}")

        total_w = sum(w for _, w in parts.values())
        score = sum(s * w for s, w in parts.values()) / total_w
        details = {"rsi": round(r, 2), "sma": round(ma_now, 4), "components": {k: s for k, (s, _) in parts.items()}}
        return score, "; ".join(notes), details
