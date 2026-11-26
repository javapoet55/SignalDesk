"""Tests for scheduled strategy runs and the redesigned Strategies tab.  python -m unittest -v test_schedules.py"""
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
from test_strategies import FakeFinviz, FV_TOKEN, V5_FILE, make_scan, wait_idle

PT = ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc


def pt(y, m, d, hh, mm):
    return dt.datetime(y, m, d, hh, mm, tzinfo=PT).astimezone(UTC)


class TestScheduleStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = sc.ScheduleStore(self.tmp / "schedules.json", PT)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_once_in_pdt_and_pst(self):
        now = pt(2026, 10, 26, 20, 0)
        j = self.store.add(["a"], "latest", "close", "once", "2026-10-27", "16:30", now=now)
        self.assertEqual(j["at_utc"], "2026-10-27T23:30:00+00:00")  # PDT = UTC-7
        j2 = self.store.add(["a"], "latest", "close", "once", "2026-11-03", "16:30", now=now)
        self.assertEqual(j2["at_utc"], "2026-11-04T00:30:00+00:00")  # PST = UTC-8 after Nov 1

    def test_validation(self):
        now = pt(2026, 10, 26, 20, 0)
        cases = [(["a"], "once", "2026-10-26", "19:00", "already passed"),
                 (["a"], "once", "2027-06-01", "10:00", "90 days"),
                 (["a"], "once", "", "10:00", "Pick a date"),
                 (["a"], "once", "2026-10-27", "25:00", "time like"),
                 ([], "once", "2026-10-27", "10:00", "at least one"),
                 (["a"], "hourly", "", "10:00", "Unknown schedule"),
                 (["a"], "once", "2027-03-14", "02:30", "doesn't exist")]  # spring-forward gap
        for names, rep, d, t, msg in cases:
            with self.assertRaises(ValueError, msg=(rep, d, t)) as cm:
                self.store.add(names, "latest", "close", rep, d, t, now=now)
            self.assertIn(msg, str(cm.exception))

    def test_add_many_times(self):
        now = pt(2026, 10, 26, 20, 0)
        jobs = self.store.add_many(["a"], "fresh", "close", "weekdays", "", ["13:10", "", "06:45", "13:10", "9:05"],
                                   now=now)
        self.assertEqual([j["time"] for j in jobs], ["06:45", "09:05", "13:10"])  # sorted, blanks/repeats dropped
        self.assertEqual(len({j["id"] for j in jobs}), 3)
        self.assertEqual([j["time"] for j in self.store.jobs()], ["06:45", "09:05", "13:10"])
        # each time fires on its own, once per weekday
        self.assertEqual([j["time"] for j, _ in self.store.due(pt(2026, 10, 27, 6, 46))], ["06:45"])
        self.assertEqual([j["time"] for j, _ in self.store.due(pt(2026, 10, 27, 9, 6))], ["09:05"])
        self.assertEqual([j["time"] for j, _ in self.store.due(pt(2026, 10, 27, 13, 11))], ["13:10"])
        self.assertEqual(self.store.due(pt(2026, 10, 27, 13, 30)), [])

    def test_add_many_all_or_nothing(self):
        now = pt(2026, 10, 26, 20, 0)
        cases = [(["10:00", "25:00"], "once", "2026-10-27", "time like"),
                 (["21:00", "19:00"], "once", "2026-10-26", "19:00: That time has already passed"),
                 (["", " "], "weekdays", "", "Pick a time"),
                 (["%02d:00" % h for h in range(13)], "weekdays", "", "at most 12")]
        for times, rep, d, msg in cases:
            with self.assertRaises(ValueError, msg=times) as cm:
                self.store.add_many(["a"], "latest", "close", rep, d, times, now=now)
            self.assertIn(msg, str(cm.exception))
            self.assertEqual(self.store.jobs(), [], times)  # nothing saved
        once = self.store.add_many(["a"], "latest", "close", "once", "2026-10-27", ["16:30", "09:00"], now=now)
        self.assertEqual([j["at_utc"] for j in once], ["2026-10-27T16:00:00+00:00", "2026-10-27T23:30:00+00:00"])
        # scans keep refusing a duplicate slot, and a refused batch adds nothing
        self.store.add([], "", "", "weekdays", "", "12:00", now=now, kind="scan")
        with self.assertRaises(ValueError):
            self.store.add_many([], "", "", "weekdays", "", ["11:00", "12:00"], now=now, kind="scan")
        self.assertEqual(len(self.store.jobs("scan")), 1)

    def test_once_fires_exactly_once(self):
        now = pt(2026, 10, 26, 20, 0)
        self.store.add(["a", "b"], "latest", "close", "once", "2026-10-27", "16:30", now=now)
        self.assertEqual(self.store.due(pt(2026, 10, 27, 16, 29)), [])
        fired = self.store.due(pt(2026, 10, 27, 16, 30))
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["names"], ["a", "b"])
        self.assertEqual(self.store.due(pt(2026, 10, 27, 16, 31)), [])
        # a restarted process reads the same file: still nothing to fire
        again = sc.ScheduleStore(self.tmp / "schedules.json", PT)
        self.assertEqual(again.due(pt(2026, 10, 27, 16, 45)), [])
        self.assertEqual(again.jobs()[0]["status"], "done")

    def test_weekdays_skip_weekend_and_follow_clock_change(self):
        created = pt(2026, 10, 29, 20, 0)  # Thu evening
        self.store.add(["a"], "fresh", "close", "weekdays", "", "06:45", now=created)
        fires = []
        t = created
        while t < pt(2026, 11, 4, 12, 0):
            t += dt.timedelta(minutes=5)
            for job, when in self.store.due(t):
                fires.append(when.astimezone(PT).strftime("%a %m/%d %H:%M %Z"))
        self.assertEqual(fires, ["Fri 10/30 06:45 PDT", "Mon 11/02 06:45 PST", "Tue 11/03 06:45 PST",
                                 "Wed 11/04 06:45 PST"])

    def test_missed_while_offline(self):
        self.store.add(["a"], "latest", "close", "once", "2026-10-27", "09:00", now=pt(2026, 10, 26, 20, 0))
        self.store.add(["b"], "latest", "close", "weekdays", "", "09:00", now=pt(2026, 10, 26, 20, 0))
        # back 10 minutes late: both still run (within the 30-minute grace)
        fired = self.store.due(pt(2026, 10, 27, 9, 10))
        self.assertEqual(sorted(j["names"][0] for j, _ in fired), ["a", "b"])
        # weekday job: server offline Wed and Thu 9:00, back Thu 11:00 -> both marked missed, next is Fri
        self.assertEqual(self.store.due(pt(2026, 10, 29, 11, 0)), [])
        wk = [j for j in self.store.jobs() if j["repeat"] == "weekdays"][0]
        self.assertEqual([h["result"] for h in wk["history"]][-2:], ["missed", "missed"])
        self.assertEqual(self.store.next_fire(wk), pt(2026, 10, 30, 9, 0))
        # one-time job missed entirely
        self.store.add(["c"], "latest", "close", "once", "2026-10-30", "10:00", now=pt(2026, 10, 29, 11, 0))
        # Fri 12:00: c (10:00) is 2 h late and b (09:00) 3 h late -> both missed, nothing fires
        self.assertEqual(self.store.due(pt(2026, 10, 30, 12, 0)), [])
        c = [j for j in self.store.jobs() if j["names"] == ["c"]][0]
        self.assertEqual(c["status"], "missed")

    def test_cancel(self):
        j = self.store.add(["a"], "latest", "close", "once", "2026-10-27", "16:30", now=pt(2026, 10, 26, 20, 0))
        self.assertEqual(self.store.cancel(j["id"])["status"], "cancelled")
        self.assertEqual(self.store.due(pt(2026, 10, 27, 16, 30)), [])
        with self.assertRaises(ValueError):
            self.store.cancel(j["id"])


class TestSchedulesWeb(unittest.TestCase):
    """The real server + fake FinViz; scans run in --offline mode against the fake's tickers."""

    @classmethod
    def setUpClass(cls):
        cls.fake = FakeFinviz()
        cls.vol = Path(tempfile.mkdtemp())
        cls.csv_dir = Path(tempfile.mkdtemp())
        from test_finviz_agent import make_csv, random_stocks
        (cls.csv_dir / "top-gainers.csv").write_text(make_csv([(f"S{i:03d}", 5.0 + i % 7, 1.0, 50.0)
                                                               for i in range(30)]))
        env = {"DATA_DIR": str(cls.vol), "DASHBOARD_PASSWORD": "pw", "FINVIZ_API_TOKEN": FV_TOKEN,
               "FINVIZ_BASE_URL": cls.fake.url, "FINVIZ_PRICE_INTERVAL": "0", "SCHEDULE_TIMES": "off",
               "FINVIZ_AGENT_EXTRA_ARGS": f"--offline {cls.csv_dir}", "STRATEGY_TIMEOUT_SEC": "60",
               "SCREENS_JSON": json.dumps({"top_n": 10, "builtin_signals": {"include": ["Top Gainers"]}})}
        cls.app = sv.build_app(env, log=lambda m: None)
        cls.app.strategies.finviz.client._sleep = lambda s: None
        make_scan(cls.app.settings.reports_dir, "2026-10-25_120003", ["S%03d" % i for i in range(40)])
        cls.app.strategies.upload_strategy("macdgen_v5_regime_backtest.py", V5_FILE.read_bytes())
        cls.app.strategies.upload_strategy(
            "list_tickers.py", b"def finviz_signals(t, a, c):\n    return [{'Ticker': x} for x in t]\n")
        cls.httpd = sv.ThreadingHTTPServer(("127.0.0.1", 0), sv.Handler)
        cls.httpd.app = cls.app
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.fake.close()
        shutil.rmtree(cls.vol, ignore_errors=True); shutil.rmtree(cls.csv_dir, ignore_errors=True)

    def req(self, method, path, form=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode(),
             "Origin": f"http://127.0.0.1:{self.port}"}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form, doseq=True)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse(); data = r.read(); conn.close()
        return r, data.decode()

    def page(self):
        return self.req("GET", "/strategies")[1]

    def test_1_page_layout(self):
        page = self.page()
        for text in ("Ticker source", "Price basis", "New scan at run time", "Select Strategies", "Select All",
                     "2 of 2 selected", "Run Schedule", "Custom time (one-time)", "Schedule (recurring)", "Run activity",
                     "Run selected strategies after each new scan", "Upload Strategy", "Data &amp; configuration",
                     "Macdgen V5 regime backtest", "macdgen_v5_regime_backtest.py", "V5-style", "Custom",
                     "Scan schedule: None scheduled"):
            self.assertIn(text, page, text)
        dash = self.req("GET", "/")[1]
        self.assertIn("Next scan</div><div class='v'>None scheduled", dash)

    def test_2_run_now_selected_only(self):
        r, _ = self.req("POST", "/strategies/go", {"s": ["list_tickers"], "scan": "2026-10-25_120003",
                                                    "mode": "close", "when": "now"})
        self.assertEqual(r.getheader("Location"), "/strategies#activity")
        self.assertIn("1 strategy queued.", self.page())
        wait_idle(self.app.strategies)
        res = self.app.strategies.results()[0]
        self.assertEqual((res["strategy"], res["status"], res["scan"]), ("list_tickers", "ok", "2026-10-25_120003"))
        self.assertIn("1 of 2 selected", self.page())  # selection remembered

    def test_3_schedule_once_shows_and_cancels(self):
        tomorrow = (dt.datetime.now(PT) + dt.timedelta(days=1)).strftime("%Y-%m-%d")
        r, _ = self.req("POST", "/strategies/go", {"s": ["list_tickers", "macdgen_v5_regime_backtest"],
                                                    "scan": "latest", "mode": "close", "when": "once",
                                                    "date": tomorrow, "time": "16:30"})
        page = self.page()
        self.assertIn("Scheduled: 2 strategies", page)
        self.assertIn("16:30 P", page)
        job = [j for j in self.app.schedules.jobs() if j["status"] == "scheduled"][0]
        self.req("POST", "/strategies/schedule/cancel", {"id": job["id"]})
        self.assertIn("Schedule cancelled.", self.page())
        self.assertFalse([j for j in self.app.schedules.jobs() if j["status"] == "scheduled"])

    def test_4_bad_inputs(self):
        cases = [({"when": "now"}, "Select at least one strategy"),
                 ({"s": ["list_tickers"], "when": "once", "date": "2020-01-01", "time": "10:00"}, "already passed"),
                 ({"s": ["list_tickers"], "when": "now", "scan": "../etc"}, "Unknown ticker source"),
                 ({"s": ["nope"], "when": "now"}, "Select at least one strategy")]
        for form, msg in cases:
            form = {"scan": "latest", "mode": "close", **form}
            self.req("POST", "/strategies/go", form)
            self.assertIn(msg, self.page(), form)

    def test_5_scheduled_fresh_scan_then_strategies(self):
        """A due 'new scan at run time' job runs a scan, then the strategies on that scan's tickers."""
        app = self.app
        now = dt.datetime.now(UTC)
        job = app.schedules.add(["list_tickers"], "fresh", "close", "once",
                                (now + dt.timedelta(minutes=5)).astimezone(PT).strftime("%Y-%m-%d"),
                                (now + dt.timedelta(minutes=5)).astimezone(PT).strftime("%H:%M"), now=now)
        before = {r.stamp for r in sv.list_runs(app.settings)}
        n = sv.schedule_tick(app, now=now + dt.timedelta(minutes=6), log=lambda m: None, background=False)
        self.assertEqual(n, 1)
        wait_idle(app.strategies)
        new_scans = [r for r in sv.list_runs(app.settings) if r.stamp not in before]
        self.assertEqual(len(new_scans), 1)
        res = [r for r in app.strategies.results() if r.get("reason") == "scheduled"][0]
        self.assertEqual((res["status"], res["scan"]), ("ok", new_scans[0].stamp))
        hist = [j for j in app.schedules.jobs() if j["id"] == job["id"]][0]["history"]
        self.assertIn("queued 1 of 1", hist[-1]["result"])

    def test_7_same_strategy_twice_in_one_second(self):
        st = self.app.strategies
        before = len(st.results())
        st.enqueue("list_tickers", "2026-10-25_120003", "close", "manual")
        st.enqueue("list_tickers", "2026-10-25_120003", "intraday", "manual")  # different basis: both run
        wait_idle(st)
        new = st.results()[: len(st.results()) - before]
        self.assertEqual(len(new), 2)
        self.assertEqual({r["status"] for r in new}, {"ok"})
        self.assertEqual(len({r["id"] for r in new}), 2)

    def test_8_several_times_from_the_page(self):
        page = self.page()
        self.assertIn("<div id='tlist'>", page)
        self.assertIn("id='addtime'", page)
        before = {j["id"] for j in self.app.schedules.jobs()}
        r, _ = self.req("POST", "/strategies/go", {"s": ["list_tickers"], "scan": "fresh", "mode": "close",
                                                    "when": "weekdays", "time": ["13:10", "06:45", "", "13:10"]})
        self.assertEqual(r.getheader("Location"), "/strategies#activity")
        new = [j for j in self.app.schedules.jobs() if j["id"] not in before]
        self.assertEqual(sorted(j["time"] for j in new), ["06:45", "13:10"])
        page = self.page()
        self.assertIn("Scheduled 2 runs: 1 strategy · every weekday at 06:45, 13:10 P", page)
        self.assertEqual(sum(page.count(f"value='{j['id']}'") for j in new), 2)  # each has its own Cancel
        self.req("POST", "/strategies/schedule/cancel", {"id": new[0]["id"]})
        left = [j for j in self.app.schedules.jobs() if j["id"] in {x["id"] for x in new} and j["status"] == "scheduled"]
        self.assertEqual(len(left), 1)
        self.req("POST", "/strategies/schedule/cancel", {"id": left[0]["id"]})
        before = len(self.app.schedules.jobs())
        self.req("POST", "/strategies/go", {"s": ["list_tickers"], "scan": "latest", "mode": "close",
                                            "when": "weekdays", "time": ["10:00", "99:99"]})
        self.assertIn("Enter a time like 16:30", self.page())
        self.assertEqual(len(self.app.schedules.jobs()), before)

    def test_6_auto_set_after_scan_and_dedupe(self):
        self.req("POST", "/strategies/autoset", {"s": ["list_tickers"], "auto": "on", "scan": "latest",
                                                  "mode": "close", "when": "now"})
        page = self.page()
        self.assertIn("After each new scan, 1 strategy will run", page)
        self.assertIn("After each new scan: List tickers", page)
        st = self.app.strategies
        self.assertEqual(st.auto_config()["names"], ["list_tickers"])
        # a scheduled 'fresh' job for the same strategy: auto-run + job must not run it twice on one scan
        before = len(st.results())
        stamp, why = sv.run_fresh_scan(self.app)
        self.assertIsNotNone(stamp, why)
        st.enqueue_many(["list_tickers"], stamp, "close", "scheduled")
        wait_idle(st)
        on_that_scan = [r for r in st.results() if r.get("scan") == stamp]
        self.assertEqual(len(on_that_scan), 1)
        self.assertEqual(len(st.results()), before + 1)
        self.req("POST", "/strategies/autoset", {"s": ["list_tickers"], "scan": "latest", "mode": "close"})
        self.assertEqual(st.auto_config()["names"], [])


if __name__ == "__main__":
    unittest.main()
