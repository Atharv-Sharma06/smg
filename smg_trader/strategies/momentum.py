"""Strategy 1: momentum by vote counting.

Each *source* looks at one kind of evidence and votes +1 (bullish), -1
(bearish), 0 (neutral) or abstains (``None``, no data). The strategy buys when
at least ``min_signals`` sources vote bullish and bulls outnumber bears, and
sells on the mirror condition.

Sources that need current-only data (analyst actions, news) abstain in
backtests so the replay has no look-ahead.

Add a source by writing a function ``(ticker, ctx) -> Vote | None`` and
passing it in ``sources=`` (or appending to ``DEFAULT_SOURCES``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from ..market_data import SECTOR_ETFS
from ..models import Action
from .base import ScoreResult, Strategy, StrategyContext, register_strategy
from .indicators import pct_return


@dataclass
class Vote:
    source: str
    vote: int  # +1, 0, -1
    note: str


Source = Callable[[str, StrategyContext], "Vote | None"]


def price_trend(ticker: str, ctx: StrategyContext) -> Vote | None:
    closes = ctx.closes(ticker)
    r20, r60 = pct_return(closes, 20), pct_return(closes, 60)
    if r20 is None or r60 is None:
        return None
    note = f"20d {r20:+.1%}, 60d {r60:+.1%}"
    if r20 > 0 and r60 > 0:
        return Vote("price_trend", 1, note)
    if r20 < 0 and r60 < 0:
        return Vote("price_trend", -1, note)
    return Vote("price_trend", 0, note)


def relative_strength(ticker: str, ctx: StrategyContext, margin: float = 0.02) -> Vote | None:
    r = pct_return(ctx.closes(ticker), 60)
    b = pct_return(ctx.closes(ctx.benchmark), 60)
    if r is None or b is None:
        return None
    edge = r - b
    note = f"60d vs {ctx.benchmark} {edge:+.1%}"
    return Vote("relative_strength", 1 if edge > margin else -1 if edge < -margin else 0, note)


def sector_trend(ticker: str, ctx: StrategyContext) -> Vote | None:
    f = ctx.provider.fundamentals(ticker)
    etf = SECTOR_ETFS.get(f.sector or "") if f else None
    if not etf:
        return None
    r = pct_return(ctx.closes(etf), 20)
    if r is None:
        return None
    note = f"sector {etf} 20d {r:+.1%}"
    return Vote("sector_trend", 1 if r > 0.01 else -1 if r < -0.01 else 0, note)


def volume_confirmation(ticker: str, ctx: StrategyContext) -> Vote | None:
    df = ctx.history(ticker)
    if len(df) < 51 or "Volume" not in df:
        return None
    vol10 = df["Volume"].iloc[-10:].mean()
    vol50 = df["Volume"].iloc[-50:].mean()
    r10 = pct_return(df["Close"], 10)
    if not vol50 or r10 is None:
        return None
    ratio = vol10 / vol50
    note = f"volume {ratio:.2f}x avg, 10d {r10:+.1%}"
    if ratio >= 1.2:
        return Vote("volume", 1 if r10 > 0 else -1, note)
    return Vote("volume", 0, note)


def near_52w_high(ticker: str, ctx: StrategyContext) -> Vote | None:
    closes = ctx.closes(ticker)
    if len(closes) < 120:
        return None
    window = closes.iloc[-252:]
    ratio = float(closes.iloc[-1] / window.max())
    note = f"{ratio:.0%} of 52w high"
    return Vote("52w_high", 1 if ratio >= 0.95 else -1 if ratio <= 0.80 else 0, note)


def analyst_actions(ticker: str, ctx: StrategyContext) -> Vote | None:
    if not ctx.live:
        return None
    acts = ctx.provider.analyst_actions(ticker, ctx.as_of - timedelta(days=30))
    if acts is None:
        return None
    net = acts.upgrades - acts.downgrades
    note = f"analysts 30d: {acts.upgrades} up / {acts.downgrades} down"
    return Vote("analysts", 1 if net > 0 else -1 if net < 0 else 0, note)


_POSITIVE = re.compile(
    r"\b(beat|beats|surge|surges|soar|soars|record|upgrade|upgraded|raises|raised|rally|"
    r"jumps?|gains?|strong|outperform|bullish|growth|buyback|approval|wins?)\b",
    re.I,
)
_NEGATIVE = re.compile(
    r"\b(miss|misses|plunge|plunges|falls?|drops?|downgrade|downgraded|cuts?|lawsuit|probe|"
    r"recall|weak|bearish|slump|slumps|layoffs?|warns?|fraud|decline|declines|loss)\b",
    re.I,
)


def headline_sentiment(headlines: list[str]) -> int:
    return sum(len(_POSITIVE.findall(h)) - len(_NEGATIVE.findall(h)) for h in headlines)


def news_sentiment(ticker: str, ctx: StrategyContext) -> Vote | None:
    if not ctx.live:
        return None
    headlines = ctx.provider.news_headlines(ticker)
    if not headlines:
        return None
    net = headline_sentiment(headlines)
    note = f"news sentiment {net:+d} over {len(headlines)} headlines"
    return Vote("news", 1 if net >= 2 else -1 if net <= -2 else 0, note)


DEFAULT_SOURCES: list[Source] = [
    price_trend,
    relative_strength,
    sector_trend,
    volume_confirmation,
    near_52w_high,
    analyst_actions,
    news_sentiment,
]


@register_strategy("momentum")
class MomentumStrategy(Strategy):
    def __init__(
        self,
        min_signals: int = 3,
        sources: list[Source] | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.min_signals = min_signals
        self.sources = list(sources or DEFAULT_SOURCES)

    def score(self, ticker: str, ctx: StrategyContext) -> ScoreResult:
        votes = [v for src in self.sources if (v := src(ticker, ctx)) is not None]
        if not votes:
            return None
        bulls = [v for v in votes if v.vote > 0]
        bears = [v for v in votes if v.vote < 0]
        score = (len(bulls) - len(bears)) / len(votes)
        reason = (
            f"{len(bulls)} bullish / {len(bears)} bearish of {len(votes)} sources "
            f"(need {self.min_signals}): " + "; ".join(v.note for v in votes)
        )
        details = {
            "bullish": len(bulls),
            "bearish": len(bears),
            "sources": {v.source: v.vote for v in votes},
        }
        return score, reason, details

    def to_action(self, score: float, details: dict[str, Any]) -> Action:
        bulls, bears = details["bullish"], details["bearish"]
        if bulls >= self.min_signals and bulls > bears:
            return Action.BUY
        if bears >= self.min_signals and bears > bulls:
            return Action.SELL
        return Action.HOLD
