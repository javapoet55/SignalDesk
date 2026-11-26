"""Scheduled runs: FinViz scans (Scans tab) and strategy runs (Strategies tab → Run schedule).

Jobs live on the volume (/data/strategies/schedules.json) so they survive redeploys.
Each job has a kind:
  * "scan"        run a FinViz scan of your presets
  * "strategies"  run a set of strategies on one ticker source with one price basis
and fires either:
  * once     at a date and time in the schedule's time zone, or
  * weekdays at a time, every Monday–Friday (daylight saving handled).

Ticker source per job:
  "latest"  the newest scan at the moment the job fires
  "fresh"   run a new FinViz scan first, then the strategies on its tickers
  <stamp>   one specific earlier scan
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

GRACE = dt.timedelta(minutes=30)  # a job missed by a restart still runs if we are back within this window
MAX_AHEAD = dt.timedelta(days=90)
KEEP_FINISHED = 30
WEEKDAYS = [0, 1, 2, 3, 4]
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
KINDS = ("strategies", "scan")


def job_kind(job: dict) -> str:
    return job.get("kind") or "strategies"  # jobs saved before scans could be scheduled


def job_days(job: dict) -> List[int]:
    days = job.get("days")
    return sorted({int(d) for d in days if 0 <= int(d) <= 6}) if days else list(WEEKDAYS)


def days_text(days: List[int]) -> str:
    return "every weekday" if list(days) == WEEKDAYS else (
        "every day" if list(days) == list(range(7)) else "every " + ", ".join(DAY_NAMES[d] for d in days))


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_hhmm(text: str) -> dt.time:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("Enter a time like 16:30.")
    return dt.time(int(m.group(1)), int(m.group(2)))


def local_to_utc(day: dt.date, at: dt.time, tz) -> dt.datetime:
    """Wall-clock time in `tz` -> UTC. Times skipped by the spring clock change are rejected."""
    local = dt.datetime.combine(day, at, tzinfo=tz)
    back = local.astimezone(dt.timezone.utc).astimezone(tz)
    if (back.hour, back.minute) != (at.hour, at.minute):
        raise ValueError(f"{at:%H:%M} doesn't exist on {day} (clocks move forward). Pick another time.")
    return local.astimezone(dt.timezone.utc)


def next_weekday_slot(after: dt.datetime, at: dt.time, tz, days: Optional[List[int]] = None) -> dt.datetime:
    """First `at` (in tz) on one of `days` (default Mon–Fri) strictly after `after`."""
    days = days or WEEKDAYS
    d = after.astimezone(tz).date()
    for i in range(0, 9):
        day = d + dt.timedelta(days=i)
        if day.weekday() not in days:
            continue
        cand = dt.datetime.combine(day, at, tzinfo=tz)
        if cand.astimezone(dt.timezone.utc) > after:
            return cand.astimezone(dt.timezone.utc)
    raise ValueError("no slot")  # pragma: no cover


class ScheduleStore:
    def __init__(self, path: Path, tz):
        self.path = path
        self.tz = tz
        self._lock = threading.Lock()

    def _load_doc(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load(self) -> List[dict]:
        jobs = self._load_doc().get("jobs", [])
        return jobs if isinstance(jobs, list) else []

    def _save(self, jobs: List[dict], meta: Optional[dict] = None) -> None:
        doc = self._load_doc()
        doc["jobs"] = jobs
        if meta is not None:
            doc["meta"] = meta
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def jobs(self, kind: Optional[str] = None) -> List[dict]:
        with self._lock:
            jobs = self._load()
        return [j for j in jobs if kind is None or job_kind(j) == kind]

    def starts_scans(self, job: dict) -> bool:
        return job_kind(job) == "scan" or job.get("scan") == "fresh"

    def next_scan(self) -> Optional[Tuple[dt.datetime, dict]]:
        """The next scheduled job that will run a FinViz scan (scan jobs and 'new scan first' strategy jobs)."""
        best: Optional[Tuple[dt.datetime, dict]] = None
        for j in self.jobs():
            if self.starts_scans(j):
                when = self.next_fire(j)
                if when is not None and (best is None or when < best[0]):
                    best = (when, j)
        return best

    def seed_scan_times(self, times: List[dt.time], days: List[int], since: dt.datetime) -> int:
        """One time only: turn the SCHEDULE_TIMES setting into page-managed 'scan' jobs.
        `since` is when they count from, so a slot just before a restart is still caught up."""
        with self._lock:
            doc = self._load_doc()
            meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
            if meta.get("scan_times_seeded"):
                return 0
            jobs = self._load()
            for at in times:
                jobs.append({"id": uuid.uuid4().hex[:10], "kind": "scan", "names": [], "scan": "fresh",
                             "mode": "close", "repeat": "weekdays", "days": sorted(set(days)) or list(WEEKDAYS),
                             "time": f"{at:%H:%M}", "status": "scheduled",
                             "created_utc": since.isoformat(timespec="seconds"), "last_fired_utc": None,
                             "history": [], "origin": "SCHEDULE_TIMES"})
            meta["scan_times_seeded"] = utc_now().isoformat(timespec="seconds")
            self._save(jobs, meta)
            return len(times)

    def next_fire(self, job: dict) -> Optional[dt.datetime]:
        if job.get("status") != "scheduled":
            return None
        if job["repeat"] == "once":
            return dt.datetime.fromisoformat(job["at_utc"])
        base = dt.datetime.fromisoformat(job.get("last_fired_utc") or job["created_utc"])
        return next_weekday_slot(base, parse_hhmm(job["time"]), self.tz, job_days(job))

    MAX_TIMES = 12

    def add_many(self, names: List[str], scan: str, mode: str, repeat: str, date_text: str,
                 time_texts: List[str], now: Optional[dt.datetime] = None, kind: str = "strategies",
                 candles: str = "1d", filters: Optional[dict] = None) -> List[dict]:
        """One job per time (blank entries ignored, repeats merged). All are checked before any is saved,
        so a bad time adds nothing."""
        now = now or utc_now()
        texts = [t.strip() for t in time_texts if (t or "").strip()]
        if not texts:
            raise ValueError("Pick a time.")
        seen, unique = set(), []
        for t in texts:
            key = f"{parse_hhmm(t):%H:%M}"
            if key not in seen:
                seen.add(key)
                unique.append(t)
        if len(unique) > self.MAX_TIMES:
            raise ValueError(f"Pick at most {self.MAX_TIMES} times.")
        jobs = []
        for t in sorted(unique, key=parse_hhmm):
            try:
                job = self._make(names, scan, mode, repeat, date_text, t, now, kind, candles)
                if kind == "strategies" and filters:
                    job["filters"] = dict(filters)
                jobs.append(job)
            except ValueError as err:
                raise ValueError(f"{parse_hhmm(t):%H:%M}: {err}" if len(unique) > 1 else str(err)) from None
        with self._lock:
            existing = self._load()
            for job in jobs:
                self._check_duplicate(existing, job)
            existing.extend(jobs)
            self._save(existing)
        return jobs

    def add(self, names: List[str], scan: str, mode: str, repeat: str, date_text: str, time_text: str,
            now: Optional[dt.datetime] = None, kind: str = "strategies", candles: str = "1d") -> dict:
        now = now or utc_now()
        job = self._make(names, scan, mode, repeat, date_text, time_text, now, kind, candles)
        with self._lock:
            jobs = self._load()
            self._check_duplicate(jobs, job)
            jobs.append(job)
            self._save(jobs)
        return job

    def _make(self, names: List[str], scan: str, mode: str, repeat: str, date_text: str, time_text: str,
              now: dt.datetime, kind: str, candles: str = "1d") -> dict:
        if kind not in KINDS:
            raise ValueError("Unknown schedule type.")
        if kind == "scan":
            names, scan, mode = [], "fresh", "close"
        elif not names:
            raise ValueError("Select at least one strategy.")
        at = parse_hhmm(time_text)
        job = {"id": uuid.uuid4().hex[:10], "kind": kind, "names": list(dict.fromkeys(names)), "scan": scan,
               "mode": mode, "repeat": repeat, "time": f"{at:%H:%M}", "status": "scheduled",
               "created_utc": now.isoformat(timespec="seconds"), "last_fired_utc": None, "history": []}
        if kind != "scan" and candles and candles != "1d":
            job["candles"] = candles
        if repeat == "once":
            try:
                day = dt.date.fromisoformat((date_text or "").strip())
            except ValueError:
                raise ValueError("Pick a date.") from None
            when = local_to_utc(day, at, self.tz)
            if when <= now + dt.timedelta(seconds=30):
                raise ValueError("That time has already passed. Pick a time in the future, or choose Run now.")
            if when > now + MAX_AHEAD:
                raise ValueError("Schedule within the next 90 days.")
            job["at_utc"] = when.isoformat(timespec="seconds")
        elif repeat == "weekdays":
            job["days"] = list(WEEKDAYS)
        else:
            raise ValueError("Unknown schedule type.")
        return job

    def _check_duplicate(self, jobs: List[dict], job: dict) -> None:
        """The same scan twice at the same minute would just be skipped later, so refuse it."""
        if job_kind(job) != "scan":
            return
        for j in jobs:
            if job_kind(j) != "scan" or j.get("status") != "scheduled":
                continue
            if job["repeat"] == "weekdays" and j["repeat"] == "weekdays" and j["time"] == job["time"] \
                    and job_days(j) == WEEKDAYS:
                raise ValueError(f"A scan is already scheduled every weekday at {job['time']}.")
            if job["repeat"] == "once" and self.next_fire(j) == dt.datetime.fromisoformat(job["at_utc"]):
                raise ValueError("A scan is already scheduled at that time.")

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            jobs = self._load()
            for j in jobs:
                if j["id"] == job_id and j["status"] in ("scheduled", "missed"):
                    j["status"] = "cancelled" if j["status"] == "scheduled" else "dismissed"
                    self._save(jobs)
                    return j
        raise ValueError("That schedule no longer exists. Reload the page.")

    def due(self, now: Optional[dt.datetime] = None) -> List[Tuple[dict, dt.datetime]]:
        """Claim every job whose time has come (state is saved BEFORE running, so nothing fires twice)."""
        now = now or utc_now()
        fire: List[Tuple[dict, dt.datetime]] = []
        with self._lock:
            jobs = self._load()
            changed = False
            for j in jobs:
                while j.get("status") == "scheduled":
                    when = self.next_fire(j)
                    if when is None or when > now:
                        break
                    changed = True
                    if now - when > GRACE:  # the server was down at that time
                        j["history"] = (j.get("history", []) + [{"at_utc": when.isoformat(), "result": "missed"}])[-10:]
                        if j["repeat"] == "once":
                            j["status"] = "missed"
                        else:
                            j["last_fired_utc"] = when.isoformat(timespec="seconds")
                        continue
                    j["last_fired_utc"] = when.isoformat(timespec="seconds")
                    j["history"] = (j.get("history", []) + [{"at_utc": when.isoformat(), "result": "started"}])[-10:]
                    if j["repeat"] == "once":
                        j["status"] = "done"
                    fire.append((dict(j), when))
                    break
            if changed:
                finished = [j for j in jobs if j["status"] not in ("scheduled", "missed")]
                keep = {j["id"] for j in finished[-KEEP_FINISHED:]}
                jobs = [j for j in jobs if j["status"] in ("scheduled", "missed") or j["id"] in keep]
                self._save(jobs)
        return fire

    def note_result(self, job_id: str, text: str) -> None:
        with self._lock:
            jobs = self._load()
            for j in jobs:
                if j["id"] == job_id and j.get("history"):
                    j["history"][-1]["result"] = text
                    self._save(jobs)
                    return


def dispatch(job: dict, enqueue_many: Callable[[List[str], str, str, str], List[str]],
             run_fresh_scan: Callable[[], Tuple[Optional[str], str]],
             note: Callable[[str, str], None], log: Callable[[str], None],
             run_scan: Optional[Callable[[], str]] = None) -> None:
    """Run one due job: a scan job runs a scan; a strategies job runs an optional fresh scan, then
    queues its strategies."""
    if job_kind(job) == "scan":
        result = run_scan() if run_scan else "scans are not available"
        note(job["id"], result)
        log(f"scheduled scan {job['id']}: {result}")
        return
    scan = job["scan"]
    if scan == "fresh":
        stamp, why = run_fresh_scan()
        if stamp is None:
            note(job["id"], f"scan failed: {why}")
            log(f"scheduled job {job['id']}: scan failed ({why}); strategies not run")
            return
        scan = stamp
    candles = job.get("candles") or "1d"
    args = (job["names"], scan, job["mode"], "scheduled")
    if job.get("filters"):
        problems = enqueue_many(*args, candles, filters=job["filters"])
    elif candles != "1d":
        problems = enqueue_many(*args, candles)
    else:
        problems = enqueue_many(*args)
    ok = len(job["names"]) - len(problems)
    note(job["id"], f"queued {ok} of {len(job['names'])}" + (f"; {problems[0]}" if problems else ""))
    log(f"scheduled job {job['id']}: queued {ok} strategy run(s) on scan {scan}")
