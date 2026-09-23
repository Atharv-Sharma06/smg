"""Daily trading run + weekly leaderboard check with APScheduler."""

from __future__ import annotations

import logging

from .config import AppConfig
from .market_hours import ET, is_trading_day, now_et
from .pipeline import Pipeline, RunOptions

log = logging.getLogger(__name__)


def _hm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def run_scheduler(cfg: AppConfig, opts: RunOptions) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    pipeline = Pipeline(cfg)
    sched = BlockingScheduler(timezone=ET)

    def trading_job() -> None:
        if not is_trading_day(now_et().date()):
            log.info("market holiday; skipping run")
            return
        try:
            pipeline.run_once(RunOptions(live=opts.live, strategy=opts.strategy, check_leaderboard=False))
        except Exception:
            log.exception("scheduled run failed")

    def leaderboard_job() -> None:
        try:
            pipeline.check_leaderboard()
        except Exception as exc:
            log.exception("leaderboard check failed")
            pipeline.alerts.send("error", "Leaderboard check failed", str(exc))

    hour, minute = _hm(cfg.schedule_time)
    sched.add_job(trading_job, CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=ET),
                  id="daily_run", max_instances=1, coalesce=True, misfire_grace_time=900)
    lb_hour, lb_minute = _hm(cfg.leaderboard_time)
    sched.add_job(leaderboard_job, CronTrigger(day_of_week=cfg.leaderboard_day, hour=lb_hour,
                                               minute=lb_minute, timezone=ET),
                  id="weekly_leaderboard", max_instances=1, coalesce=True, misfire_grace_time=3600)
    for job in sched.get_jobs():
        log.info("scheduled %s: %s", job.id, job.trigger)
    log.info("Scheduler started in %s mode (Ctrl+C to stop)", "LIVE" if opts.live else "DRY-RUN")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
