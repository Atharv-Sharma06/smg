"""Strategy 2: fundamentals (P/E, EPS growth, dividend yield).

In backtests the numbers come from the provider's point-in-time data (e.g.
P/E rebuilt from reported EPS, or snapshots the bot recorded on earlier live
runs). When nothing is known for a date the strategy abstains rather than use
today's numbers in the past.
"""

from __future__ import annotations

from typing import Any

from .base import ScoreResult, Strategy, StrategyContext, register_strategy


def pe_score(pe: float | None, fair_pe: float, expensive_pe: float) -> float | None:
    if pe is None:
        return None
    if pe <= 0:
        return -1.0  # losing money
    if pe <= fair_pe * 0.6:
        return 1.0
    if pe <= fair_pe:
        return 0.5
    if pe <= expensive_pe:
        return 0.0
    return -0.5 if pe <= expensive_pe * 1.5 else -1.0


def growth_score(growth: float | None) -> float | None:
    if growth is None:
        return None
    if growth >= 0.20:
        return 1.0
    if growth >= 0.05:
        return 0.5
    if growth >= -0.05:
        return 0.0
    return -1.0


def dividend_score(yield_: float | None) -> float | None:
    if yield_ is None:
        return None
    if yield_ >= 0.03:
        return 0.5
    if yield_ >= 0.01:
        return 0.25
    return 0.0


@register_strategy("fundamentals")
class FundamentalsStrategy(Strategy):
    def __init__(self, fair_pe: float = 25.0, expensive_pe: float = 40.0, **kwargs: Any):
        super().__init__(**kwargs)
        self.fair_pe = fair_pe
        self.expensive_pe = expensive_pe

    def score(self, ticker: str, ctx: StrategyContext) -> ScoreResult:
        f = ctx.fundamentals(ticker)
        if f is None:
            return None
        parts = {
            "pe": pe_score(f.pe_ratio, self.fair_pe, self.expensive_pe),
            "eps_growth": growth_score(f.eps_growth),
            "dividend": dividend_score(f.dividend_yield),
        }
        present = {k: v for k, v in parts.items() if v is not None}
        if not present:
            return None
        score = sum(present.values()) / len(present)
        pe = f"{f.pe_ratio:.1f}" if f.pe_ratio is not None else "n/a"
        growth = f"{f.eps_growth:+.0%}" if f.eps_growth is not None else "n/a"
        div = f"{f.dividend_yield:.1%}" if f.dividend_yield is not None else "n/a"
        reason = f"P/E {pe}, EPS growth {growth}, dividend {div}"
        return score, reason, {"components": present}
