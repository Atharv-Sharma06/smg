"""Text -> value helpers for scraped tables (pure functions, unit tested)."""

from __future__ import annotations

import re

_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*|[-+]?\.\d+")
_EMPTY = {"", "-", "--", "—", "–", "n/a", "na", "none", "null"}


def parse_number(text: str | None) -> float | None:
    """'$1,234.56' -> 1234.56, '(12.30)' -> -12.3, '-5.2%' -> -5.2, '--' -> None."""
    if text is None:
        return None
    t = str(text).strip().replace("−", "-")  # unicode minus
    if t.lower() in _EMPTY:
        return None
    negative = t.startswith("(") and t.endswith(")")
    m = _NUM_RE.search(t.replace(" ", ""))
    if not m:
        return None
    value = float(m.group().replace(",", ""))
    if negative and value > 0:
        value = -value
    # "$-12.30" or "- $12.30"
    if value > 0 and re.match(r"^[^\d]*-", t):
        value = -value
    return value


def parse_int(text: str | None) -> int | None:
    value = parse_number(text)
    return int(round(value)) if value is not None else None


def parse_ticker(text: str | None) -> str | None:
    """First symbol-looking token: 'AAPL - Apple Inc.' -> 'AAPL', 'BRK.B' kept."""
    if not text:
        return None
    m = re.search(r"\b[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?\b", text.strip())
    return m.group() if m else None


def map_columns(headers: list[str], spec: dict[str, list[str]]) -> dict[str, int]:
    """Map field names to column indexes by header keywords.

    Fields are resolved in ``spec`` order and a column is used at most once, so
    put specific fields first (``purchase_price`` before ``price``).
    Exact header matches beat substring matches.
    """
    norm = [re.sub(r"\s+", " ", h).strip().lower() for h in headers]
    used: set[int] = set()
    out: dict[str, int] = {}
    for field_name, keywords in spec.items():
        found = None
        for kw in keywords:
            kw = kw.lower()
            exact = [i for i, h in enumerate(norm) if i not in used and h == kw]
            partial = [i for i, h in enumerate(norm) if i not in used and kw in h]
            if exact or partial:
                found = (exact or partial)[0]
                break
        if found is not None:
            out[field_name] = found
            used.add(found)
    return out
