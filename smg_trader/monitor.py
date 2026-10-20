"""Portfolio performance tracking and leaderboard monitoring (with alerts)."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .alerts import AlertManager
from .models import LeaderboardEntry, Portfolio, Signal

log = logging.getLogger(__name__)


@dataclass
class PerformanceSnapshot:
    date: str
    total_equity: float
    cash: float
    holdings: int
    daily_pnl: float
    daily_return: float
    total_return: float
    peak_equity: float
    drawdown: float


class PerformanceTracker:
    """Keeps one row per day in ``portfolio_history.csv`` and alerts on drawdown."""

    FIELDS = list(PerformanceSnapshot.__dataclass_fields__)

    def __init__(self, path: str | Path, starting_equity: float, alerts: AlertManager,
                 drawdown_threshold: float):
        self.path = Path(path)
        self.starting_equity = starting_equity
        self.alerts = alerts
        self.drawdown_threshold = drawdown_threshold

    def history(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(newline="") as fh:
            return list(csv.DictReader(fh))

    def record(self, portfolio: Portfolio, today: date | None = None) -> PerformanceSnapshot:
        today = today or portfolio.timestamp.date()
        rows = [r for r in self.history() if r["date"] != today.isoformat()]
        prev = rows[-1] if rows else None
        prev_equity = float(prev["total_equity"]) if prev else self.starting_equity
        prev_peak = float(prev["peak_equity"]) if prev else self.starting_equity
        prev_drawdown = float(prev["drawdown"]) if prev else 0.0

        equity = portfolio.total_equity
        peak = max(prev_peak, equity)
        snap = PerformanceSnapshot(
            date=today.isoformat(),
            total_equity=round(equity, 2),
            cash=round(portfolio.cash, 2),
            holdings=len(portfolio.holdings),
            daily_pnl=round(equity - prev_equity, 2),
            daily_return=round(equity / prev_equity - 1, 6) if prev_equity else 0.0,
            total_return=round(equity / self.starting_equity - 1, 6),
            peak_equity=round(peak, 2),
            drawdown=round(1 - equity / peak, 6) if peak else 0.0,
        )
        rows.append({k: str(v) for k, v in snap.__dict__.items()})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        log.info("Performance: equity $%s, day %+.2f (%+.2f%%), total %+.2f%%, drawdown %.2f%%",
                 f"{equity:,.2f}", snap.daily_pnl, snap.daily_return * 100, snap.total_return * 100,
                 snap.drawdown * 100)
        # Alert once when crossing the threshold, not every day we stay below it.
        if snap.drawdown >= self.drawdown_threshold > prev_drawdown:
            self.alerts.send(
                "drawdown",
                f"Portfolio drawdown {snap.drawdown:.1%} (threshold {self.drawdown_threshold:.0%})",
                f"Equity ${equity:,.2f} vs peak ${peak:,.2f}; total return {snap.total_return:+.2%}",
            )
        return snap


class LeaderboardMonitor:
    """Stores the last leaderboard snapshot and alerts when our rank changes."""

    def __init__(self, path: str | Path, team_name: str, alerts: AlertManager):
        self.path = Path(path)
        self.team_name = team_name
        self.alerts = alerts

    def load(self) -> list[LeaderboardEntry]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text())
        return [LeaderboardEntry(**e) for e in data.get("entries", [])]

    def find_team(self, entries: list[LeaderboardEntry]) -> LeaderboardEntry | None:
        if not self.team_name:
            return None
        name = self.team_name.strip().lower()
        return next((e for e in entries if e.team.strip().lower() == name), None) or next(
            (e for e in entries if name in e.team.lower()), None
        )

    def update(self, entries: list[LeaderboardEntry], today: date | None = None) -> dict:
        previous = self.load()
        old, new = self.find_team(previous), self.find_team(entries)
        prev_teams = [e.team for e in previous]
        new_in_top = [e.team for e in entries if prev_teams and e.team not in prev_teams]

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "date": (today or date.today()).isoformat(),
            "entries": [e.to_dict() for e in entries],
        }, indent=2))

        summary = {
            "our_rank": new.rank if new else None,
            "previous_rank": old.rank if old else None,
            "new_in_top": new_in_top,
        }
        board = "\n".join(
            f"{e.rank:>3}. {e.team} ({e.school})"
            + (f" ${e.total_equity:,.0f}" if e.total_equity is not None else "")
            + (f" {e.pct_return:+.2f}%" if e.pct_return is not None else "")
            for e in entries
        )
        if previous and (old.rank if old else None) != (new.rank if new else None):
            before = f"#{old.rank}" if old else "unranked"
            after = f"#{new.rank}" if new else "out of the top list"
            arrow = "up" if old and new and new.rank < old.rank else "down" if old else "up"
            self.alerts.send("rank_change", f"NC ranking {arrow}: {before} -> {after}", board)
        elif new_in_top:
            self.alerts.send("rank_change", f"New teams in NC top list: {', '.join(new_in_top)}", board)
        log.info("Leaderboard (%d entries); our rank: %s\n%s", len(entries), summary["our_rank"], board)
        return summary


def alert_high_confidence(signals: list[Signal], alerts: AlertManager, threshold: float) -> list[Signal]:
    strong = [s for s in signals if s.confidence > threshold and s.action.value != "HOLD"]
    if strong:
        body = "\n".join(
            f"{s.action.value} {s.ticker} conf {s.confidence:.2f} @ ${s.price or 0:.2f}: {s.reason[:200]}"
            for s in sorted(strong, key=lambda s: -s.confidence)
        )
        alerts.send("high_confidence", f"{len(strong)} signal(s) above {threshold:.2f} confidence", body)
    return strong
