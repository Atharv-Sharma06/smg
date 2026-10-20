from datetime import date
from unittest.mock import patch

from smg_trader.alerts import AlertManager, SlackChannel, build_alert_manager
from smg_trader.config import AlertConfig
from smg_trader.models import Action, LeaderboardEntry, Portfolio, Signal
from smg_trader.monitor import LeaderboardMonitor, PerformanceTracker, alert_high_confidence


def test_performance_tracking_and_drawdown_alert_once(tmp_path):
    alerts = AlertManager([])
    tracker = PerformanceTracker(tmp_path / "hist.csv", 100_000, alerts, drawdown_threshold=0.10)
    s1 = tracker.record(Portfolio(cash=0, total_equity=110_000), date(2026, 9, 21))
    assert s1.daily_pnl == 10_000 and s1.total_return == 0.1 and s1.drawdown == 0
    s2 = tracker.record(Portfolio(cash=0, total_equity=97_000), date(2026, 9, 22))
    assert s2.peak_equity == 110_000 and round(s2.drawdown, 4) == round(1 - 97 / 110, 4)
    assert [a.kind for a in alerts.sent] == ["drawdown"]
    tracker.record(Portfolio(cash=0, total_equity=96_000), date(2026, 9, 23))
    assert len(alerts.sent) == 1  # still below threshold: no repeat alert
    # Re-recording the same day replaces the row.
    tracker.record(Portfolio(cash=0, total_equity=96_500), date(2026, 9, 23))
    assert len(tracker.history()) == 3


def board(*teams):
    return [LeaderboardEntry(i, t, "School") for i, t in enumerate(teams, 1)]


def test_leaderboard_rank_change_alerts(tmp_path):
    alerts = AlertManager([])
    mon = LeaderboardMonitor(tmp_path / "lb.json", "Tar Heel Traders", alerts)
    first = mon.update(board("A", "Tar Heel Traders", "C"))
    assert first["our_rank"] == 2 and alerts.sent == []  # first snapshot: nothing to compare
    mon.update(board("A", "Tar Heel Traders", "C"))
    assert alerts.sent == []
    mon.update(board("Tar Heel Traders", "A", "C"))
    assert alerts.sent[-1].kind == "rank_change" and "#2 -> #1" in alerts.sent[-1].title
    assert "up" in alerts.sent[-1].title
    mon.update(board("A", "B", "C"))
    assert "out of the top list" in alerts.sent[-1].title


def test_high_confidence_alerts():
    alerts = AlertManager([])
    sigs = [Signal("A", Action.BUY, 0.85, 0.85, "r", "s", 10.0), Signal("B", Action.BUY, 0.5, 0.5, "r", "s"),
            Signal("C", Action.HOLD, 0.9, 0.9, "r", "s")]
    strong = alert_high_confidence(sigs, alerts, 0.8)
    assert [s.ticker for s in strong] == ["A"]
    assert alerts.sent[0].kind == "high_confidence"


def test_alert_channels(tmp_path):
    mgr = build_alert_manager(AlertConfig(slack_webhook_url="https://hooks.slack.test/x"))
    assert [c.name for c in mgr.channels] == ["log", "slack"]
    with patch("smg_trader.alerts.requests.post") as post:
        mgr.send("trade", "BUY 10 KO", "details")
        payload = post.call_args.kwargs["json"]
        assert "BUY 10 KO" in payload["text"]
    # A failing channel must not raise.
    with patch("smg_trader.alerts.requests.post", side_effect=OSError("down")):
        AlertManager([SlackChannel("https://x")]).send("trade", "t")
