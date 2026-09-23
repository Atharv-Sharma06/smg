"""US equity market hours (9:30 AM - 4:00 PM ET, Monday-Friday, excluding NYSE holidays)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Full-day NYSE closures. Extend each year (https://www.nyse.com/markets/hours-calendars).
NYSE_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2025
        date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
        date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
        date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
        # 2026
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
        # 2027
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
    }
)


def now_et() -> datetime:
    return datetime.now(ET)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def is_market_open(at: datetime | None = None) -> bool:
    at = (at or now_et()).astimezone(ET)
    return is_trading_day(at.date()) and MARKET_OPEN <= at.time() < MARKET_CLOSE


def market_status(at: datetime | None = None) -> str:
    at = (at or now_et()).astimezone(ET)
    if at.weekday() >= 5:
        return f"closed (weekend, {at:%a %Y-%m-%d %H:%M} ET)"
    if at.date() in NYSE_HOLIDAYS:
        return f"closed (NYSE holiday {at:%Y-%m-%d})"
    if at.time() < MARKET_OPEN:
        return f"closed (pre-market, {at:%H:%M} ET; opens 09:30)"
    if at.time() >= MARKET_CLOSE:
        return f"closed (after hours, {at:%H:%M} ET; closed 16:00)"
    return f"open ({at:%H:%M} ET)"
