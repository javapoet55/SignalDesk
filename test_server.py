"""Tests for server.py (standard library only).  Run: python -m unittest -v test_server.py"""
from __future__ import annotations

import base64
import datetime as dt
import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

import schedules as sc
import server as sv

# The live server asks for a fresh emailed code before configuration changes (change_guard).
# Feature tests below exercise what each change does, so they skip that step; TestChangeGuard checks the
# real rule separately.
import server as _sv_guard  # noqa: E402
_REAL_PROTECTED_CHANGE_LABEL = getattr(_sv_guard, '_real_protected_change_label',
                                       _sv_guard.protected_change_label)
_sv_guard._real_protected_change_label = _REAL_PROTECTED_CHANGE_LABEL
_sv_guard.protected_change_label = lambda *a, **k: None
from test_finviz_agent import make_csv, random_stocks

PT = ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc
TIMES = sv.parse_times("12:00,13:30")
WEEKDAYS = sv.parse_days("mon-fri")


def pt(y, m, d, hh, mm, ss=0):
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=PT)


class TestScheduleParsing(unittest.TestCase):
    def test_times(self):
        self.assertEqual(sv.parse_times("13:30, 12:00,12:00"), [dt.time(12, 0), dt.time(13, 30)])
        for bad in ["", "25:00", "12:60", "noon", "1230"]:
            with self.assertRaises(ValueError, msg=bad):
                sv.parse_times(bad)

    def test_days(self):
        self.assertEqual(sv.parse_days("mon-fri"), [0, 1, 2, 3, 4])
        self.assertEqual(sv.parse_days("Monday, wed,FRI"), [0, 2, 4])
        self.assertEqual(sv.parse_days("fri-mon"), [0, 4, 5, 6])
        for bad in ["", "funday", "mon-xyz"]:
            with self.assertRaises(ValueError, msg=bad):
                sv.parse_days(bad)

    def test_settings_defaults_and_errors(self):
        s = sv.Settings.from_env({"RAILWAY_VOLUME_MOUNT_PATH": "/data", "PORT": "1234"})
        self.assertEqual((s.tz_name, s.times, s.days, s.port, s.user), ("America/Los_Angeles", TIMES, WEEKDAYS,
                                                                      1234, "admin"))
        self.assertEqual(s.data_dir, Path("/data"))
        self.assertEqual(s.schedule_text(), "Mon–Fri at 12:00, 13:30 (America/Los_Angeles)")
        self.assertFalse(s.token_present)
        with self.assertRaises(ValueError):
            sv.Settings.from_env({"SCHEDULE_TZ": "Pacific/Nowhere"})
        with self.assertRaises(ValueError):
            sv.Settings.from_env({"KEEP_DAYS": "ten"})


class TestScanSchedule(unittest.TestCase):
    """Scan times live on the Scans tab now. SCHEDULE_TIMES only seeds them, once."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def app(self, **env):
        base = {"DATA_DIR": str(self.tmp / "vol"), "DASHBOARD_PASSWORD": "pw",
                "SCREENS_JSON": json.dumps({"builtin_signals": {"include": ["Top Gainers"]}})}
        base.update(env)
        return sv.build_app(base, log=lambda m: None)

    def scan_jobs(self, app, status="scheduled"):
        return [j for j in app.schedules.jobs("scan") if j["status"] == status]

    def test_first_start_imports_schedule_times_once(self):
        app = self.app()  # SCHEDULE_TIMES not set: the old default 12:00 and 13:30
        jobs = self.scan_jobs(app)
        self.assertEqual(sorted(j["time"] for j in jobs), ["12:00", "13:30"])
        self.assertTrue(all(j["repeat"] == "weekdays" and sc.job_days(j) == [0, 1, 2, 3, 4] for j in jobs))
        self.assertEqual(sv.scan_schedule_summary(app), "Every weekday at 12:00, 13:30 (America/Los_Angeles)")
        app.schedules.cancel([j for j in jobs if j["time"] == "13:30"][0]["id"])
        again = self.app(SCHEDULE_TIMES="09:00")  # restart; the variable no longer matters
        self.assertEqual([j["time"] for j in self.scan_jobs(again)], ["12:00"])

    def test_existing_strategy_schedules_are_kept_when_importing(self):
        vol = self.tmp / "vol" / "strategies"
        vol.mkdir(parents=True)
        old = {"jobs": [{"id": "abc", "names": ["x"], "scan": "latest", "mode": "close", "repeat": "weekdays",
                         "time": "09:00", "status": "scheduled", "created_utc": "2026-10-20T00:00:00+00:00",
                         "last_fired_utc": None, "history": []}]}
        (vol / "schedules.json").write_text(json.dumps(old))  # saved by the previous version
        app = self.app()
        self.assertEqual([j["id"] for j in app.schedules.jobs("strategies")], ["abc"])
        self.assertEqual(len(self.scan_jobs(app)), 2)

    def test_off_and_custom_days(self):
        self.assertEqual(self.scan_jobs(self.app(SCHEDULE_TIMES="off")), [])
        shutil.rmtree(self.tmp / "vol")
        app = self.app(SCHEDULE_TIMES="10:00", SCHEDULE_DAYS="mon,wed,fri")
        (job,) = self.scan_jobs(app)
        self.assertEqual(sc.job_days(job), [0, 2, 4])
        self.assertEqual(sv.scan_schedule_summary(app), "Every Mon, Wed, Fri at 10:00 (America/Los_Angeles)")
        job = dict(job, last_fired_utc=pt(2026, 10, 26, 10, 0).astimezone(UTC).isoformat())  # Monday
        self.assertEqual(app.schedules.next_fire(job), pt(2026, 10, 28, 10, 0))  # -> Wednesday

    def seeded_store(self, now, last_slot=None):
        state = sv.State(self.tmp / f"state{now.timestamp()}.json")
        if last_slot:
            state.update(last_slot=last_slot.isoformat())
        store = sc.ScheduleStore(self.tmp / f"sched{now.timestamp()}.json", PT)
        settings = sv.Settings.from_env({"DATA_DIR": str(self.tmp)})
        sv.seed_scan_schedule(settings, state, store, now.astimezone(UTC), log=lambda m: None)
        return store

    def test_redeploy_at_scan_time_still_catches_up_but_never_twice(self):
        # back 10 minutes after Monday noon; noon did not run -> it runs now (as the old timer did)
        store = self.seeded_store(pt(2026, 10, 26, 12, 10), last_slot=pt(2026, 10, 23, 13, 30))
        fired = store.due(pt(2026, 10, 26, 12, 10).astimezone(UTC))
        self.assertEqual([(j["time"], w) for j, w in fired], [("12:00", pt(2026, 10, 26, 12, 0))])
        # noon already ran before the redeploy -> nothing runs again
        store = self.seeded_store(pt(2026, 10, 26, 12, 11), last_slot=pt(2026, 10, 26, 12, 0))
        self.assertEqual(store.due(pt(2026, 10, 26, 12, 11).astimezone(UTC)), [])
        # back long after noon -> no late run and no fake "missed" entries
        store = self.seeded_store(pt(2026, 10, 26, 13, 5))
        self.assertEqual(store.due(pt(2026, 10, 26, 13, 5).astimezone(UTC)), [])
        self.assertTrue(all(not j["history"] for j in store.jobs()))

    def test_fires_once_per_slot_and_follows_clock_change(self):
        store = self.seeded_store(pt(2026, 10, 30, 11, 0))  # Friday before clocks fall back
        fired = store.due(pt(2026, 10, 30, 12, 0, 30).astimezone(UTC))
        self.assertEqual([w for _, w in fired], [pt(2026, 10, 30, 12, 0)])
        self.assertEqual(store.due(pt(2026, 10, 30, 12, 1).astimezone(UTC)), [])
        fired = store.due(pt(2026, 10, 30, 13, 30).astimezone(UTC))
        self.assertEqual([(w.astimezone(UTC).hour, w.astimezone(UTC).minute) for _, w in fired], [(20, 30)])
        nxt = {j["time"]: store.next_fire(j) for j in store.jobs()}
        self.assertEqual(nxt["12:00"], pt(2026, 11, 2, 12, 0))            # weekend skipped
        self.assertEqual(nxt["12:00"].astimezone(UTC).hour, 20)          # noon PST = 20:00 UTC
        self.assertEqual(store.due(pt(2026, 10, 31, 12, 0).astimezone(UTC)), [])  # Saturday


class TestPrune(unittest.TestCase):
    def test_keep_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = sv.Settings.from_env({"DATA_DIR": tmp, "KEEP_DAYS": "30"})
            for name in ["2026-08-01_120000", "2026-10-20_120000", "not-a-run"]:
                (s.reports_dir / name).mkdir(parents=True)
            s.logs_dir.mkdir()
            (s.logs_dir / "2026-08-01_120000_schedule.log").write_text("x")
            (s.logs_dir / "2026-10-20_120000_schedule.log").write_text("x")
            removed = sv.prune_old(s, dt.date(2026, 10, 25))
            self.assertEqual(sorted(removed), ["2026-08-01_120000", "2026-08-01_120000_schedule.log"])
            self.assertTrue((s.reports_dir / "not-a-run").exists())
            self.assertTrue((s.reports_dir / "2026-10-20_120000").exists())
            self.assertEqual(sv.prune_old(sv.Settings.from_env({"DATA_DIR": tmp}), dt.date(2030, 1, 1)), [])


class LiveServer:
    """Starts the real HTTP server on a free port with the real agent in offline mode."""

    def __init__(self, env_extra=None, password="pw-123", cfg=None, tmp=None):
        self.tmp = tmp or tempfile.mkdtemp()
        root = Path(self.tmp)
        data = root / "csv"
        data.mkdir(exist_ok=True)
        self.csv_dir = data
        cfg = cfg or {"top_n": 5, "saved_presets": [], "builtin_signals": {"include": ["Top Gainers", "New High"]}}
        for slug, seed in (("top-gainers", 1), ("new-high", 2)):
            (data / f"{slug}.csv").write_text(make_csv(random_stocks(30, seed)))
        env = {"DATA_DIR": str(root / "vol"), "PORT": "0", "DASHBOARD_PASSWORD": password,
               "SCREENS_JSON": json.dumps(cfg), "FINVIZ_AGENT_EXTRA_ARGS": f"--offline {data}",
               "FINVIZ_API_TOKEN": "unused-offline"}
        env.update(env_extra or {})
        self.app = sv.build_app(env, log=lambda m: None)
        self.app.open_marks.client = None  # Offline fixtures never call live quote APIs.
        self.httpd = sv.ThreadingHTTPServer(("127.0.0.1", 0), sv.Handler)
        self.httpd.app = self.app
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def request(self, method, path, auth=("admin", "pw-123"), headers=None, form=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        if auth:
            h["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r, body

    def close(self, keep_files=False):
        self.httpd.shutdown()
        self.httpd.server_close()
        if not keep_files:
            shutil.rmtree(self.tmp, ignore_errors=True)


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = LiveServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def test_config_was_validated(self):
        self.assertIsNone(self.srv.app.config_error)
        self.assertEqual(self.srv.app.screen_count, 2)

    def test_healthz_needs_no_password(self):
        r, body = self.srv.request("GET", "/healthz", auth=None)
        self.assertEqual((r.status, body), (200, b"ok"))

    def test_auth(self):
        r, _ = self.srv.request("GET", "/", auth=None)
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))  # signed out -> login page
        r, _ = self.srv.request("GET", "/", auth=("admin", "wrong"))
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))
        r, _ = self.srv.request("GET", "/", auth=("someone", "pw-123"))
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))
        r, body = self.srv.request("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn("Scheduled scans: Every weekday at 12:00, 13:30".encode(), body)
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))

    def test_path_traversal_and_unknown_files_blocked(self):
        for path in ["/runs/../state.json", "/runs/2026-10-25_120000/../../state.json",
                     "/runs/2026-10-25_120000/raw.csv", "/runs/x/report.html", "/logs/..%2Fstate.json",
                     "/logs/../state.json", "/state.json", "/screens.runtime.json", "/latest/../state.json"]:
            r, _ = self.srv.request("GET", path)
            self.assertEqual(r.status, 404, path)

    def test_cross_site_post_rejected(self):
        r, _ = self.srv.request("POST", "/run", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.srv.request("POST", "/run", auth=None)
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))

    def test_run_now_end_to_end(self):
        host = f"127.0.0.1:{self.srv.port}"
        r, _ = self.srv.request("POST", "/run", headers={"Origin": f"http://{host}", "Host": host})
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Location"), "/?started=1")
        r2, _ = self.srv.request("POST", "/run", headers={"Origin": f"http://{host}"})
        self.assertIn(r2.getheader("Location"), ("/?busy=1", "/?started=1"))  # busy unless already done
        r, body = self.srv.request("GET", "/?started=1")
        self.assertEqual(r.status, 200)
        self.srv.app.runner.wait(120)
        last = self.srv.app.state.get("last_run")
        self.assertEqual(last["exit_code"], 0, (self.srv.app.settings.logs_dir / last["log"]).read_text())
        runs = sv.list_runs(self.srv.app.settings)
        self.assertGreaterEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.files, list(sv.RUN_FILES))
        # run folder is named in Pacific time even though the clock source is UTC
        self.assertEqual(run.when.tzinfo, PT)
        r, body = self.srv.request("GET", "/")
        page = body.decode()
        self.assertIn(f"/runs/{run.stamp}/tickers.txt", page)
        self.assertIn("Copy tickers", page)
        for name, ctype in sv.RUN_FILES.items():
            r, body = self.srv.request("GET", f"/runs/{run.stamp}/{name}")
            self.assertEqual(r.status, 200, name)
            self.assertEqual(r.getheader("Content-Type"), ctype)
        r, body = self.srv.request("GET", "/latest/tickers.txt")
        lines = body.decode().splitlines()
        self.assertTrue(lines[0].startswith("Run: ") and lines[0].endswith(("PDT", "PST")))
        self.assertEqual(lines[1].split(","), run.tickers)
        r, _ = self.srv.request("GET", "/latest/report.html")
        self.assertEqual(r.status, 200)
        r, body = self.srv.request("GET", f"/logs/{last['log']}")
        self.assertEqual(r.status, 200)
        self.assertIn(b"Exit code 0", body)


class TestScanScheduleWeb(unittest.TestCase):
    """Scans tab: Run now / Schedule for later / Every weekday, through the real web server."""

    def setUp(self):
        self.srv = LiveServer(env_extra={"SCHEDULE_TIMES": "off"})
        self.host = f"127.0.0.1:{self.srv.port}"

    def tearDown(self):
        self.srv.app.runner.wait(120)
        self.srv.close()

    def post(self, path, form, origin=True):
        headers = {"Origin": f"http://{self.host}"} if origin else {"Origin": "https://evil.example"}
        return self.srv.request("POST", path, headers=headers, form=form)

    def page(self):
        return self.srv.request("GET", "/")[1].decode()

    def test_page_has_the_scheduler(self):
        page = self.page()
        for text in ("Run a scan", "Run schedule", ">Run now<", ">Schedule for later<", ">Every weekday<",
                     "Scheduled scans", "None. Pick Schedule for later or Every weekday above.",
                     "Scheduled scans: None scheduled", "Next scan</div><div class='v'>None scheduled"):
            self.assertIn(text, page, text)

    def test_schedule_once_and_weekdays_then_cancel(self):
        tomorrow = (dt.datetime.now(PT) + dt.timedelta(days=1)).strftime("%Y-%m-%d")
        r, _ = self.post("/scans/go", {"when": "once", "date": tomorrow, "time": "16:30"})
        self.assertEqual((r.status, r.getheader("Location")), (303, "/#scan"))
        page = self.page()
        self.assertIn("Scheduled: Once · ", page)
        self.assertRegex(page, r"16:30 P[DS]T")
        self.post("/scans/go", {"when": "weekdays", "time": "09:15"})
        page = self.page()
        self.assertIn("Scheduled: Every weekday at 09:15 · next ", page)
        self.assertIn("Scheduled scans: Every weekday at 09:15 · 1 one-time scan", page)
        jobs = self.srv.app.schedules.jobs("scan")
        self.assertEqual(sorted(j["repeat"] for j in jobs), ["once", "weekdays"])
        nxt = self.srv.app.schedules.next_scan()
        self.assertIn(f"Next scan</div><div class='v'>{nxt[0].astimezone(PT):%a %b %d, %H:%M %Z}", page)
        # the same scan twice is refused
        self.post("/scans/go", {"when": "weekdays", "time": "09:15"})
        self.assertIn("A scan is already scheduled every weekday at 09:15.", self.page())
        self.post("/scans/go", {"when": "once", "date": tomorrow, "time": "16:30"})
        self.assertIn("A scan is already scheduled at that time.", self.page())
        # not on the Strategies tab's list, but its summary mentions it
        strat = self.srv.request("GET", "/strategies")[1].decode()
        for j in jobs:
            self.assertNotIn(f"value='{j['id']}'", strat)
        self.assertIn("Scan schedule: Every weekday at 09:15 · 1 one-time scan", strat)
        weekly = [j for j in jobs if j["repeat"] == "weekdays"][0]
        self.post("/scans/schedule/cancel", {"id": weekly["id"]})
        self.assertIn("Scheduled scan cancelled.", self.page())
        self.assertEqual([j["repeat"] for j in self.srv.app.schedules.jobs("scan") if j["status"] == "scheduled"],
                         ["once"])

    def test_bad_inputs_and_security(self):
        for form, msg in [({"when": "once", "date": "2020-01-01", "time": "10:00"}, "already passed"),
                          ({"when": "once", "date": "", "time": "10:00"}, "Pick a date."),
                          ({"when": "weekdays", "time": "25:00"}, "Enter a time like 16:30."),
                          ({"when": "monthly", "time": "10:00"}, "Unknown schedule type."),
                          ({"when": "once", "date": "2027-03-14", "time": "02:30"}, "doesn't exist on 2027-03-14")]:
            self.post("/scans/go", form)
            self.assertIn(html_escape(msg), self.page(), form)
        self.assertEqual(self.srv.app.schedules.jobs("scan"), [])
        r, _ = self.post("/scans/go", {"when": "weekdays", "time": "09:15"}, origin=False)
        self.assertEqual(r.status, 403)
        r, _ = self.srv.request("POST", "/scans/go", auth=None, form={"when": "now"})
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))
        self.assertEqual(self.srv.app.schedules.jobs("scan"), [])
        # a strategy schedule can't be cancelled through the Scans tab
        strat = self.srv.app.schedules.add(["x"], "latest", "close", "weekdays", "", "08:00")
        self.post("/scans/schedule/cancel", {"id": strat["id"]})
        self.assertIn("That schedule no longer exists", self.page())
        self.assertEqual(self.srv.app.schedules.jobs("strategies")[0]["status"], "scheduled")

    def test_weekday_scan_shows_its_last_result(self):
        store = self.srv.app.schedules
        job = store.add([], "fresh", "close", "weekdays", "", "11:45", kind="scan")
        when = store.next_fire(job)
        fired = store.due(when + dt.timedelta(minutes=1))
        self.assertEqual([j["id"] for j, _ in fired], [job["id"]])
        store.note_result(job["id"], "ok · 12 tickers")
        page = self.page()
        self.assertIn(f"Last: {when.astimezone(PT):%b %d, %H:%M} · ok · 12 tickers", page)
        nxt = store.next_fire([j for j in store.jobs("scan") if j["id"] == job["id"]][0])
        self.assertGreater(nxt, when)
        self.assertIn(f"Every weekday at 11:45 · next {nxt.astimezone(PT):%a %b %d, %H:%M %Z}", page)

    def test_run_now_from_the_scheduler_form(self):
        self.post("/scans/go", {"when": "now"})
        self.assertIn("Scan started. This page refreshes until it finishes.", self.page())
        if self.srv.app.runner.running:
            self.post("/scans/go", {"when": "now"})
            self.assertIn("A scan is already running.", self.page())
        self.srv.app.runner.wait(120)
        self.assertEqual(self.srv.app.state.get("last_run")["reason"], "manual")
        self.assertEqual(self.srv.app.state.get("last_run")["exit_code"], 0)

    def test_scheduled_scan_runs_the_agent_once(self):
        app = self.srv.app
        now = dt.datetime.now(UTC)
        at = (now + dt.timedelta(minutes=5)).astimezone(PT)
        job = app.schedules.add([], "fresh", "close", "once", f"{at:%Y-%m-%d}", f"{at:%H:%M}", now=now, kind="scan")
        later = now + dt.timedelta(minutes=6)
        self.assertEqual(sv.schedule_tick(app, now=later, log=lambda m: None, background=False), 1)
        self.assertEqual(sv.schedule_tick(app, now=later, log=lambda m: None, background=False), 0)  # once only
        last = app.state.get("last_run")
        self.assertEqual((last["reason"], last["exit_code"]), ("schedule", 0))
        run = sv.list_runs(app.settings)[0]
        done = [j for j in app.schedules.jobs("scan") if j["id"] == job["id"]][0]
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["history"][-1]["result"], f"ok · {len(run.tickers)} tickers")
        page = self.page()  # a finished one-time scan leaves the list; its run shows as the last run
        self.assertIn("(schedule) · <a href='/logs/", page)
        self.assertIn(f"/runs/{run.stamp}/report.html", page)
        self.assertNotIn(f"value='{job['id']}'", page)
        # a scheduled scan that comes due while another scan is running joins it instead of failing
        self.assertTrue(app.runner.start("manual"))
        text = sv.run_scheduled_scan(app, poll=0.1)
        self.assertTrue(text.startswith("ok"), text)
        self.assertIn("joined the scan already running", text)


URL_NEW = "https://elite.finviz.com/screener/stocks?v=111&f=cap_small,sh_price_o5&ft=4"
URL_OLD = "https://elite.finviz.com/screener.ashx?v=111&f=sh_avgvol_o500,ta_highlow52w_nh&ft=4"


class TestPresets(unittest.TestCase):
    def setUp(self):
        self.srv = LiveServer()
        self.host = f"127.0.0.1:{self.srv.port}"
        self.cfg_path = self.srv.app.settings.data_dir / "screens.json"

    def tearDown(self):
        self.srv.close()

    def post(self, path, form, origin=True, auth=("admin", "pw-123")):
        headers = {"Origin": f"http://{self.host}"} if origin else {"Origin": "https://evil.example"}
        return self.srv.request("POST", path, auth=auth, headers=headers, form=form)

    def page(self):
        return self.srv.request("GET", "/")[1].decode()

    def saved(self):
        return json.loads(self.cfg_path.read_text())["saved_presets"]

    def test_seeded_on_volume_from_screens_json(self):
        self.assertTrue(self.cfg_path.is_file())
        self.assertEqual(self.saved(), [])
        self.assertIn("Your presets", self.page())
        self.assertIn("No saved presets yet", self.page())
        self.assertIn("Also scanning 2 built-in FinViz screens", self.page())

    def test_usual_top_per_screen(self):
        r, _ = self.post("/presets/topn", {"top_n": "25"})
        self.assertEqual(r.getheader("Location"), "/#presets")
        self.assertEqual(json.loads(self.cfg_path.read_text())["top_n"], 25)
        page = self.page()
        self.assertIn("Every screen will keep its top 25 gainers from the next scan", page)
        self.assertIn("name='top_n' min='1' max='500' step='1' value='25'", page)
        self.assertIn("Top 25 (usual)", page)
        before = self.cfg_path.read_text()
        for v, msg in (("0", "Pick 1–500"), ("501", "Pick 1–500"), ("ten", "whole number")):
            self.post("/presets/topn", {"top_n": v})
            self.assertIn(msg, self.page(), v)
            self.assertEqual(self.cfg_path.read_text(), before, v)
        self.assertEqual(self.post("/presets/topn", {"top_n": "5"}, origin=False)[0].status, 403)
        self.assertEqual(self.cfg_path.read_text(), before)

    def test_keep_setting_per_preset(self):
        r, _ = self.post("/presets/add", {"name": "Market Top Gainers", "url": URL_NEW, "keep": "all"})
        self.assertIn("Added &#x27;Market Top Gainers&#x27;", self.page())
        self.post("/presets/add", {"name": "Momentum", "url": URL_OLD})
        self.assertEqual(self.saved(), [{"name": "Market Top Gainers", "url": URL_NEW, "keep": "all"},
                                        {"name": "Momentum", "url": URL_OLD}])
        page = self.page()
        self.assertIn("<th>Keep</th>", page)
        self.assertIn("<option value='all' selected>All</option>", page)
        self.assertIn("<option value='' selected>Top 5 (usual)</option>", page)  # this server's top_n is 5
        r, _ = self.post("/presets/keep", {"index": "1", "name": "Momentum", "keep": "20"})
        self.assertEqual(r.getheader("Location"), "/#presets")
        self.assertIn("&#x27;Momentum&#x27; will keep the top 20 from the next scan.", self.page())
        self.assertEqual(self.saved()[1]["keep"], "20")
        self.post("/presets/keep", {"index": "1", "name": "Momentum", "keep": ""})
        self.assertNotIn("keep", self.saved()[1])
        self.assertIn("will keep the top 5 (the usual)", self.page())
        before = self.cfg_path.read_text()
        for form, msg in (({"index": "0", "name": "Market Top Gainers", "keep": "lots"}, "must be a number"),
                          ({"index": "0", "name": "Market Top Gainers", "keep": "0"}, "between 1 and"),
                          ({"index": "1", "name": "Market Top Gainers", "keep": "all"}, "preset list changed"),
                          ({"index": "9", "name": "X", "keep": "all"}, "preset list changed")):
            self.post("/presets/keep", form)
            self.assertIn(msg, self.page(), form)
            self.assertEqual(self.cfg_path.read_text(), before, form)
        r, _ = self.post("/presets/keep", {"index": "0", "name": "Market Top Gainers", "keep": "5"}, origin=False)
        self.assertEqual(r.status, 403)
        self.assertEqual(self.cfg_path.read_text(), before)

    def test_add_valid_both_url_styles(self):
        for name, url in (("Small caps", URL_NEW), ("New highs liquid", URL_OLD)):
            r, _ = self.post("/presets/add", {"name": name, "url": url})
            self.assertEqual((r.status, r.getheader("Location")), (303, "/#presets"))
            self.assertIn(f"Added &#x27;{name}&#x27;", self.page())
        self.assertEqual(self.saved(), [{"name": "Small caps", "url": URL_NEW},
                                        {"name": "New highs liquid", "url": URL_OLD}])
        self.assertEqual(self.srv.app.screen_count, 4)
        self.assertIsNone(self.srv.app.config_error)
        page = self.page()
        self.assertIn("Small caps", page)
        self.assertIn("rel='noopener noreferrer'", page)

    def test_rejections_leave_file_unchanged(self):
        before = self.cfg_path.read_text()
        cases = [
            ({"name": "", "url": URL_NEW}, "Enter a name"),
            ({"name": "X", "url": ""}, "Paste the preset"),
            ({"name": "X", "url": "https://example.com/screener.ashx?f=a"}, "finviz.com"),
            ({"name": "X", "url": "javascript:alert(1)"}, "finviz.com"),
            ({"name": "X", "url": "https://finviz.com/screener/stocks?v=111"}, "no filters"),
            ({"name": "X", "url": "https://finviz.com/quote.ashx?t=AAPL"}, "screener page"),
            ({"name": "N" * 81, "url": URL_NEW}, "under 80"),
        ]
        for form, expected in cases:
            r, _ = self.post("/presets/add", form)
            self.assertEqual(r.status, 303)
            self.assertIn(expected, self.page(), form)
        self.assertEqual(self.cfg_path.read_text(), before)
        self.assertEqual(self.srv.app.screen_count, 2)

    def test_duplicate_name_case_insensitive(self):
        self.post("/presets/add", {"name": "Momentum", "url": URL_NEW})
        self.page()
        self.post("/presets/add", {"name": "  momentum ", "url": URL_OLD})
        self.assertIn("already exists", self.page())
        self.assertEqual(len(self.saved()), 1)

    def test_names_are_escaped(self):
        evil = "<img src=x onerror=alert(1)>"
        self.post("/presets/add", {"name": evil, "url": URL_NEW})
        page = self.page()
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)

    def test_remove(self):
        self.post("/presets/add", {"name": "A", "url": URL_NEW})
        self.post("/presets/add", {"name": "B", "url": URL_OLD})
        self.page()
        r, _ = self.post("/presets/remove", {"index": "0", "name": "B"})  # stale/mismatched -> refused
        self.assertIn("list changed", self.page())
        self.assertEqual([p["name"] for p in self.saved()], ["A", "B"])
        self.post("/presets/remove", {"index": "0", "name": "A"})
        page = self.page()
        self.assertIn("Removed &#x27;A&#x27;", page)
        self.assertIn(html_escape(URL_NEW), page)  # URL shown so it can be re-added
        self.assertEqual([p["name"] for p in self.saved()], ["B"])
        self.post("/presets/remove", {"index": "abc", "name": "B"})
        self.assertIn("list changed", self.page())
        self.assertEqual(self.srv.app.screen_count, 3)

    def test_cannot_remove_last_screen(self):
        self.srv.close()
        cfg = {"saved_presets": [{"name": "Only", "url": URL_NEW}], "builtin_signals": {"enabled": False}}
        self.srv = LiveServer(cfg=cfg)
        self.host = f"127.0.0.1:{self.srv.port}"
        self.cfg_path = self.srv.app.settings.data_dir / "screens.json"
        self.post("/presets/remove", {"index": "0", "name": "Only"})
        self.assertIn("nothing left to scan", self.page())
        self.assertEqual(len(self.saved()), 1)

    def test_security_on_preset_routes(self):
        r, _ = self.post("/presets/add", {"name": "X", "url": URL_NEW}, origin=False)
        self.assertEqual(r.status, 403)
        r, _ = self.post("/presets/add", {"name": "X", "url": URL_NEW}, auth=None)
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))
        r, _ = self.post("/presets/add", {"name": "X", "url": "https://finviz.com/screener.ashx?f=" + "a" * 20000})
        self.assertEqual(r.status, 413)
        self.assertEqual(self.saved(), [])

    def test_persists_across_restart_and_env_no_longer_used(self):
        self.post("/presets/add", {"name": "Keeper", "url": URL_NEW})
        tmp = self.srv.tmp
        self.srv.close(keep_files=True)
        other = {"saved_presets": [{"name": "From env", "url": URL_OLD}],
                 "builtin_signals": {"include": ["Top Gainers"]}}
        self.srv = LiveServer(tmp=tmp, cfg=other)  # restart with a DIFFERENT SCREENS_JSON
        self.host = f"127.0.0.1:{self.srv.port}"
        self.assertEqual([p["name"] for p in self.saved()], ["Keeper"])
        page = self.page()
        self.assertIn("Keeper", page)
        self.assertNotIn("From env", page)
        self.assertIn("SCREENS_JSON variable in Railway is no longer used", page)

    def test_scheduled_run_uses_page_presets(self):
        self.post("/presets/add", {"name": "My Screen", "url": URL_NEW})
        (self.srv.csv_dir / "my-screen.csv").write_text(make_csv(random_stocks(20, 77)))
        self.assertTrue(self.srv.app.runner.start("manual"))
        self.srv.app.runner.wait(120)
        self.assertEqual(self.srv.app.state.get("last_run")["exit_code"], 0)
        run = sv.list_runs(self.srv.app.settings)[0]
        summary = json.loads((self.srv.app.settings.reports_dir / run.stamp / "summary.json").read_text())
        status = {s["name"]: (s["type"], s["status"]) for s in summary["screens"]}
        self.assertEqual(status["My Screen"], ("Saved preset", "ok"))


def html_escape(s):
    import html
    return html.escape(s)


class TestSeeding(unittest.TestCase):
    def test_uploaded_file_used_when_no_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = sv.build_app({"DATA_DIR": tmp, "DASHBOARD_PASSWORD": "x"}, log=lambda m: None)
            seeded = json.loads((Path(tmp) / "screens.json").read_text())
            shipped = json.loads((Path(sv.__file__).with_name("screens.json")).read_text())
            self.assertEqual(seeded, shipped)
            self.assertEqual(app.runner.config_path, Path(tmp) / "screens.json")

    def test_invalid_env_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = sv.build_app({"DATA_DIR": tmp, "SCREENS_JSON": "[1,2]"}, log=lambda m: None)
            self.assertIn("must contain a JSON object", app.config_error)
            self.assertFalse((Path(tmp) / "screens.json").exists())


class TestLockedAndBadConfig(unittest.TestCase):
    def test_no_password_locks_page_but_health_ok(self):
        srv = LiveServer(password="")
        try:
            r, _ = srv.request("GET", "/", auth=None)
            self.assertEqual(r.status, 503)
            r, _ = srv.request("GET", "/healthz", auth=None)
            self.assertEqual(r.status, 200)
        finally:
            srv.close()

    def test_bad_screens_json_reported(self):
        srv = LiveServer(env_extra={"SCREENS_JSON": "{not json"})
        try:
            self.assertIn("not valid JSON", srv.app.config_error)
            r, body = srv.request("GET", "/")
            self.assertIn(b"Configuration problem", body)
        finally:
            srv.close()

    def test_missing_token_banner(self):
        srv = LiveServer(env_extra={"FINVIZ_API_TOKEN": ""})
        try:
            r, body = srv.request("GET", "/")
            self.assertIn(b"FINVIZ_API_TOKEN is not set", body)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()


class TestChangeGuard(unittest.TestCase):
    """Every configuration change needs a fresh emailed code, including the newer ones (TV link, preset Keep,
    usual top per screen, candle size on schedules). Uses the real rule, not the feature-test shortcut."""

    def test_rule_covers_every_configuration_change(self):
        label = _REAL_PROTECTED_CHANGE_LABEL
        form = lambda d: urllib.parse.urlencode(d, doseq=True).encode()
        ct = "application/x-www-form-urlencoded"
        for path, data, words in (
                ("/strategies/tvlink", {"name": "fmfi_scanner", "url": "https://www.tradingview.com/chart/x/"},
                 ("TradingView link", "Name: fmfi_scanner", "Link: https://www.tradingview.com/chart/x/")),
                ("/presets/keep", {"index": "0", "name": "Market Top Gainers", "keep": "all"},
                 ("Keep setting", "Name: Market Top Gainers", "Keep: all")),
                ("/presets/topn", {"top_n": "25"}, ("usual top per screen", "Top per screen: 25")),
                ("/strategies/go", {"s": "fmfi_scanner", "when": "weekdays", "time": "13:10", "cn": "5", "cu": "days"},
                 ("strategy schedule", "Candles: 5 days", "Time: 13:10")),
                ("/strategies/autoset", {"s": "fmfi_scanner", "auto": "on", "cn": "15", "cu": "minutes"},
                 ("automatic strategy settings", "Candles: 15 minutes")),
                ("/strategies/delete", {"name": "x"}, ("recycle bin",)),
                ("/presets/add", {"name": "X", "url": "https://elite.finviz.com/screener.ashx?f=a", "keep": "all"},
                 ("Add screener preset", "Keep: all"))):
            got = label(path, form(data), ct)
            self.assertIsNotNone(got, path)
            for w in words:
                self.assertIn(w, got, (path, w))
        # running now, or a one-off read, needs no code
        self.assertIsNone(label("/strategies/go", form({"s": "x", "when": "now", "cn": "5", "cu": "days"}), ct))
        self.assertIsNone(label("/strategies/run", form({"name": "x"}), ct))

    def test_change_is_not_applied_without_a_fresh_code(self):
        srv = LiveServer()
        try:
            sv.protected_change_label = _REAL_PROTECTED_CHANGE_LABEL
            cfg_path = srv.app.settings.data_dir / "screens.json"
            before = cfg_path.read_text()
            host = f"127.0.0.1:{srv.port}"
            for path, data in (("/presets/topn", {"top_n": "25"}),
                               ("/presets/keep", {"index": "0", "name": "x", "keep": "all"})):
                # a script with the password (no browser session) can't make protected changes at all
                r, _ = srv.request("POST", path, headers={"Origin": f"http://{host}"}, form=data)
                self.assertEqual((r.status, r.getheader("Location")), (303, "/login"), path)
                self.assertEqual(cfg_path.read_text(), before, path)  # nothing changed
        finally:
            sv.protected_change_label = lambda *a, **k: None
            srv.close()


class TestJournalWeb(unittest.TestCase):
    """Journal tab through the real HTTP server: import, add, edit, notes, delete, stats, export, safety."""

    CSV = (b"Date/Time,Symbol,Side,Quantity,Price,Commission\n"
           b"2026-10-28 09:31:00,AAPL,Buy,10,100,1\n"
           b"2026-10-28 10:00:00,AAPL,Sell,10,105,1\n"
           b"2026-10-29 09:45:00,TSLA,Sell,5,200,0.5\n"
           b"2026-10-29 11:00:00,TSLA,Buy,5,210,0.5\n"
           b"2026-10-30 09:35:00,KO,Buy,20,60,0\n")

    def setUp(self):
        self.srv = LiveServer()
        self.srv.app.clock = lambda: dt.datetime(2026, 10, 31, 20, tzinfo=dt.timezone.utc)
        self.host = f"127.0.0.1:{self.srv.port}"

    def tearDown(self):
        self.srv.close()

    def get(self, path):
        r, body = self.srv.request("GET", path)
        return r, body.decode("utf-8-sig")

    def post(self, path, form, origin=True):
        headers = {"Origin": f"http://{self.host}" if origin else "https://evil.example"}
        return self.srv.request("POST", path, headers=headers, form=form)

    def upload(self, name, content):
        b = "jrnlboundary"
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: text/csv\r\n\r\n").encode() + content + f"\r\n--{b}--\r\n".encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=30)
        conn.request("POST", "/journal/import", body=body, headers={
            "Authorization": "Basic " + base64.b64encode(b"admin:pw-123").decode(),
            "Content-Type": f"multipart/form-data; boundary={b}", "Origin": f"http://{self.host}"})
        r = conn.getresponse()
        r.read()
        conn.close()
        return r

    def test_empty_journal_and_tab(self):
        r, page = self.get("/journal")
        self.assertEqual(r.status, 200)
        self.assertIn("No trades yet", page)
        self.assertIn("href='/journal' class='on'", page)
        _, home = self.get("/")
        self.assertIn(">Journal</a>", home)

    def test_import_then_stats_calendar_export(self):
        r = self.upload("trades.csv", self.CSV)
        self.assertEqual((r.status, r.getheader("Location")), (303, "/journal/trades"))
        _, trades = self.get("/journal/trades")
        self.assertIn("Imported trades.csv (Generic format): 5 new executions added", trades)
        for sym in ("AAPL", "TSLA", "KO"):
            self.assertIn(f"<strong>{sym}</strong>", trades)
        self.assertIn("+$48.00", trades)   # AAPL 10 x +5 - 2 fees
        self.assertIn("-$51.00", trades)   # TSLA short 5 x -10 - 1 fee
        _, ov = self.get("/journal")
        self.assertIn("-$3.00", ov)        # net of the two closed trades
        self.assertIn("50.0%", ov)          # 1 win, 1 loss
        self.assertIn("1 open", ov)
        self.assertIn("October 2026", ov)
        self.assertIn("href='/journal/trades?day=2026-10-28'", ov)
        r2 = self.upload("trades.csv", self.CSV)  # same file again: nothing new
        _, again = self.get("/journal/trades")
        self.assertIn("0 new executions added, 5 already in the journal", again)
        r, csv_text = self.get("/journal/export.csv")
        self.assertEqual(r.getheader("Content-Type"), "text/csv; charset=utf-8")
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        self.assertEqual(len(csv_text.strip().splitlines()), 4)
        _, f = self.get("/journal/trades?symbol=aapl")
        self.assertIn("1 trade ", f)
        _, imp = self.get("/journal/import")
        self.assertIn("Import history", imp)

    def test_bad_file_message(self):
        self.upload("x.csv", b"hello,world\n1,2\n")
        _, page = self.get("/journal/import")
        self.assertIn("Couldn&#x27;t find the trade columns", page)

    def test_add_edit_notes_delete_and_escaping(self):
        form = {"symbol": "nvda", "side": "long", "qty": "10", "entry_price": "130", "entry_date": "2026-10-30",
                "entry_time": "09:45", "exit_price": "128", "exit_date": "2026-10-30", "exit_time": "10:15",
                "fees": "", "strategy": "fmfi_scanner", "setup": "Breakout", "tags": "news, gap",
                "mistakes": "", "notes": "<script>alert(1)</script>"}
        r, _ = self.post("/journal/add", form)
        self.assertEqual(r.status, 303)
        loc = r.getheader("Location")
        self.assertTrue(loc.startswith("/journal/trade?id=m"))
        _, page = self.get(loc)
        self.assertIn("Saved the NVDA trade", page)
        self.assertIn("-$20.00", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        tid = loc.split("=", 1)[1]
        r, _ = self.post("/journal/edit", {**form, "id": tid, "exit_price": "135"})
        _, page = self.get(loc)
        self.assertIn("+$50.00", page)
        _, ov = self.get("/journal")
        self.assertIn("By strategy", ov)
        self.assertIn("fmfi_scanner", ov)
        # invalid edit keeps the typed values and explains
        r, body = self.post("/journal/edit", {**form, "id": tid, "exit_date": "2026-10-29"})
        self.assertEqual(r.status, 400)
        self.assertIn("The exit is before the entry", body.decode())
        r, _ = self.post("/journal/delete", {"id": tid})
        self.assertEqual(r.getheader("Location"), "/journal/trades")
        r, _ = self.get(loc)
        self.assertEqual(r.status, 404)

    def test_add_form_errors_and_prefill(self):
        r, body = self.post("/journal/add", {"symbol": "AAPL", "side": "long", "qty": "0", "entry_price": "1",
                                             "entry_date": "2026-10-30", "notes": "keep me"})
        self.assertEqual(r.status, 400)
        page = body.decode()
        self.assertIn("Enter the number of shares", page)
        self.assertIn("keep me", page)
        _, page = self.get("/journal/add?symbol=amd&strategy=fmfi_scanner&price=150.25&date=2026-10-30")
        self.assertIn("value='AMD'", page)
        self.assertIn("value='150.25'", page)
        self.assertIn("value='2026-10-30'", page)
        self.assertIn("From a fmfi_scanner BUY signal", page)
        _, page = self.get("/journal/add?symbol=%27%3E%3Cb%3E&date=x&price=abc")
        self.assertNotIn("'><b>", page)

    def test_imported_trade_notes_and_executions(self):
        self.upload("t.csv", self.CSV)
        tid = next(t["id"] for t in self.srv.app.journal.trades() if t["symbol"] == "AAPL")
        r, _ = self.post("/journal/notes", {"id": tid, "setup": "ORB", "tags": "a, b", "mistakes": "late",
                                            "strategy": "", "notes": "ok"})
        self.assertEqual(r.status, 303)
        _, page = self.get(f"/journal/trade?id={tid}")
        self.assertIn("Executions", page)
        self.assertIn("t.csv · row 2", page)
        self.assertIn("value='ORB'", page)
        _, tr = self.get("/journal/trades?tag=late")
        self.assertIn("<strong>AAPL</strong>", tr)
        self.assertNotIn("<strong>TSLA</strong>", tr)

    def test_safety(self):
        r, _ = self.post("/journal/add", {"symbol": "AAPL"}, origin=False)
        self.assertEqual(r.status, 403)
        r, _ = self.srv.request("GET", "/journal", auth=None)
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))
        for path in ("/journal/trade?id=../../state.json", "/journal/trade?id=m0000000000000000", "/journal/nope"):
            r, _ = self.get(path)
            self.assertEqual(r.status, 404, path)

    def test_clear_needs_a_fresh_code(self):
        label = _REAL_PROTECTED_CHANGE_LABEL("/journal/clear", b"", "application/x-www-form-urlencoded")
        self.assertIn("Delete all journal trades", label)
        self.assertIn("Delete journal trade", _REAL_PROTECTED_CHANGE_LABEL(
            "/journal/delete", b"id=x", "application/x-www-form-urlencoded"))
        for p, d in (("/journal/add", {"symbol": "A"}),
                     ("/journal/notes", {"id": "x"})):
            self.assertIsNone(_REAL_PROTECTED_CHANGE_LABEL(p, urllib.parse.urlencode(d).encode(),
                                                           "application/x-www-form-urlencoded"), p)
        self.upload("t.csv", self.CSV)
        try:
            sv.protected_change_label = _REAL_PROTECTED_CHANGE_LABEL
            r, _ = self.post("/journal/clear", {})
            self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))
            self.assertEqual(len(self.srv.app.journal.trades()), 3)  # nothing deleted
        finally:
            sv.protected_change_label = lambda *a, **k: None
        r, _ = self.post("/journal/clear", {})
        self.assertEqual(len(self.srv.app.journal.trades()), 0)

    def test_log_trade_link_on_buy_rows(self):
        st = self.srv.app.strategies
        rid = "2026-10-30_131000_fmfi_scanner"
        d = st.results_dir / rid
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"strategy": "fmfi_scanner", "status": "ok", "asof": "2026-10-30"}))
        (d / "signals.csv").write_text("Ticker,Status,As_Of,Close,Signal_Dates\n"
                                       "AMD,BUY,2026-10-30,150.25,2026-10-30\nXOM,NO SIGNAL,2026-10-30,110,\n")
        _, page = self.get(f"/strategies/results/{rid}/")
        self.assertIn("class='logt' href='/journal/add?symbol=AMD&amp;strategy=fmfi_scanner&amp;date=2026-10-30"
                      "&amp;price=150.25'", page)
        self.assertEqual(page.count("class='logt'"), 1)  # not on NO SIGNAL rows
        self.assertIn("<a href='/journal'>Journal</a>", page)
