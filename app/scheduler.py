"""Background jobs. Runs inside the web app process."""
from __future__ import annotations

import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import agent, db

_sched: BackgroundScheduler | None = None
_busy = threading.Lock()


def _guard(name, fn, exclusive: bool = True):
    """Heavy jobs (sync, planning, metrics) never overlap; publishing always runs."""
    def run():
        if exclusive and not _busy.acquire(blocking=False):
            db.log(f"Skipped {name}: another job is running", "warn")
            return
        try:
            fn()
        except Exception as e:
            db.log(f"Job {name} failed: {e}", "error")
        finally:
            if exclusive:
                _busy.release()
    return run


def _weekly():
    agent.weekly_report()
    agent.sync_sources()
    agent.plan_week()


def start() -> BackgroundScheduler:
    global _sched
    if _sched:
        return _sched
    tz = agent.tz()
    s = db.all_settings()
    hh, mm = (s.get("planning_time") or "16:00").split(":")
    _sched = BackgroundScheduler(timezone=tz, job_defaults={"coalesce": True, "max_instances": 1,
                                                             "misfire_grace_time": 3600})
    _sched.add_job(_guard("publish", agent.publish_due, exclusive=False), "interval", minutes=2, id="publish")
    _sched.add_job(_guard("sync", agent.sync_sources), "interval", hours=6, id="sync")
    _sched.add_job(_guard("metrics", agent.fetch_metrics), "interval", hours=3, id="metrics")
    _sched.add_job(_guard("weekly", _weekly), CronTrigger(day_of_week=s.get("planning_day") or "sun",
                                                          hour=int(hh), minute=int(mm), timezone=tz), id="weekly")
    _sched.start()
    db.log("Scheduler started")
    return _sched


def reschedule_weekly() -> None:
    if not _sched:
        return
    s = db.all_settings()
    hh, mm = (s.get("planning_time") or "16:00").split(":")
    _sched.reschedule_job("weekly", trigger=CronTrigger(day_of_week=s.get("planning_day") or "sun",
                                                        hour=int(hh), minute=int(mm), timezone=agent.tz()))


def next_runs() -> dict:
    if not _sched:
        return {}
    return {j.id: j.next_run_time for j in _sched.get_jobs()}
