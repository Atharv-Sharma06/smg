"""Small, dependency-free technical indicators on pandas Series."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    # No losses in the window -> RSI 100; flat -> 50.
    out = out.where(avg_loss != 0, np.where(avg_gain > 0, 100.0, 50.0))
    return out


def pct_return(series: pd.Series, days: int) -> float | None:
    if len(series) <= days:
        return None
    start = float(series.iloc[-days - 1])
    return float(series.iloc[-1]) / start - 1 if start else None


def crossed_above(fast: pd.Series, slow: pd.Series, within: int) -> bool:
    """True if ``fast`` moved from <= ``slow`` to > ``slow`` in the last ``within`` bars."""
    diff = (fast - slow).dropna()
    if len(diff) < 2:
        return False
    recent = diff.iloc[-(within + 1):]
    above = recent > 0
    return bool(above.iloc[-1] and (~above.iloc[:-1]).any())


def crossed_below(fast: pd.Series, slow: pd.Series, within: int) -> bool:
    return crossed_above(-fast, -slow, within)
