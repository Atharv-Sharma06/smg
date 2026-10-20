"""Ensemble: weighted average of member strategy scores.

Members that abstain for a ticker (no data) are dropped and the remaining
weights renormalised, so e.g. a backtest without fundamentals still works.
"""

from __future__ import annotations

from typing import Any

from ..config import StrategyConfig
from .base import ScoreResult, Strategy, StrategyContext, create_strategy, register_strategy


@register_strategy("ensemble")
class EnsembleStrategy(Strategy):
    def __init__(self, members: list[tuple[Strategy, float]], **kwargs: Any):
        super().__init__(**kwargs)
        if not members:
            raise ValueError("Ensemble needs at least one member strategy")
        self.members = [(s, float(w)) for s, w in members if w > 0]

    def score(self, ticker: str, ctx: StrategyContext) -> ScoreResult:
        parts = []
        for strategy, weight in self.members:
            sig = strategy.generate(ticker, ctx)
            if sig is not None:
                parts.append((sig, weight))
        if not parts:
            return None
        total_w = sum(w for _, w in parts)
        score = sum(sig.score * w for sig, w in parts) / total_w
        reason = " | ".join(
            f"{sig.strategy} {sig.score:+.2f} x{w / total_w:.2f} [{sig.action.value}]: {sig.reason}"
            for sig, w in parts
        )
        details = {
            "members": {
                sig.strategy: {"score": sig.score, "action": sig.action.value, "weight": round(w / total_w, 4)}
                for sig, w in parts
            }
        }
        return score, reason, details


def build_strategy(cfg: StrategyConfig, only: str | None = None) -> Strategy:
    """Build the configured ensemble, or a single named strategy if ``only`` is set."""

    def make(name: str) -> Strategy:
        params: dict[str, Any] = {
            "buy_threshold": cfg.buy_threshold,
            "sell_threshold": cfg.sell_threshold,
        }
        if name == "momentum":
            params["min_signals"] = cfg.momentum_min_signals
        params.update(cfg.params.get(name, {}))
        return create_strategy(name, **params)

    if only and only != "ensemble":
        return make(only)
    members = [(make(name), weight) for name, weight in cfg.weights.items() if weight > 0]
    return EnsembleStrategy(
        members, buy_threshold=cfg.buy_threshold, sell_threshold=cfg.sell_threshold
    )
