"""Tests for the Strategies tab: Massive price data, sandboxed strategy runs, and the web pages.

A fake Massive API (same paths and response shapes as the official client uses) serves a
synthetic market. The V5 strategy used is the user's real macdgen_v5_regime_backtest.py.
Run: python -m unittest -v test_strategies.py
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import http.client
import http.server
import io
import json
import os
import re
import math
import random
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

import macdgen_v5_finviz as v5
import prices as px
import server as sv

# The live server asks for a fresh emailed code before configuration changes (change_guard).
# Feature tests below exercise what each change does, so they skip that step; TestChangeGuard checks the
# real rule separately.
import server as _sv_guard  # noqa: E402
_REAL_PROTECTED_CHANGE_LABEL = getattr(_sv_guard, '_real_protected_change_label',
                                       _sv_guard.protected_change_label)
_sv_guard._real_protected_change_label = _REAL_PROTECTED_CHANGE_LABEL
_sv_guard.protected_change_label = lambda *a, **k: None
import strategies as sm

HERE = Path(__file__).resolve().parent
V5_FILE = HERE / "testdata" / "macdgen_v5_regime_backtest.py"
KEY = "mk_TEST_KEY_123/+="
UTC = dt.timezone.utc


# ── synthetic market ──────────────────────────────────────────────────────────
def build_market(seed=7, n=120, end=dt.date(2026, 10, 23)):
    rng = random.Random(seed)
    days, d = [], dt.date(2025, 12, 1)
    while d <= end:
        if d.weekday() < 5 and d != dt.date(2026, 9, 7):  # Labor Day closed
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    market = {day: {} for day in days}
    syms = ["SPY", "BRK.B"] + [f"S{i:03d}" for i in range(n)]
    for sym in syms:
        p = 500.0 if sym == "SPY" else 450.0 if sym == "BRK.B" else rng.uniform(3, 150)
        vol = 0.006 if sym == "SPY" else 0.012 if sym == "BRK.B" else rng.uniform(0.015, 0.05)
        base_v = 7e7 if sym == "SPY" else rng.choice([200_000, 800_000, 3_000_000])
        for day in days:
            r = rng.gauss(0.0012 if sym == "SPY" else 0.0008, vol)
            if sym != "SPY" and rng.random() < 0.05:
                r += rng.uniform(0.03, 0.12)
            o, p = p, max(0.5, p * (1 + r))
            market[day][sym] = {"o": round(o, 4), "h": round(max(o, p) * (1 + abs(rng.gauss(0, vol / 2))), 4),
                                "l": round(min(o, p) * (1 - abs(rng.gauss(0, vol / 2))), 4), "c": round(p, 4),
                                "v": int(base_v * rng.uniform(0.5, 1.8) * (2.5 if r > 0.04 else 1))}
    return days, syms, market


DAYS, SYMS, MARKET = build_market()


class FakeMassive:
    """Serves /v2/aggs/grouped/... and /v2/snapshot/... like api.massive.com."""

    def __init__(self):
        self.requests = []
        self.fail_429_once = False
        self.snapshot_day = None  # dict sym -> bar for "today"
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    return self._json(401, {"status": "ERROR", "request_id": "x", "error": "Unknown API Key"})
                if outer.fail_429_once:
                    outer.fail_429_once = False
                    self.send_response(429); self.send_header("Retry-After", "0"); self.end_headers()
                    return
                parts = urllib.parse.urlsplit(self.path)
                m = parts.path.split("/")
                if parts.path.startswith("/v2/aggs/grouped/locale/us/market/stocks/"):
                    day = m[-1]
                    q = urllib.parse.parse_qs(parts.query)
                    assert q.get("adjusted") == ["true"]
                    bars = MARKET.get(day, {})
                    results = [{"T": s, **b, "t": 0} for s, b in bars.items()]
                    return self._json(200, {"status": "OK", "resultsCount": len(results), "results": results}
                                      if results else {"status": "OK", "resultsCount": 0})
                if parts.path == "/v2/snapshot/locale/us/markets/stocks/tickers":
                    want = urllib.parse.parse_qs(parts.query).get("tickers", [""])[0].split(",")
                    snap = outer.snapshot_day or {}
                    return self._json(200, {"status": "OK", "tickers": [
                        {"ticker": s, "day": snap[s], "prevDay": {}} for s in want if s in snap]})
                return self._json(404, {"status": "NOT_FOUND"})

            def _json(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


FRIDAY_EVENING = dt.datetime(2026, 10, 24, 1, 0, tzinfo=UTC)  # Fri 10/23 21:00 ET -> Friday bar published


def client(fake, **kw):
    return px.MassiveClient(KEY, base_url=fake.url, min_interval=0, sleep=lambda s: None, **kw)


class TestPrices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeMassive()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake.requests.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backfill_holiday_and_incremental(self):
        store = px.PriceStore(self.tmp, history_days=60)
        res = store.update(client(self.fake), now_utc=FRIDAY_EVENING)
        self.assertEqual(len(self.fake.requests), 60)
        stored = store.trading_days()
        self.assertEqual(stored[-1], "2026-10-23")
        self.assertNotIn("2026-09-07", stored)  # holiday: empty, remembered
        self.assertEqual(json.loads((self.tmp / "fetched.json").read_text())["2026-09-07"], 0)
        self.assertEqual(len(stored), 59)
        self.fake.requests.clear()
        store.update(client(self.fake), now_utc=FRIDAY_EVENING)
        self.assertEqual(self.fake.requests, [])  # nothing refetched, holiday included
        # Monday after the close: exactly one new day requested
        store.update(client(self.fake), now_utc=dt.datetime(2026, 10, 26, 22, 0, tzinfo=UTC))
        self.assertEqual(len(self.fake.requests), 1)
        self.assertIn("2026-10-26", self.fake.requests[0])

    def test_latest_session_not_published_yet_is_retried(self):
        store = px.PriceStore(self.tmp, history_days=40)
        # Monday 10/26 evening: market data has no 10/26 bar yet -> empty, must NOT be marked as holiday
        store.update(client(self.fake), now_utc=dt.datetime(2026, 10, 26, 22, 0, tzinfo=UTC))
        idx = json.loads((self.tmp / "fetched.json").read_text())
        self.assertNotIn("2026-10-26", idx)

    def test_rate_limit_retry_and_auth_error_redacted(self):
        self.fake.fail_429_once = True
        rows = client(self.fake).grouped_daily("2026-10-23")
        self.assertEqual(len(rows), len(SYMS))
        bad = px.MassiveClient(KEY + "x", base_url=self.fake.url, min_interval=0, sleep=lambda s: None)
        with self.assertRaises(px.PriceAuthError) as cm:
            bad.grouped_daily("2026-10-23")
        self.assertIn("Unknown API Key", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))
        with self.assertRaises(px.PriceAuthError):
            px.MassiveClient("")
        self.assertIn("not recognised", str(cm.exception))
        # common paste mistakes are undone; the key still works
        for pasted in (f' "{KEY}" ', f"Bearer {KEY}", f"apiKey={KEY}\n", f"'{KEY}'"):
            c = px.MassiveClient(pasted, base_url=self.fake.url, min_interval=0, sleep=lambda s: None)
            self.assertEqual(c.api_key, KEY)
            self.assertEqual(len(c.grouped_daily("2026-10-23")), len(SYMS))
        ok = client(self.fake).test(now_utc=FRIDAY_EVENING)
        self.assertTrue(ok.startswith("✅ Key works"), ok)
        self.assertIn(f"ending …{KEY[-4:]}", ok)
        self.assertNotIn(KEY, ok)
        badmsg = bad.test(now_utc=FRIDAY_EVENING)
        self.assertTrue(badmsg.startswith("❌") and "Unknown API Key" in badmsg and KEY not in badmsg, badmsg)

    def test_build_cache_subset_format_and_intraday(self):
        store = px.PriceStore(self.tmp, history_days=60)
        c = client(self.fake)
        store.update(c, now_utc=FRIDAY_EVENING)
        cache, info = store.build_cache(["S001", "BRK-B", "NOPE"])
        self.assertEqual(set(cache["tickers"]), {"S001", "BRK.B", "SPY"})
        day = "2026-10-23"
        self.assertEqual(cache["history"][day]["S001"], {k: float(v) for k, v in MARKET[day]["S001"].items()})
        self.assertIsNone(info["intraday"])
        # Monday 12:00 PT, market open: today's bar so far comes from the snapshot
        self.fake.snapshot_day = {"S001": {"o": 10, "h": 11, "l": 9.5, "c": 10.8, "v": 123456},
                                  "SPY": {"o": 700, "h": 705, "l": 699, "c": 704, "v": 1e7}}
        noon = dt.datetime(2026, 10, 26, 19, 0, tzinfo=UTC)
        cache2, info2 = store.build_cache(["S001"], include_intraday=True, client=c, now_utc=noon)
        self.assertEqual(cache2["history"]["2026-10-26"]["S001"]["c"], 10.8)
        self.assertIn("as of 15:00 ET", info2["intraday"])
        self.assertEqual(info2["last_day"], "2026-10-26")
        # weekend: no intraday bar
        _, info3 = store.build_cache(["S001"], include_intraday=True, client=c,
                                     now_utc=dt.datetime(2026, 10, 25, 19, 0, tzinfo=UTC))
        self.assertIn("weekend", info3["intraday"])


FINVIZ_SPY = HERE / "testdata" / "finviz_spy"  # synthetic FinViz-format fixtures for SPY (Oct 29-30, 2026)


def intraday_csv(sym: str, period: str):
    """SPY's synthetic intraday fixture; other tickers get the same timestamps with prices and volume reshaped
    (deterministically per ticker) so their money flow differs."""
    path = FINVIZ_SPY / f"SPY_{period}.csv"
    if not path.is_file() or not (sym == "SPY" or re.fullmatch(r"S\d{3}", sym)):
        return None
    lines = path.read_text().splitlines()
    if sym == "SPY":
        return "\n".join(lines) + "\n"
    k = int(sym[1:]) + 1
    out = [lines[0]]
    for i, ln in enumerate(lines[1:]):
        t, o, h, l, c, v = ln.split(",")
        f = (1 + 0.01 * k) * (1 + 0.004 * math.sin(i * 0.37 * k))
        out.append(f"{t},{float(o) * f:.4f},{float(h) * f * 1.001:.4f},{float(l) * f * 0.999:.4f},{float(c) * f:.4f},"
                   f"{int(float(v) * (1 + (i * k) % 7))}")
    return "\n".join(out) + "\n"


def make_scan(reports_dir: Path, stamp: str, tickers, run_line="Run: 2026-10-25 12:00 PDT"):
    d = reports_dir / stamp
    d.mkdir(parents=True)
    (d / "tickers.txt").write_text(f"{run_line}\n{','.join(tickers)}\n")
    (d / "report.html").write_text("<!doctype html><html><head></head><body><main>REPORT BODY</main></body></html>")
    return d


def wait_idle(st: sm.StrategyManager, timeout=180):
    t0 = time.time()
    time.sleep(0.2)
    while (st.current or st.queued or not st._jobs.empty()) and time.time() - t0 < timeout:
        time.sleep(0.1)


class TestStrategyRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeMassive()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = Path(tempfile.mkdtemp())  # private (0700) volume root, like an unknown Railway volume
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        self.reports = self.data / "reports"
        self.reports.mkdir(parents=True)
        env = {"MASSIVE_API_KEY": KEY, "MASSIVE_BASE_URL": self.fake.url, "MASSIVE_MIN_INTERVAL": "0",
               "PRICE_HISTORY_DAYS": "120", "STRATEGY_TIMEOUT_SEC": "60"}
        self.st = sm.StrategyManager(self.data, self.reports, sv.ZoneInfo("America/Los_Angeles"), env,
                                     log=lambda m: None)
        self.st.client._sleep = lambda s: None
        self.st.prices.update(self.st.client, now_utc=FRIDAY_EVENING)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def test_private_volume_is_opened_only_for_pass_through(self):
        self.assertIsNone(self.st.sandbox_error)
        if os.geteuid() == 0:
            self.assertEqual(self.data.stat().st_mode & 0o777, 0o701)  # others may pass through, not list

    def upload(self, name, text):
        return self.st.upload_strategy(name, text.encode())

    def test_upload_validation(self):
        self.assertIn("V5-style", self.st.upload_strategy("macdgen_v5.py", V5_FILE.read_bytes()))
        self.assertIn("Updated", self.st.upload_strategy("macdgen_v5.py", V5_FILE.read_bytes()))
        self.assertIn("custom", self.upload("mine.py", "def finviz_signals(t, a, c):\n    return []\n"))
        cases = [("x.txt", "print(1)", "ending in .py"), ("bad.py", "def x(:\n", "syntax error on line 1"),
                 ("other.py", "x = 1\n", "not a supported strategy"),
                 ("boom.py", "raise RuntimeError('nope')\n", "Could not load the file"),
                 ("empty.py", "", "empty")]
        for fname, text, expect in cases:
            with self.assertRaises(ValueError, msg=fname) as cm:
                self.upload(fname, text)
            self.assertIn(expect, str(cm.exception))
        with self.assertRaises(ValueError):
            self.st.upload_strategy("big.py", b"#" * (sm.MAX_SCRIPT_BYTES + 1))
        self.assertEqual([n for n, _ in self.st.list()], ["macdgen_v5", "mine"])
        self.assertFalse(list(self.st.scripts_dir.glob(".upload_*")))  # no temp files left behind

    def test_v5_run_matches_the_verified_adapter_exactly(self):
        tickers = ["S%03d" % i for i in range(120)] + ["BRK-B", "NOPE"]
        make_scan(self.reports, "2026-10-25_120003", tickers)
        self.st.upload_strategy("macdgen_v5.py", V5_FILE.read_bytes())
        self.st.enqueue("macdgen_v5", "latest", "close", "manual")
        wait_idle(self.st)
        res = self.st.results()[0]
        self.assertEqual(res["status"], "ok", (self.st.results_dir / res["id"] / "output.txt").read_text())
        self.assertEqual(res["asof"], "2026-10-23")  # Sunday scan -> Friday close
        out = self.st.results_dir / res["id"]
        got = list(csv.DictReader((out / "signals.csv").open()))
        # Reference: the adapter (verified against the original script) run directly on the same prices
        ref_dir = self.tmp / "ref"; ref_dir.mkdir()
        with open(os.devnull, "w") as dn:
            import contextlib
            with contextlib.redirect_stdout(dn):
                v5.main(["--strategy", str(V5_FILE), "--tickers", str(out / "tickers.txt"), "--cache",
                         str(out / "history_cache.json"), "--out", str(ref_dir), "--asof", "2026-10-23",
                         "--no-mcap-filter"])
        with next(ref_dir.glob("*.csv")).open() as fh:
            ref = list(csv.DictReader(fh))
        self.assertEqual(got, ref)
        self.assertGreater(res["counts"].get("BUY SIGNAL", 0) + res["counts"].get("NO SIGNAL", 0), 100)
        # And the same decisions as running on the FULL market history (not just the stored window)
        full = {"tickers": SYMS, "history": {d: {s: {k: float(v) for k, v in b.items()} for s, b in bars.items()}
                                             for d, bars in MARKET.items()}}
        (self.tmp / "full.json").write_text(json.dumps(full))
        ref2 = self.tmp / "ref2"; ref2.mkdir()
        with open(os.devnull, "w") as dn:
            import contextlib
            with contextlib.redirect_stdout(dn):
                v5.main(["--strategy", str(V5_FILE), "--tickers", str(out / "tickers.txt"), "--cache",
                         str(self.tmp / "full.json"), "--out", str(ref2), "--asof", "2026-10-23", "--no-mcap-filter"])
        with next(ref2.glob("*.csv")).open() as fh:
            full_rows = list(csv.DictReader(fh))
        self.assertEqual([(r["Ticker"], r["Status"]) for r in got], [(r["Ticker"], r["Status"]) for r in full_rows])
        self.assertEqual(json.loads((out / "meta.json").read_text())["mcap_rule"], False)

    def test_mcap_list_is_applied(self):
        make_scan(self.reports, "2026-10-25_120003", ["S001", "S002", "S003"])
        self.st.upload_mcap(b"Ticker,MarketCap\nS001,1e9\nS002,2e9\nSPY,1\n")
        self.st.upload_strategy("v5.py", V5_FILE.read_bytes())
        self.st.enqueue("v5", "latest", "close", "manual"); wait_idle(self.st)
        out = self.st.results_dir / self.st.results()[0]["id"]
        rows = {r["Ticker"]: r["Status"] for r in csv.DictReader((out / "signals.csv").open())}
        self.assertEqual(rows["S003"], "EXCLUDED")
        with self.assertRaises(ValueError):
            self.st.upload_mcap(b"Symbol\nAAPL\n")

    def test_sandbox_has_no_secrets_and_limited_rights(self):
        os.environ["FINVIZ_API_TOKEN"] = "tok_SERVER_SECRET"
        os.environ["MASSIVE_API_KEY"] = KEY
        try:
            probe = (
                "import os, pathlib\n"
                "def finviz_signals(tickers, asof, cache):\n"
                "    rows = [{'Ticker': 'ENV', 'Value': ','.join(sorted(os.environ))}, {'Ticker': 'UID', 'Value': str(os.getuid())}]\n"
                "    for label, path in (('PROC1', '/proc/1/environ'), ('PARENT', f'/proc/{os.getppid()}/environ')):\n"
                "        try:\n"
                "            rows.append({'Ticker': label, 'Value': 'READ:' + str(len(pathlib.Path(path).read_bytes()))})\n"
                "        except Exception as e:\n"
                "            rows.append({'Ticker': label, 'Value': type(e).__name__})\n"
                f"    target = pathlib.Path({str(self.data / 'strategies' / 'strategies.json')!r})\n"
                "    try:\n"
                "        target.write_text('hacked'); rows.append({'Ticker': 'WRITE', 'Value': 'WROTE'})\n"
                "    except Exception as e:\n"
                "        rows.append({'Ticker': 'WRITE', 'Value': type(e).__name__})\n"
                "    return rows\n")
            make_scan(self.reports, "2026-10-25_120003", ["S001"])
            self.upload("probe.py", probe)
            self.st.enqueue("probe", "latest", "close", "manual"); wait_idle(self.st)
            out = self.st.results_dir / self.st.results()[0]["id"]
            rows = {r["Ticker"]: r["Value"] for r in csv.DictReader((out / "signals.csv").open())}
            self.assertNotIn("FINVIZ", rows["ENV"]); self.assertNotIn("MASSIVE", rows["ENV"])
            self.assertNotIn("DASHBOARD", rows["ENV"])
            if os.geteuid() == 0:
                self.assertEqual(rows["UID"], str(sm.NOBODY))
                self.assertFalse(rows["PROC1"].startswith("READ"), rows["PROC1"])
                self.assertFalse(rows["PARENT"].startswith("READ"), rows["PARENT"])
                self.assertNotEqual(rows["WRITE"], "WROTE")
                self.assertIn("strategies", json.loads((self.data / "strategies" / "strategies.json").read_text()))
        finally:
            os.environ.pop("FINVIZ_API_TOKEN", None); os.environ.pop("MASSIVE_API_KEY", None)

    def test_timeout_and_crash_are_reported(self):
        make_scan(self.reports, "2026-10-25_120003", ["S001"])
        self.st.timeout = 3
        self.upload("loop.py", "def finviz_signals(t, a, c):\n    while True:\n        pass\n")
        self.upload("crash.py", "def finviz_signals(t, a, c):\n    return 1/0\n")
        self.st.enqueue("loop", "latest", "close", "manual")
        self.st.enqueue("crash", "latest", "close", "manual")
        wait_idle(self.st, 60)
        res = {r["strategy"]: r for r in self.st.results()}
        self.assertEqual(res["loop"]["status"], "error"); self.assertEqual(res["loop"]["exit_code"], -1)
        self.assertIn("ran longer than", (self.st.results_dir / res["loop"]["id"] / "output.txt").read_text())
        self.assertEqual(res["crash"]["status"], "error")
        self.assertIn("ZeroDivisionError", (self.st.results_dir / res["crash"]["id"] / "output.txt").read_text())

    def test_auto_run_after_scan_and_intraday_on_old_scan(self):
        make_scan(self.reports, "2026-10-20_120003", ["S001", "S002"])
        self.upload("auto.py", "def finviz_signals(t, a, c):\n    return [{'Ticker': x, 'AsOf': a} for x in t]\n")
        self.st.set_auto("auto", True, "intraday")
        self.assertEqual(self.st.enqueue_auto("2026-10-20_120003"), 1)
        wait_idle(self.st)
        r = self.st.results()[0]
        self.assertEqual((r["status"], r["scan"], r["asof"], r["reason"]), ("ok", "2026-10-20_120003", "2026-10-20",
                                                                            "after scan"))
        self.assertIn("only applies to today's scan", (self.st.results_dir / r["id"] / "output.txt").read_text())


# ── web pages ────────────────────────────────────────────────────────────────
class TestStrategiesWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeMassive()
        cls.tmp = Path(tempfile.mkdtemp())  # private volume root
        env = {"DATA_DIR": str(cls.tmp), "DASHBOARD_PASSWORD": "pw", "FINVIZ_API_TOKEN": "x",
               "PRICE_SOURCE": "massive", "MASSIVE_API_KEY": KEY, "MASSIVE_BASE_URL": cls.fake.url, "MASSIVE_MIN_INTERVAL": "0",
               "PRICE_HISTORY_DAYS": "60", "STRATEGY_TIMEOUT_SEC": "60",
               "SCREENS_JSON": json.dumps({"builtin_signals": {"include": ["Top Gainers"]}})}
        cls.app = sv.build_app(env, log=lambda m: None)
        cls.app.strategies.client._sleep = lambda s: None
        cls.app.strategies.prices.update(cls.app.strategies.client, now_utc=FRIDAY_EVENING)
        make_scan(cls.app.settings.reports_dir, "2026-10-25_120003", ["S%03d" % i for i in range(40)])
        (cls.app.settings.reports_dir / "latest_report.html").write_text(
            "<!doctype html><html><body><main>LATEST</main></body></html>")
        cls.httpd = sv.ThreadingHTTPServer(("127.0.0.1", 0), sv.Handler)
        cls.httpd.app = cls.app
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.fake.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, ctype=None, auth=True, origin=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {}
        if auth:
            h["Authorization"] = "Basic " + base64.b64encode(b"admin:pw").decode()
        if origin:
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        if ctype:
            h["Content-Type"] = ctype
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse(); data = r.read(); conn.close()
        return r, data

    def upload(self, path, filename, content):
        boundary = "----testboundary7"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.req("POST", path, body, f"multipart/form-data; boundary={boundary}")

    def form(self, path, data):
        return self.req("POST", path, urllib.parse.urlencode(data), "application/x-www-form-urlencoded")

    def test_1_tabs_auth_and_report_nav(self):
        r, body = self.req("GET", "/strategies", auth=False)
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))
        r, body = self.req("GET", "/strategies")
        self.assertEqual(r.status, 200)
        page = body.decode()
        for text in ("aria-label='Portal modules'", "Your strategies", "Upload strategy", "Daily prices (Massive)", "59 trading days"):
            self.assertIn(text, page)
        r, body = self.req("GET", "/")
        self.assertIn(b"href='/strategies'", body)
        for path in ("/latest/report.html", "/runs/2026-10-25_120003/report.html"):
            r, body = self.req("GET", path)
            self.assertIn(b"href='/strategies'", body, path)
            self.assertLess(body.index(b"Strategies"), body.index(b"<main>"))

    def test_2_upload_run_view_end_to_end(self):
        r, _ = self.upload("/strategies/upload", "macdgen_v5_regime_backtest.py", V5_FILE.read_bytes())
        self.assertEqual((r.status, r.getheader("Location")), (303, "/strategies#strategies"))
        page = self.req("GET", "/strategies")[1].decode()
        self.assertIn("Added &#x27;macdgen_v5_regime_backtest&#x27; (V5-style strategy)", page)
        r, _ = self.form("/strategies/run", {"name": "macdgen_v5_regime_backtest", "scan": "latest", "mode": "close"})
        self.assertEqual(r.getheader("Location"), "/strategies#results")
        wait_idle(self.app.strategies)
        res = self.app.strategies.results()[0]
        self.assertEqual(res["status"], "ok")
        page = self.req("GET", "/strategies")[1].decode()
        self.assertIn(f"/strategies/results/{res['id']}/", page)
        r, body = self.req("GET", f"/strategies/results/{res['id']}/")
        self.assertEqual(r.status, 200)
        view = body.decode()
        self.assertIn("<h2>Signals</h2>", view)
        self.assertIn("2026-10-23", view)
        for f in ("signals.csv", "output.txt", "tickers.txt", "meta.json", "history_cache.json"):
            r, _ = self.req("GET", f"/strategies/results/{res['id']}/{f}")
            self.assertEqual(r.status, 200, f)

    def test_3_bad_upload_message_shown_and_security(self):
        r, _ = self.upload("/strategies/upload", "notes.txt", b"hello")
        page = self.req("GET", "/strategies")[1].decode()
        self.assertIn("Choose a Python file ending in .py", page)
        r, _ = self.req("POST", "/strategies/upload", b"x", "text/plain")
        self.assertIn("Upload the file using the form", self.req("GET", "/strategies")[1].decode())
        conn_origin_bad = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn_origin_bad.request("POST", "/strategies/run", body="name=x",
                                headers={"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode(),
                                         "Origin": "https://evil.example",
                                         "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(conn_origin_bad.getresponse().status, 403)
        for path in ("/strategies/results/../state.json", "/strategies/results/x/signals.csv",
                     "/strategies/results/2026-10-25_120003_x/../../state.json",
                     "/strategies/results/2026-10-25_120003_x/strategies.json"):
            self.assertEqual(self.req("GET", path)[0].status, 404, path)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)  # announce 13 MB, send nothing
        conn.putrequest("POST", "/strategies/upload")
        conn.putheader("Authorization", "Basic " + base64.b64encode(b"admin:pw").decode())
        conn.putheader("Content-Type", "multipart/form-data; boundary=zz")
        conn.putheader("Content-Length", str(13_000_000))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    def test_5_custom_buy_counted_and_shown(self):
        code = ("def finviz_signals(tickers, asof, cache):\n"
                "    return [{'Ticker': t, 'Status': 'BUY' if i < 2 else 'NO SIGNAL', 'Close': 1.5, 'Original_MFI': 60,\n"
                "             'Note': '', 'Error': ''} for i, t in enumerate(tickers)] + "
                "[{'Ticker': 'ZZZ', 'Status': 'ERROR', 'Error': 'Insufficient history'}]\n")
        self.upload("/strategies/upload", "custombuy.py", code.encode())
        self.form("/strategies/run", {"name": "custombuy", "scan": "latest", "mode": "close"})
        wait_idle(self.app.strategies)
        res = [m for m in self.app.strategies.results() if m.get("strategy") == "custombuy"][0]
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["counts"]["BUY"], 2)
        self.assertEqual(sv._counts_text(res["counts"]), "2 buy")
        page = self.req("GET", "/strategies")[1].decode()
        row = page[page.index(f"/strategies/results/{res['id']}/") - 900:page.index(f"/strategies/results/{res['id']}/")]
        self.assertIn("<td>2 buy</td>", row)
        view = self.req("GET", f"/strategies/results/{res['id']}/")[1].decode()
        sig = view[view.index("<h2>Signals</h2>"):view.index("<h2>Run details</h2>")]
        self.assertNotIn("No result table", sig)
        self.assertEqual(sig.count("class='badge buy'"), 2)
        self.assertIn("data-seg='buy'>Buy <span class='cnt'>2</span>", sig)
        self.assertIn("class='seg on' role='tab' aria-selected='true' data-seg='buy'", sig)  # opens on Buy
        for col in ("Close <span class='ar'>", "Original MFI <span class='ar'>"):  # the strategy's own columns
            self.assertIn(col, sig)
        self.assertNotIn("regime", sig.lower())
        self.assertIn("<div class='det'>Insufficient history</div>", sig)  # the reason shows under the ticker

    def test_7_signal_tickers_txt(self):
        # custom strategy with a Status column: only BUY rows, in table order, comma-separated
        self.upload("/strategies/upload", "txtbuy.py", (
            "def finviz_signals(tickers, asof, cache):\n"
            "    s = ['NO SIGNAL', 'BUY', 'ERROR', 'BUY', 'NO SIGNAL']\n"
            "    return [{'Ticker': t, 'Status': s[i % 5]} for i, t in enumerate(tickers[:10])]"
            " + [{'Ticker': tickers[1], 'Status': 'BUY'}]\n").encode())
        # no Status column: the table itself is the list of picks
        self.upload("/strategies/upload", "txtall.py", (
            "def finviz_signals(tickers, asof, cache):\n"
            "    return [{'Ticker': t, 'Score': 1} for t in tickers[:3]]\n").encode())
        # nothing signalled
        self.upload("/strategies/upload", "txtnone.py", (
            "def finviz_signals(tickers, asof, cache):\n"
            "    return [{'Ticker': t, 'Status': 'NO SIGNAL'} for t in tickers[:3]]\n").encode())
        for n in ("txtbuy", "txtall", "txtnone"):
            self.form("/strategies/run", {"name": n, "scan": "latest", "mode": "close"})
        wait_idle(self.app.strategies)
        res = {m["strategy"]: m for m in self.app.strategies.results() if m.get("strategy", "").startswith("txt")}
        tick = lambda n: [r["Ticker"] for r in csv.DictReader(
            (self.app.strategies.results_dir / res[n]["id"] / "signals.csv").open())]
        want = {"txtbuy": [tick("txtbuy")[i] for i in (1, 3, 6, 8)],  # BUY rows; the repeat is listed once
                "txtall": tick("txtall"), "txtnone": []}
        page = self.req("GET", "/strategies")[1].decode()
        for n, tickers in want.items():
            rid = res[n]["id"]
            self.assertEqual(res[n]["status"], "ok", n)
            self.assertIn(f"href='/strategies/results/{rid}/signal_tickers.txt'", page)
            r, body = self.req("GET", f"/strategies/results/{rid}/signal_tickers.txt")
            self.assertEqual((r.status, r.getheader("Content-Type")), (200, "text/plain; charset=utf-8"), n)
            self.assertIn(f'filename="{rid}_signals.txt"', r.getheader("Content-Disposition"))
            self.assertEqual(body.decode(), ",".join(tickers) + "\n", n)
            view = self.req("GET", f"/strategies/results/{rid}/")[1].decode()
            self.assertIn(f"/strategies/results/{rid}/signal_tickers.txt'>signal_tickers.txt</a>", view)
        self.assertEqual(len(want["txtbuy"]), 4)
        d = Path(tempfile.mkdtemp())  # V5-style and dashboard statuses
        try:
            (d / "signals.csv").write_text("AsOf,Ticker,Status\nx,AAA,BUY SIGNAL\nx,BBB,BLOCKED BY REGIME\n"
                                           "x,CCC,NO SIGNAL\nx,DDD,SIGNAL\nx,EEE,NOT IN PRICE CACHE\nx,,BUY\n")
            self.assertEqual(sv.signal_tickers(d), ["AAA", "DDD"])
        finally:
            shutil.rmtree(d)
        self.assertEqual(self.req("GET", "/strategies/results/2026-10-25_120003_x/signal_tickers.txt")[0].status, 404)
        self.assertEqual(self.req("GET", f"/strategies/results/{res['txtbuy']['id']}/signal_tickers.txt",
                                  auth=False)[0].status, 303)  # signed out -> login page

    def test_8_title_and_description_from_the_file(self):
        code = ("TITLE = '  Fourier   Money Flow '\nDESCRIPTION = 'Original MFI crosses above <b>Fourier</b> MF'\n"
                "def finviz_signals(tickers, asof, cache):\n"
                "    return [{'Ticker': t, 'Status': 'BUY', 'Close': 0.305, 'Original_MFI': 92.19, 'Fourier_MF': 47.05,"
                " 'Buy_Latest': True} for t in tickers[:2]]\n")
        self.upload("/strategies/upload", "fmfi_scanner.py", code.encode())
        m = self.app.strategies.meta()["fmfi_scanner"]
        self.assertEqual((m["title"], m["description"]),
                         ("Fourier Money Flow", "Original MFI crosses above <b>Fourier</b> MF"))
        self.form("/strategies/run", {"name": "fmfi_scanner", "scan": "latest", "mode": "close"})
        wait_idle(self.app.strategies)
        res = [x for x in self.app.strategies.results() if x.get("strategy") == "fmfi_scanner"][0]
        view = self.req("GET", f"/strategies/results/{res['id']}/")[1].decode()
        self.assertIn("<h1>Fourier Money Flow</h1>", view)
        self.assertIn("<div class='sub'>FMFI scanner · Original MFI crosses above &lt;b&gt;Fourier&lt;/b&gt; MF</div>",
                      view)  # escaped, never raw HTML
        self.assertIn("<span class='pill done'><span class='dot'></span>Completed</span>", view)
        for txt in ("MFI gap <span class='ar'>↓</span>", "<td class='n up'>+45.14</td>", "<td class='n '>$0.305</td>",
                    "Latest candle only", "Min original MFI", "MFI gap = Original MFI − Fourier MF"):
            self.assertIn(txt, view)

    def test_9_tradingview_link(self):
        self.upload("/strategies/upload", "tvstrat.py",
                    b"def finviz_signals(t, a, c):\n    return [{'Ticker': x, 'Status': 'BUY'} for x in t[:1]]\n")
        page = self.req("GET", "/strategies")[1].decode()
        row = page[page.index("value='tvstrat'"):page.index("</label>", page.index("value='tvstrat'"))]
        self.assertIn("class='ghost tvedit add' hidden data-name='tvstrat'", row)
        self.assertIn("+ TV link</button>", row)
        self.assertNotIn("class='tvlink'", row)
        self.assertIn("<dialog id='tvdlg'", page)
        url = "https://www.tradingview.com/chart/AbC123/?symbol=NASDAQ%3AAAPL&interval=D"
        r, _ = self.form("/strategies/tvlink", {"name": "tvstrat", "url": url})
        self.assertEqual(r.getheader("Location"), "/strategies#strategies")
        page = self.req("GET", "/strategies")[1].decode()
        self.assertIn("TradingView link saved for &#x27;tvstrat&#x27;.", page)
        row = page[page.index("value='tvstrat'"):page.index("</label>", page.index("value='tvstrat'"))]
        self.assertIn("<a class='tvlink' href='https://www.tradingview.com/chart/AbC123/?symbol=NASDAQ%3AAAPL&amp;"
                      "interval=D' target='_blank' rel='noopener noreferrer'", row)
        self.assertIn("data-url='https://www.tradingview.com/chart/AbC123/?symbol=NASDAQ%3AAAPL&amp;interval=D'", row)
        # bad links are refused and the saved one stays
        for bad, msg in (("https://evil.example/chart", "Use a TradingView link"), ("javascript:alert(1)", "Use a TradingView"),
                         ("https://www.tradingview.com/x'onclick='y", "normal https://")):
            self.form("/strategies/tvlink", {"name": "tvstrat", "url": bad})
            self.assertIn(msg, self.req("GET", "/strategies")[1].decode(), bad)
            self.assertEqual(self.app.strategies.meta()["tvstrat"]["tv_url"], url)
        # bare domain is accepted and made https
        self.form("/strategies/tvlink", {"name": "tvstrat", "url": "tradingview.com/script/xyz/"})
        self.assertEqual(self.app.strategies.meta()["tvstrat"]["tv_url"], "https://tradingview.com/script/xyz/")
        self.form("/strategies/tvlink", {"name": "tvstrat", "url": url})
        # survives uploading a new version of the file
        self.upload("/strategies/upload", "tvstrat.py",
                    b"def finviz_signals(t, a, c):\n    return [{'Ticker': x, 'Status': 'BUY'} for x in t[:2]]\n")
        self.assertEqual(self.app.strategies.meta()["tvstrat"]["tv_url"], url)
        # shown on its result page
        self.form("/strategies/run", {"name": "tvstrat", "scan": "latest", "mode": "close"})
        wait_idle(self.app.strategies)
        res = [x for x in self.app.strategies.results() if x.get("strategy") == "tvstrat"][0]
        view = self.req("GET", f"/strategies/results/{res['id']}/")[1].decode()
        self.assertIn("class='tvbtn tvlink' href='https://www.tradingview.com/chart/AbC123/", view)
        # removing: save an empty link
        self.form("/strategies/tvlink", {"name": "tvstrat", "url": ""})
        self.assertNotIn("tv_url", self.app.strategies.meta()["tvstrat"])
        self.assertIn("TradingView link removed", self.req("GET", "/strategies")[1].decode())
        self.form("/strategies/tvlink", {"name": "nope", "url": url})
        self.assertIn("That strategy no longer exists", self.req("GET", "/strategies")[1].decode())

    def test_10_candle_picker_on_the_page(self):
        code = (b"CANDLE_DAYS = 1\n"
                b"def finviz_signals(tickers, asof, cache):\n"
                b"    n = len(cache['history'])\n"
                b"    return [{'Ticker': t, 'Status': 'BUY', 'Candles': CANDLE_LABEL, 'N': n} for t in tickers[:1]]\n")
        self.upload("/strategies/upload", "candly.py", code)
        self.assertTrue(self.app.strategies.meta()["candly"].get("candles"))
        page = self.req("GET", "/strategies")[1].decode()
        self.assertIn("<input type='number' name='cn' min='1' max='390' step='1' value='1'", page)
        self.assertIn("<option value='days' selected>days</option>", page)
        self.assertIn("Used by: Candly", page)
        base = {"s": "candly", "scan": "latest", "mode": "close", "when": "now"}
        self.form("/strategies/go", {**base, "cn": "0", "cu": "days"})
        self.assertIn("Pick 1–30 days per candle.", self.req("GET", "/strategies")[1].decode())
        self.form("/strategies/go", {**base, "cn": "500", "cu": "minutes"})
        self.assertIn("Pick 1–390 minutes per candle.", self.req("GET", "/strategies")[1].decode())
        # run now on 5-day candles
        before = {m["id"] for m in self.app.strategies.results()}
        self.form("/strategies/go", {**base, "cn": "5", "cu": "days"})
        page = self.req("GET", "/strategies")[1].decode()
        self.assertIn("1 strategy queued (5-day candles).", page)
        wait_idle(self.app.strategies)
        res = [m for m in self.app.strategies.results() if m["id"] not in before][0]
        self.assertEqual((res["status"], res["candles"]), ("ok", "5d"))
        out = self.app.strategies.results_dir / res["id"]
        row = list(csv.DictReader((out / "signals.csv").open()))[0]
        self.assertEqual(row["Candles"], "5-day")
        page = self.req("GET", "/strategies")[1].decode()
        self.assertIn("value='5'", page)  # the choice is remembered
        self.assertIn("5-day candles</div>", page)  # shown in the results list
        view = self.req("GET", f"/strategies/results/{res['id']}/")[1].decode()
        self.assertIn("· Latest close · 5-day candles</span>", view)
        # minute candles need FinViz prices; this server uses Massive, so the run is daily with a note
        before = {m["id"] for m in self.app.strategies.results()}
        self.form("/strategies/go", {**base, "cn": "15", "cu": "minutes"})
        wait_idle(self.app.strategies)
        res = [m for m in self.app.strategies.results() if m["id"] not in before][0]
        self.assertEqual(res["candles"], "1d")
        self.assertIn("Minute candles need FinViz prices",
                      (self.app.strategies.results_dir / res["id"] / "output.txt").read_text())
        # schedules and the after-scan set keep the choice
        self.form("/strategies/go", {**base, "when": "weekdays", "time": "13:10", "cn": "5", "cu": "days"})
        job = [j for j in self.app.schedules.jobs("strategies") if j["status"] == "scheduled"][-1]
        self.assertEqual(job.get("candles"), "5d")
        self.assertIn("latest close · 5-day candles", self.req("GET", "/strategies")[1].decode())
        self.app.schedules.cancel(job["id"])
        self.form("/strategies/autoset", {"s": "candly", "auto": "on", "scan": "latest", "mode": "close",
                                          "cn": "5", "cu": "days"})
        self.assertEqual(self.app.strategies.auto_config()["candles"], "5d")
        self.assertIn("(latest close, 5-day candles)", self.req("GET", "/strategies")[1].decode())
        self.form("/strategies/autoset", {"s": "candly", "scan": "latest", "mode": "close", "cn": "1", "cu": "days"})

    def test_6_delete_button_next_to_each_strategy(self):
        self.upload("/strategies/upload", "throwaway.py", b"def finviz_signals(t, a, c):\n    return []\n")
        page = self.req("GET", "/strategies")[1].decode()
        start = page.index("value='throwaway'")
        row = page[start:page.index("</label>", start)]
        self.assertIn("type='button' class='ghost del' hidden data-name='throwaway'", row)
        self.assertIn("<form id='delform' method='post' action='/strategies/delete'", page)
        go = page[page.index("<form id='go'"):page.index("</form>", page.index("<form id='go'"))]
        self.assertNotIn("formaction='/strategies/delete'", go)  # Enter in the run form can't delete
        r, _ = self.form("/strategies/delete", {"name": "throwaway"})
        self.assertEqual(r.getheader("Location"), "/strategies#strategies")
        page = self.req("GET", "/strategies")[1].decode()
        self.assertIn("Moved &#x27;throwaway&#x27; to the recycle bin for 30 days. Its past results are kept.", page)
        self.assertNotIn("value='throwaway'", page)
        self.form("/strategies/delete", {"name": "throwaway"})
        self.assertIn("That strategy no longer exists", self.req("GET", "/strategies")[1].decode())

    def test_4_mcap_and_prices_buttons(self):
        r, _ = self.upload("/strategies/mcap", "mcap.csv", b"Ticker,Cap\nS001,1\n")
        self.assertEqual(r.getheader("Location"), "/strategies#data")
        self.assertIn("Market-cap list saved (1 tickers)", self.req("GET", "/strategies")[1].decode())
        self.form("/strategies/mcap/delete", {})
        self.assertIn("Market-cap list removed", self.req("GET", "/strategies")[1].decode())
        self.form("/strategies/prices/test", {})
        page = self.req("GET", "/strategies")[1].decode()
        self.assertTrue("✅ Key works" in page or "✅ Key accepted" in page)
        self.form("/strategies/prices/update", {})
        page = self.req("GET", "/strategies")[1].decode()
        self.assertTrue("Price update started" in page or "already running" in page)


# ── FinViz Elite price export ────────────────────────────────────────────────
FV_TOKEN = "fvTOKEN_secret_42/="


class FakeFinviz:
    """/quote_export?t=SYM&p=d&auth=TOKEN like FinViz Elite. Old .ashx path answers 404 (tests fallback).
    Dates as MM/DD/YYYY, newest first, volume with thousands separators. Unknown ticker -> HTML page."""

    def __init__(self, market=None, extra_today=None, date_fmt="%m/%d/%Y"):
        self.market = market or MARKET
        self.extra_today = extra_today  # (iso_day, {sym: bar}) appended as a still-forming bar
        self.date_fmt = date_fmt
        self.requests = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(parts.query)
                outer.requests.append(parts.path + "?" + parts.query)
                if parts.path == "/quote_export.ashx":
                    return self._send(404, "text/html", "<html>not here</html>")
                if parts.path != "/quote_export":
                    return self._send(404, "text/plain", "nope")
                if q.get("auth") != [FV_TOKEN]:
                    return self._send(200, "text/html", "<!DOCTYPE html><html><body>Login</body></html>")
                sym, p = q.get("t", [""])[0], q.get("p", [""])[0]
                if p.startswith("i"):  # intraday: the real FinViz SPY bars (testdata), varied per ticker
                    text = intraday_csv(sym, p)
                    if text is None:
                        return self._send(200, "text/html", "<html><body>Ticker not found</body></html>")
                    return self._send(200, "text/csv", text)
                assert p == "d"
                days = sorted(d for d in outer.market if sym in outer.market[d])
                if not days:
                    return self._send(200, "text/html", "<html><body>Ticker not found</body></html>")
                lines = ["Date,Open,High,Low,Close,Volume"]
                rows = [(d, outer.market[d][sym]) for d in days]
                if outer.extra_today and sym in outer.extra_today[1]:
                    rows.append((outer.extra_today[0], outer.extra_today[1][sym]))
                for d, b in reversed(rows):
                    ds = dt.date.fromisoformat(d).strftime(outer.date_fmt)
                    lines.append(f"{ds},{b['o']},{b['h']},{b['l']},{b['c']},\"{int(b['v']):,}\"")
                return self._send(200, "text/csv", "\n".join(lines) + "\n")

            def _send(self, code, ctype, text):
                body = text.encode()
                self.send_response(code); self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


import finviz_agent as fa  # noqa: E402
import finviz_prices as fvp  # noqa: E402


class TestFinvizPrices(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def source(self, fake, token=FV_TOKEN, history=150):
        c = fa.FinvizClient(token, base_url=fake.url, min_interval=0, sleep=lambda s: None)
        return fvp.FinvizPriceSource(c, self.tmp / "fv", history)

    def test_parse_formats(self):
        for raw, want in [("10/23/2026", "2026-10-23"), ("2026-10-23", "2026-10-23"),
                          ("2026-10-23 00:00:00", "2026-10-23"), ("10/23/2026 4:00 PM", "2026-10-23"),
                          ("20261023", "2026-10-23"), ("10/23/26", "2026-10-23"), ("2026-10-23T16:00:00", "2026-10-23"),
                          ("junk", None), ("", None)]:
            self.assertEqual(fvp.parse_date(raw), want, raw)
        bars, info = fvp.parse_quote_csv('﻿Date,Open,High,Low,Close,Volume\n10/23/2026,1,2,0.5,1.5,"1,234"\nbad,row\n')
        self.assertEqual(bars, {"2026-10-23": {"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1234.0}})
        self.assertEqual(info["bad_rows"], 1)
        with self.assertRaises(fvp.FinvizPriceError):
            fvp.parse_quote_csv("Foo,Bar\n1,2\n")

    def test_cache_equals_market_and_fallback_path(self):
        fake = FakeFinviz()
        try:
            src = self.source(fake)
            cache, info = src.build_cache(["S001", "BRK.B", "NOPE"], now_utc=FRIDAY_EVENING)
            self.assertEqual(info["missing"].keys(), {"NOPE"})
            self.assertEqual(set(cache["tickers"]), {"SPY", "S001", "BRK.B"})
            for d in ("2026-10-23", "2026-06-01"):
                self.assertEqual(cache["history"][d]["S001"], {k: float(v) for k, v in MARKET[d]["S001"].items()})
            self.assertEqual(len(cache["history"]), 150)
            self.assertEqual(max(cache["history"]), "2026-10-23")
            self.assertTrue(fake.requests[0].startswith("/quote_export.ashx"))
            self.assertTrue(all(r.startswith("/quote_export?") for r in fake.requests[2:]))  # remembered
            n = len(fake.requests)
            src.build_cache(["S001"], now_utc=FRIDAY_EVENING)  # within 10 min: served from disk
            self.assertEqual(len(fake.requests), n)
        finally:
            fake.close()

    def test_bad_token_aborts_and_is_redacted(self):
        fake = FakeFinviz()
        try:
            with self.assertRaises(fvp.FinvizPriceError) as cm:
                self.source(fake, token="wrong_" + FV_TOKEN).build_cache(["S001"], now_utc=FRIDAY_EVENING)
            self.assertNotIn(FV_TOKEN, str(cm.exception))
            msg = self.source(fake, token="wrong_" + FV_TOKEN).test()
            self.assertTrue(msg.startswith("❌"), msg)
            ok = self.source(fake).test()
            self.assertTrue(ok.startswith("✅ FinViz prices work: SPY"), ok)
            self.assertIn("→ 2026-10-23", ok)
        finally:
            fake.close()

    def test_close_vs_intraday_today_bar(self):
        today = "2026-10-26"
        partial = {s: {"o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000} for s in ("SPY", "S001")}
        fake = FakeFinviz(extra_today=(today, partial))
        try:
            noon_pt = dt.datetime(2026, 10, 26, 19, 0, tzinfo=UTC)  # 15:00 ET, market open
            c1, i1 = self.source(fake).build_cache(["S001"], include_intraday=False, now_utc=noon_pt)
            self.assertNotIn(today, c1["history"]); self.assertTrue(i1["dropped_today"])
            shutil.rmtree(self.tmp / "fv")
            c2, i2 = self.source(fake).build_cache(["S001"], include_intraday=True, now_utc=noon_pt)
            self.assertEqual(c2["history"][today]["S001"]["c"], 10.5)
            self.assertIn("today's bar so far from FinViz", i2["intraday"])
            shutil.rmtree(self.tmp / "fv")
            evening = dt.datetime(2026, 10, 26, 22, 0, tzinfo=UTC)  # after the close: today's bar is final
            c3, _ = self.source(fake).build_cache(["S001"], include_intraday=False, now_utc=evening)
            self.assertIn(today, c3["history"])
        finally:
            fake.close()


ET = sv.ZoneInfo("America/New_York")


def et(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=ET).astimezone(UTC)


class TestFinvizPriceReuse(unittest.TestCase):
    """A download made after the latest close has settled is reused until the next close."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = FakeFinviz()
        c = fa.FinvizClient(FV_TOKEN, base_url=self.fake.url, min_interval=0, sleep=lambda s: None)
        self.src = fvp.FinvizPriceSource(c, self.tmp / "fv", 150)

    def tearDown(self):
        self.fake.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def downloads(self, fetched_at, now, intraday=False, tickers=("S001", "S002")):
        """Save files as if downloaded at `fetched_at`; return how many FinViz requests a run at `now` makes."""
        shutil.rmtree(self.tmp / "fv", ignore_errors=True)
        (self.tmp / "fv").mkdir()
        self.src.build_cache(list(tickers), now_utc=fetched_at)
        for p in (self.tmp / "fv").iterdir():
            os.utime(p, (fetched_at.timestamp(), fetched_at.timestamp()))
        before = len(self.fake.requests)
        _, info = self.src.build_cache(list(tickers), include_intraday=intraday, now_utc=now)
        n = len(self.fake.requests) - before
        self.assertEqual(info["reused"] + info["downloaded"], len(tickers) + 1)
        self.assertEqual(info["downloaded"], n)
        return n

    def test_last_close(self):
        cases = [
            (et(2026, 10, 30, 16, 0), et(2026, 10, 30, 16, 0)),    # Fri 16:00 exactly
            (et(2026, 10, 30, 15, 59), et(2026, 10, 29, 16, 0)),   # Fri before close -> Thu
            (et(2026, 10, 31, 11, 0), et(2026, 10, 30, 16, 0)),    # Sat -> Fri
            (et(2026, 11, 2, 12, 0), et(2026, 10, 30, 16, 0)),     # Mon session -> Fri
            (et(2026, 11, 2, 16, 30), et(2026, 11, 2, 16, 0)),     # Mon evening -> Mon
            (et(2026, 11, 3, 1, 0), et(2026, 11, 2, 16, 0)),       # after the DST change (Nov 1)
        ]
        for now, want in cases:
            self.assertEqual(fvp.last_close(now), want, now)

    def test_after_close_download_reused_until_next_close(self):
        fri = et(2026, 10, 30, 16, 10)  # 1:10 pm Pacific
        for now in (et(2026, 10, 30, 20, 0), et(2026, 10, 31, 12, 0), et(2026, 11, 2, 9, 0),
                    et(2026, 11, 2, 12, 0), et(2026, 11, 2, 15, 59)):
            self.assertEqual(self.downloads(fri, now), 0, now)
        self.assertEqual(self.downloads(fri, et(2026, 11, 2, 16, 10)), 3)  # Monday's close: new bar

    def test_download_before_settled_close_not_reused_after_it(self):
        self.assertEqual(self.downloads(et(2026, 10, 30, 15, 58), et(2026, 10, 30, 16, 10)), 3)
        self.assertEqual(self.downloads(et(2026, 10, 30, 16, 1), et(2026, 10, 30, 16, 10)), 3)
        self.assertEqual(self.downloads(et(2026, 10, 30, 15, 58), et(2026, 10, 30, 16, 2)), 3)
        # during the session, a download made earlier the same session is fine for latest close
        self.assertEqual(self.downloads(et(2026, 10, 30, 10, 0), et(2026, 10, 30, 15, 0)), 0)

    def test_intraday_while_open_needs_fresh_download(self):
        mon = et(2026, 11, 2, 12, 0)
        self.assertEqual(self.downloads(et(2026, 10, 30, 16, 10), mon, intraday=True), 3)
        self.assertEqual(self.downloads(et(2026, 11, 2, 11, 55), mon, intraday=True), 0)  # 5 min old
        self.assertEqual(self.downloads(et(2026, 11, 2, 11, 45), mon, intraday=True), 3)  # 15 min old
        # market closed (Saturday): an after-close download already has the final bar
        self.assertEqual(self.downloads(et(2026, 10, 30, 16, 10), et(2026, 10, 31, 12, 0), intraday=True), 0)

    def test_reused_prices_identical_to_fresh(self):
        fri = et(2026, 10, 30, 16, 10)
        c1, _ = self.src.build_cache(["S001", "S002"], now_utc=fri)
        for p in (self.tmp / "fv").iterdir():
            os.utime(p, (fri.timestamp(), fri.timestamp()))
        c2, info = self.src.build_cache(["S001", "S002"], now_utc=et(2026, 10, 31, 12, 0))
        self.assertEqual(info["reused"], 3)
        self.assertEqual(c1, c2)


class TestFinvizStrategyRun(unittest.TestCase):
    """End to end: V5 on FinViz prices gives exactly the same signals as on the reference market data."""

    def test_v5_with_finviz_prices(self):
        fake = FakeFinviz(date_fmt="%Y-%m-%d")
        data = Path(tempfile.mkdtemp())
        try:
            reports = data / "reports"; reports.mkdir(parents=True)
            tickers = ["S%03d" % i for i in range(120)] + ["BRK.B", "NOPE"]
            make_scan(reports, "2026-10-25_120003", tickers)
            env = {"FINVIZ_API_TOKEN": FV_TOKEN, "FINVIZ_BASE_URL": fake.url, "FINVIZ_PRICE_INTERVAL": "0",
                   "STRATEGY_TIMEOUT_SEC": "60"}
            st = sm.StrategyManager(data, reports, sv.ZoneInfo("America/Los_Angeles"), env, log=lambda m: None)
            self.assertEqual(st.price_source, "finviz")
            st.finviz.client._sleep = lambda s: None
            st.upload_strategy("v5.py", V5_FILE.read_bytes())
            st.enqueue("v5", "latest", "close", "manual"); wait_idle(st)
            res = st.results()[0]
            out = st.results_dir / res["id"]
            log = (out / "output.txt").read_text()
            self.assertEqual(res["status"], "ok", log)
            self.assertEqual((res["price_source"], res["asof"]), ("finviz", "2026-10-23"))
            self.assertIn("FinViz prices: 122 of 123 tickers; no prices for 1: NOPE", log)
            with (out / "signals.csv").open() as fh:
                got = list(csv.DictReader(fh))
            full = {"tickers": SYMS, "history": {d: {s: {k: float(v) for k, v in b.items()} for s, b in bars.items()}
                                                 for d, bars in MARKET.items()}}
            (data / "full.json").write_text(json.dumps(full))
            ref = data / "ref"; ref.mkdir()
            import contextlib
            with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
                v5.main(["--strategy", str(V5_FILE), "--tickers", str(out / "tickers.txt"), "--cache",
                         str(data / "full.json"), "--out", str(ref), "--asof", "2026-10-23", "--no-mcap-filter"])
            with next(ref.glob("*.csv")).open() as fh:
                want = list(csv.DictReader(fh))
            self.assertEqual([(r["Ticker"], r["Status"], r["Chg_%"], r["Tier"]) for r in got],
                             [(r["Ticker"], r["Status"], r["Chg_%"], r["Tier"]) for r in want])
            for f in data.rglob("*"):
                if f.is_file():
                    self.assertNotIn(FV_TOKEN.encode(), f.read_bytes(), f)
        finally:
            fake.close(); shutil.rmtree(data, ignore_errors=True)


RENAMED = ["MIN_PRICE", "MIN_AVG_VOL_10D", "CHG_MIN", "MAX_CHG_PCT", "MAX_WEEK_PCT", "MAX_VOL_RATIO"]


def v5_variant_bytes() -> bytes:
    """The user's case: V5 logic unchanged, but the six filter settings are named differently."""
    import re as _re
    text = V5_FILE.read_text()
    for n in RENAMED:
        text = _re.sub(rf"\b{n}\b", f"V6_{n}", text)
    return text.encode()


class TestV5Variants(unittest.TestCase):
    def setUp(self):
        self.fake = FakeFinviz()
        self.data = Path(tempfile.mkdtemp())
        self.reports = self.data / "reports"; self.reports.mkdir(parents=True)
        make_scan(self.reports, "2026-10-25_120003", ["S%03d" % i for i in range(120)])
        env = {"FINVIZ_API_TOKEN": FV_TOKEN, "FINVIZ_BASE_URL": self.fake.url, "FINVIZ_PRICE_INTERVAL": "0",
               "STRATEGY_TIMEOUT_SEC": "60"}
        self.st = sm.StrategyManager(self.data, self.reports, sv.ZoneInfo("America/Los_Angeles"), env, log=lambda m: None)
        self.st.finviz.client._sleep = lambda s: None

    def tearDown(self):
        self.fake.close(); shutil.rmtree(self.data, ignore_errors=True)

    def run_one(self, name):
        self.st.enqueue(name, "latest", "close", "manual"); wait_idle(self.st)
        res = [r for r in self.st.results() if r["strategy"] == name][0]
        out = self.st.results_dir / res["id"]
        self.assertEqual(res["status"], "ok", (out / "output.txt").read_text())
        with (out / "signals.csv").open() as fh:
            return list(csv.DictReader(fh))

    def test_renamed_settings_variant_matches_original(self):
        text = v5_variant_bytes().decode()
        self.assertNotIn("MIN_PRICE =", text.replace("V6_MIN_PRICE", ""))  # really renamed
        msg = self.st.upload_strategy("macdgen_v6.py", v5_variant_bytes())
        self.assertIn("V5-style", msg)
        self.st.upload_strategy("macdgen_v5_regime_backtest.py", V5_FILE.read_bytes())
        orig, var = self.run_one("macdgen_v5_regime_backtest"), self.run_one("macdgen_v6")
        key = lambda rows: [(r["Ticker"], r["Status"], r["Tier"], r["Chg_%"]) for r in rows]
        self.assertEqual(key(orig), key(var))  # same decisions, same sizing
        self.assertGreater(sum(r["Status"] == "BUY SIGNAL" for r in var) + sum(r["Status"] == "BLOCKED BY REGIME" for r in var), 0)
        filtered = [r for r in var if r["Status"] == "NO SIGNAL" and "MACD" not in r["Detail"]]
        self.assertTrue(filtered)
        self.assertTrue(all(r["Detail"] in ("did not pass the strategy's price/volume filters",)
                            or r["Detail"].startswith("no trading data") for r in filtered))
        # the original still gets detailed reasons
        self.assertTrue(any("is under +3%" in r["Detail"] for r in orig))

    def test_minimal_variant_without_regime_tier_or_tracking(self):
        src = (b"import numpy as np\n"
               b"def passes_v5_filters(idx, closes, volumes):\n"
               b"    return idx > 30 and closes[idx] > closes[idx-1] * 1.02\n"
               b"def detect_macd_cross_up(idx, closes):\n"
               b"    return closes[idx] > np.mean(closes[idx-10:idx])\n")
        msg = self.st.upload_strategy("tiny.py", src)
        self.assertIn("no market-regime filter, no tier sizing, no trade tracking", msg)
        rows = self.run_one("tiny")
        buys = [r for r in rows if r["Status"] == "BUY SIGNAL"]
        self.assertTrue(buys)
        self.assertTrue(all(r["Tier"] == "—" and r["Regime"] == "NO FILTER" for r in buys))
        self.assertTrue(all(float(r["Chg_%"]) >= 2.0 for r in buys))  # computed by the adapter (shown to 2 decimals)

    def test_wrong_signature_rejected_clearly(self):
        src = (b"def passes_v5_filters(closes):\n    return True\n"
               b"def detect_macd_cross_up(idx, closes):\n    return True\n")
        with self.assertRaises(ValueError) as cm:
            self.st.upload_strategy("bad_sig.py", src)
        self.assertIn("passes_v5_filters must accept (idx, closes, volumes)", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            self.st.upload_strategy("nothing.py", b"MIN_PRICE = 1\n")
        self.assertIn("missing: passes_v5_filters, detect_macd_cross_up", str(cm.exception))


DASHBOARD_FILE = HERE / "testdata" / "v5_daily_dashboard.py"


class TestScriptStrategies(unittest.TestCase):
    """Whole-script strategies such as the user's v5_daily_dashboard.py: main() is run unchanged."""

    def setUp(self):
        self.fake = FakeFinviz()
        self.data = Path(tempfile.mkdtemp())
        self.reports = self.data / "reports"; self.reports.mkdir(parents=True)
        self.tickers = ["S%03d" % i for i in range(120)] + ["BRK.B", "NOPE"]
        make_scan(self.reports, "2026-10-25_120003", self.tickers)
        env = {"FINVIZ_API_TOKEN": FV_TOKEN, "FINVIZ_BASE_URL": self.fake.url, "FINVIZ_PRICE_INTERVAL": "0",
               "STRATEGY_TIMEOUT_SEC": "120"}
        self.st = sm.StrategyManager(self.data, self.reports, sv.ZoneInfo("America/Los_Angeles"), env, log=lambda m: None)
        self.st.finviz.client._sleep = lambda s: None

    def tearDown(self):
        self.fake.close(); shutil.rmtree(self.data, ignore_errors=True)

    def run_one(self, name, expect="ok"):
        self.st.enqueue(name, "latest", "close", "manual"); wait_idle(self.st)
        res = [r for r in self.st.results() if r["strategy"] == name][0]
        out = self.st.results_dir / res["id"]
        self.assertEqual(res["status"], expect, (out / "output.txt").read_text())
        return res, out

    def page(self, rid):
        app = type("A", (), {})()
        app.settings = type("S", (), {"tz": sv.ZoneInfo("America/Los_Angeles")})()
        app.strategies = self.st
        return sv.render_result(app, rid)

    def test_dashboard_runs_unchanged_and_matches_running_the_script_itself(self):
        msg = self.st.upload_strategy("v5_daily_dashboard.py", DASHBOARD_FILE.read_bytes())
        self.assertIn("(whole script)", msg)
        self.assertIn("CACHE_FILE, OUT_DIR", msg)
        self.assertEqual(self.st.meta()["v5_daily_dashboard"]["kind"], "script")
        res, out = self.run_one("v5_daily_dashboard")
        log = (out / "output.txt").read_text()
        self.assertEqual(res["asof"], "2026-10-23")
        self.assertIn("Scan day : 2026-10-23", log)          # the script's own report
        self.assertIn("V5 DAILY DASHBOARD", log)
        self.assertIn("No prices for 1: NOPE", log)
        own = [p for p in out.glob("v5_daily_dashboard_2026-10-23_*.csv")]
        self.assertEqual(len(own), 1)                          # written by the script into OUT_DIR
        self.assertEqual(own[0].read_bytes(), (out / "signals.csv").read_bytes())

        # Reference: the user's file with only its two paths edited, on a cache built here independently
        # from the reference market (last 150 days, scan tickers that have prices, days <= as-of).
        days = sorted(MARKET)[-150:]
        ref_cache = {"tickers": [t for t in self.tickers if t in SYMS],
                     "history": {d: MARKET[d] for d in days if d <= "2026-10-23"}}
        ref = self.data / "ref"; ref.mkdir()
        (ref / "cache.json").write_text(json.dumps(ref_cache))
        src = DASHBOARD_FILE.read_text()
        src = src.replace("'/Users/sri/stock-scanners/history_cache.json'", repr(str(ref / "cache.json")))
        src = src.replace("'/Users/sri/stock data/'", repr(str(ref) + "/"))
        (ref / "copy.py").write_text(src)
        import subprocess, sys
        subprocess.run([sys.executable, str(ref / "copy.py")], check=True, capture_output=True)
        want = next(ref.glob("v5_daily_dashboard_*.csv")).read_bytes()
        self.assertEqual((out / "signals.csv").read_bytes(), want)   # byte for byte

        with (out / "signals.csv").open() as fh:
            listed = {r["Ticker"]: r for r in csv.DictReader(fh)}
        self.assertTrue(listed, "test market should produce some dashboard signals")
        with (out / "ticker_status.csv").open() as fh:
            status = list(csv.DictReader(fh))
        self.assertEqual([r["Ticker"] for r in status], self.tickers)  # every scan ticker, scan order
        self.assertEqual({r["Ticker"] for r in status if r["Status"] == "SIGNAL"}, set(listed))
        kinds = {r["Status"] for r in status}
        self.assertTrue(kinds <= {"SIGNAL", "FILTERED OUT", "NO SCANNER FIRED", "NOT IN PRICE CACHE"}, kinds)
        self.assertIn("FILTERED OUT", kinds)
        self.assertFalse([r for r in status if r["Detail"].startswith("did not pass")])  # always a real reason
        self.assertEqual([r["Ticker"] for r in status if r["Status"] == "NOT IN PRICE CACHE"], ["NOPE"])
        self.assertEqual(res["counts"].get("SIGNAL"), len(listed))
        self.assertEqual(sv._counts_text(res["counts"]), f"{len(listed)} signals of {len(self.tickers)}")

        html_page = self.page(res["id"])
        self.assertIn(f"data-seg='buy'>Buy <span class='cnt'>{len(listed)}</span>", html_page)
        self.assertIn(f"data-seg='all'>All results <span class='cnt'>{len(self.tickers)}</span>", html_page)
        self.assertIn("Scanners", html_page)
        self.assertIn(own[0].name, html_page)                  # its own CSV is downloadable too
        self.assertEqual(sm.result_file_type(own[0].name), "text/csv; charset=utf-8")
        self.assertIsNone(sm.result_file_type("../meta.json"))
        for f in self.data.rglob("*"):
            if f.is_file():
                self.assertNotIn(FV_TOKEN.encode(), f.read_bytes(), f)

    def test_any_whole_script_with_cache_file_and_main(self):
        src = (b"import json, csv\n"
               b"CACHE_FILE = '/Users/someone/history_cache.json'\n"
               b"MCAP_FILE = '/Users/someone/mcap.csv'\n"
               b"def main():\n"
               b"    c = json.load(open(CACHE_FILE))\n"
               b"    last = sorted(c['history'])[-1]\n"
               b"    with open('picks.csv', 'w', newline='') as f:\n"
               b"        w = csv.writer(f); w.writerow(['Ticker', 'Close', 'Date'])\n"
               b"        for t in c['tickers']:\n"
               b"            b = c['history'][last].get(t)\n"
               b"            if b: w.writerow([t, b['c'], last])\n"
               b"if __name__ == '__main__':\n    main()\n")
        msg = self.st.upload_strategy("closes.py", src)
        self.assertIn("(whole script)", msg)
        self.assertIn("MCAP_FILE also point to files on your Mac", msg)
        res, out = self.run_one("closes")
        log = (out / "output.txt").read_text()
        self.assertIn("MCAP_FILE = '/Users/someone/mcap.csv' does not exist on the server", log)
        with (out / "signals.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual({r["Date"] for r in rows}, {"2026-10-23"})
        self.assertNotIn("SPY", {r["Ticker"] for r in rows})   # SPY is only there because we fetch it
        self.assertEqual(len(rows), 121)
        self.assertEqual(float(rows[0]["Close"]), MARKET["2026-10-23"][rows[0]["Ticker"]]["c"])
        self.assertFalse((out / "ticker_status.csv").exists())   # not the dashboard layout
        self.assertIn("<h2>Results</h2>", self.page(res["id"]))

    def test_script_problems_are_explained(self):
        with self.assertRaises(ValueError) as cm:
            self.st.upload_strategy("no_cache.py", b"def main():\n    print('hi')\n")
        self.assertIn("has a main() but no CACHE_FILE", str(cm.exception))
        self.st.upload_strategy("no_csv.py", b"CACHE_FILE = '/x/history_cache.json'\ndef main():\n    print('done')\n")
        res, out = self.run_one("no_csv", expect="error")
        self.assertIn("did not write a CSV file", (out / "output.txt").read_text())
        self.st.upload_strategy("crash.py", b"CACHE_FILE = '/x/history_cache.json'\ndef main():\n    1/0\n")
        res, out = self.run_one("crash", expect="error")
        self.assertIn("ZeroDivisionError", (out / "output.txt").read_text())

    @unittest.skipUnless(os.geteuid() == 0, "the sandbox user only exists when the server runs as root")
    def test_links_planted_by_a_strategy_are_never_followed(self):
        secret = self.data / "secret.txt"
        secret.write_text("TOP SECRET")
        os.chmod(secret, 0o600)
        src = ("import os\n"
               "def finviz_signals(tickers, asof, cache):\n"
               "    for n in ('signals.csv', 'meta.json.tmp', 'output.txt', 'ticker_status.csv'):\n"
               "        try: os.unlink(n)\n"
               "        except OSError: pass\n"
               f"    for n in ('meta.json.tmp', 'output.txt', 'ticker_status.csv', 'x.csv'): os.symlink({str(secret)!r}, n)\n"
               "    import subprocess, sys\n"
               "    subprocess.Popen([sys.executable, '-c', 'import time,os\\ntime.sleep(3)\\nopen(\"late.csv\",\"w\").write(\"x\")'])\n"
               "    return [{'Ticker': 'S001'}]\n").encode()
        self.st.upload_strategy("sneaky.py", src)
        self.st.enqueue("sneaky", "latest", "close", "manual"); wait_idle(self.st)
        res = [r for r in self.st.results() if r["strategy"] == "sneaky"][0]
        out = self.st.results_dir / res["id"]
        self.assertEqual(secret.read_text(), "TOP SECRET")      # never written through a link
        for n in ("meta.json.tmp", "output.txt", "ticker_status.csv", "x.csv"):
            self.assertFalse((out / n).is_symlink(), n)
        self.assertIn("Removed links", (out / "output.txt").read_text())
        self.assertNotIn("TOP SECRET", self.page(res["id"]))
        self.assertEqual(out.stat().st_uid, 0)                   # folder taken back from the sandbox user
        time.sleep(4)
        self.assertFalse((out / "late.csv").exists())           # its background process was stopped


if __name__ == "__main__":
    unittest.main()


# ── Candle size: n-day and n-minute candles ───────────────────────────────────
FMFI_FILE = HERE / "testdata" / "fmfi_scanner.py"


def load_fmfi():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fmfi_ref", str(FMFI_FILE))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


class TestCandleSpecs(unittest.TestCase):
    def test_specs_and_labels(self):
        self.assertEqual([sm.candles_label(x) for x in ("1d", "5d", "15m", "60m")], ["Daily", "5-day", "15-min", "60-min"])
        self.assertEqual(sm.candles_spec("5", "days"), "5d")
        self.assertEqual(sm.candles_spec("15", "minutes"), "15m")
        for n, u, msg in (("0", "days", "1–30 days"), ("31", "days", "1–30 days"), ("400", "minutes", "1–390 minutes"),
                          ("x", "days", "whole number"), ("5", "weeks", "days or minutes"), ("2.5", "days", "whole number")):
            with self.assertRaises(ValueError) as cm:
                sm.candles_spec(n, u)
            self.assertIn(msg, str(cm.exception), (n, u))
        self.assertEqual([fvp.intraday_base(m) for m in (1, 3, 5, 10, 15, 45, 60, 90, 7, 390)],
                         [1, 3, 5, 5, 15, 15, 30, 30, 1, 30])

    def test_built_candles_equal_finviz_own_bars(self):
        """Candles built from finer FinViz bars match the independently aggregated fixture bars exactly."""
        bars = {p: fvp.parse_intraday_csv((FINVIZ_SPY / f"SPY_{p}.csv").read_text()) for p in ("i1", "i3", "i5", "i15", "i30")}
        sat = dt.datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
        for target, ref, bases in ((15, "i15", ("i1", "i3", "i5")), (30, "i30", ("i1", "i5", "i15"))):
            want = fvp.minute_candles(bars[ref], target, sat, False)
            self.assertEqual(len(want), 52 if target == 15 else 26)
            for b in bases:
                got = fvp.minute_candles(bars[b], target, sat, False)
                self.assertEqual(sorted(got), sorted(want), (target, b))
                for k in want:
                    for x in "ohlcv":
                        self.assertAlmostEqual(got[k][x], want[k][x], places=6, msg=(target, b, k, x))
        hourly = fvp.minute_candles(bars["i30"], 60, sat, False)
        oct30 = [v for k, v in sorted(hourly.items()) if k.startswith("2026-10-30")]
        self.assertEqual([k[11:] for k in sorted(hourly) if k.startswith("2026-10-30")],
                         ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"])
        daily = fvp.parse_quote_csv((FINVIZ_SPY / "SPY_d.csv").read_text())[0]["2026-10-30"]
        self.assertEqual((oct30[0]["o"], max(c["h"] for c in oct30), min(c["l"] for c in oct30), oct30[-1]["c"]),
                         (daily["o"], daily["h"], daily["l"], daily["c"]))

    def test_stamps_and_forming_candle(self):
        self.assertEqual([fvp.parse_intraday_stamp(x) for x in ("10/30/2026 15:00 PM", "10/30/2026 09:30 AM",
                                                                "10/30/2026 03:00 PM", "10/30/2026 12:15 PM", "x")],
                         [("2026-10-30", 900), ("2026-10-30", 570), ("2026-10-30", 900), ("2026-10-30", 735), None])
        b5 = fvp.parse_intraday_csv((FINVIZ_SPY / "SPY_i5.csv").read_text())
        noon = dt.datetime(2026, 10, 30, 16, 7, tzinfo=UTC)  # 12:07 ET: the 12:00 15-min candle is forming
        self.assertNotIn("2026-10-30 12:00", fvp.minute_candles(b5, 15, noon, False))
        self.assertIn("2026-10-30 11:45", fvp.minute_candles(b5, 15, noon, False))
        self.assertIn("2026-10-30 12:00", fvp.minute_candles(b5, 15, noon, True))
        self.assertFalse([k for k in fvp.minute_candles(b5, 5, noon, True) if not "09:30" <= k[11:] < "16:00"])

    def test_day_candles(self):
        dc = fvp.parse_quote_csv((FINVIZ_SPY / "SPY_d.csv").read_text())[0]
        days = sorted(dc)
        g = fvp.day_candles({"tickers": ["SPY"], "history": {d: {"SPY": b} for d, b in dc.items()}}, 5)
        self.assertEqual(len(g["history"]), len(days) // 5)
        last = g["history"][days[-1]]["SPY"]
        self.assertEqual((last["o"], last["h"], last["l"], last["c"], last["v"]),
                         (dc[days[-5]]["o"], max(dc[d]["h"] for d in days[-5:]), min(dc[d]["l"] for d in days[-5:]),
                          dc[days[-1]]["c"], sum(dc[d]["v"] for d in days[-5:])))
        ref = load_fmfi()  # the script's own grouping (command line) agrees with the agent's
        rd, rh, rl, rc, rv = ref.group_candles(days, [dc[d]["h"] for d in days], [dc[d]["l"] for d in days],
                                               [dc[d]["c"] for d in days], [dc[d]["v"] for d in days], 5)
        self.assertEqual(rd, sorted(g["history"]))
        self.assertEqual(rh, [g["history"][d]["SPY"]["h"] for d in rd])


class TestCandleRuns(unittest.TestCase):
    """End to end through the StrategyManager: the candle choice reaches the strategy and gives the same
    numbers as computing the candles independently."""

    @classmethod
    def setUpClass(cls):
        cls.fake = FakeFinviz()
        cls.data = Path(tempfile.mkdtemp())
        reports = cls.data / "reports"; reports.mkdir(parents=True)
        cls.tickers = ["S%03d" % i for i in range(30)] + ["NOPE"]
        make_scan(reports, "2026-10-25_120003", cls.tickers)
        make_scan(reports, "2026-10-30_133008", cls.tickers, run_line="Run: 2026-10-30 13:30 PDT")
        env = {"FINVIZ_API_TOKEN": FV_TOKEN, "FINVIZ_BASE_URL": cls.fake.url, "FINVIZ_PRICE_INTERVAL": "0",
               "STRATEGY_TIMEOUT_SEC": "60"}
        cls.st = sm.StrategyManager(cls.data, reports, sv.ZoneInfo("America/Los_Angeles"), env, log=lambda m: None)
        cls.st.finviz.client._sleep = lambda s: None
        cls.msg = cls.st.upload_strategy("fmfi_scanner.py", FMFI_FILE.read_bytes())
        cls.st.upload_strategy("v5.py", V5_FILE.read_bytes())
        cls.ref = load_fmfi()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close(); shutil.rmtree(cls.data, ignore_errors=True)

    def run_one(self, name, scan, candles, mode="close"):
        before = {m["id"] for m in self.st.results()}
        self.st.enqueue(name, scan, mode, "manual", candles)
        wait_idle(self.st)
        res = [m for m in self.st.results() if m["id"] not in before][0]
        out = self.st.results_dir / res["id"]
        rows = list(csv.DictReader((out / "signals.csv").open())) if (out / "signals.csv").is_file() else []
        return res, (out / "output.txt").read_text(), {r["Ticker"]: r for r in rows}, out

    def reference(self, ticker_bars, n_days=1):
        """FMFI computed directly from (day, bar) pairs, grouped with the script's own function."""
        days = [d for d, _ in ticker_bars]
        h, l, c, v = ([float(b[k]) for _, b in ticker_bars] for k in ("h", "l", "c", "v"))
        g = self.ref.group_candles(days, h, l, c, v, n_days)
        return self.ref.evaluate(*g, 3, 14, 7, 1)

    def test_upload_reports_candle_support(self):
        self.assertTrue(self.st.meta()["fmfi_scanner"].get("candles"))
        self.assertNotIn("candles", self.st.meta()["v5"])

    def test_five_day_candles(self):
        res, log, rows, out = self.run_one("fmfi_scanner", "2026-10-25_120003", "5d")
        self.assertEqual((res["status"], res["candles"], res["asof"]), ("ok", "5d", "2026-10-23"), log)
        self.assertIn("5-day candles", log)
        self.assertIn("Built 5-day candles from", log)
        checked = 0
        for t in self.tickers[:-1]:
            bars = [(d, MARKET[d][t]) for d in sorted(MARKET) if d <= "2026-10-23" and t in MARKET[d]]
            want = self.reference(bars, 5)
            got = rows[t]
            self.assertEqual(got["Candles"], "5-day")
            self.assertEqual(got["As_Of"], want["As_Of"])
            self.assertAlmostEqual(float(got["Original_MFI"]), want["Original_MFI"], places=3, msg=t)
            self.assertAlmostEqual(float(got["Fourier_MF"]), want["Fourier_MF"], places=3, msg=t)
            self.assertEqual(got["Status"] == "BUY", want["Buy_In_Lookback"], t)
            checked += 1
        self.assertEqual(checked, 30)
        self.assertEqual(rows["NOPE"]["Status"], "NOT IN PRICE CACHE")
        # daily differs from 5-day (sanity: the option really changes the input)
        res1, _, rows1, _ = self.run_one("fmfi_scanner", "2026-10-25_120003", "1d")
        self.assertEqual(rows1["S000"]["Candles"], "Daily")
        self.assertNotEqual(rows1["S000"]["Original_MFI"], rows["S000"]["Original_MFI"])

    def test_fifteen_minute_candles(self):
        res, log, rows, out = self.run_one("fmfi_scanner", "2026-10-30_133008", "15m")
        self.assertEqual((res["status"], res["candles"]), ("ok", "15m"), log)
        self.assertEqual(res["asof"], "2026-10-30 15:45")
        self.assertIn("15-minute prices from FinViz", log)  # the largest FinViz bar size that divides 15
        self.assertIn("15-min candles", log)
        sat = dt.datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
        for t in ("S000", "S007", "S029"):
            candles = fvp.minute_candles(fvp.parse_intraday_csv(intraday_csv(t, "i15")), 15, sat, False)
            keys = sorted(candles)
            want = self.ref.evaluate(keys, *([candles[k][x] for k in keys] for x in "hlcv"), 3, 14, 7, 1)
            self.assertEqual(rows[t]["As_Of"], "2026-10-30 15:45")
            self.assertEqual(rows[t]["Candles"], "15-min")
            self.assertAlmostEqual(float(rows[t]["Original_MFI"]), want["Original_MFI"], places=3, msg=t)
            self.assertAlmostEqual(float(rows[t]["Fourier_MF"]), want["Fourier_MF"], places=3, msg=t)
        # the cache handed to the strategy has regular-session candle starts only
        cache = json.loads((out / "history_cache.json").read_text())
        self.assertFalse([k for k in cache["history"] if not "09:30" <= k[11:] <= "15:45"])
        self.assertEqual(rows["NOPE"]["Status"], "NOT IN PRICE CACHE")

    def test_ten_minute_candles_grouped_from_five(self):
        res, log, rows, _ = self.run_one("fmfi_scanner", "2026-10-30_133008", "10m")
        self.assertEqual((res["status"], res["asof"]), ("ok", "2026-10-30 15:50"), log)
        self.assertIn("5-minute prices from FinViz", log)
        sat = dt.datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
        candles = fvp.minute_candles(fvp.parse_intraday_csv(intraday_csv("S003", "i5")), 10, sat, False)
        keys = sorted(candles)
        self.assertEqual(keys[:3], ["2026-10-29 09:30", "2026-10-29 09:40", "2026-10-29 09:50"])
        want = self.ref.evaluate(keys, *([candles[k][x] for k in keys] for x in "hlcv"), 3, 14, 7, 1)
        self.assertAlmostEqual(float(rows["S003"]["Original_MFI"]), want["Original_MFI"], places=3)
        self.assertAlmostEqual(float(rows["S003"]["Fourier_MF"]), want["Fourier_MF"], places=3)

    def test_minutes_on_an_old_scan_explain_missing_history(self):
        res, log, _, _ = self.run_one("fmfi_scanner", "2026-10-25_120003", "15m")
        self.assertEqual(res["status"], "error")
        self.assertIn("No 15-min candles on or before 2026-10-25", log)

    def test_unsupported_strategy_runs_daily_with_a_note(self):
        res, log, rows, _ = self.run_one("v5", "2026-10-25_120003", "5d")
        self.assertEqual((res["status"], res["candles"]), ("ok", "1d"), log)
        self.assertIn("this strategy only works on daily candles", log)

    def test_dedupe_includes_candles(self):
        st = self.st
        with st._queue_lock:
            pass
        a = st.enqueue("fmfi_scanner", "2026-10-25_120003", "close", "manual", "5d")
        b = st.enqueue("fmfi_scanner", "2026-10-25_120003", "close", "manual", "5d")
        c = st.enqueue("fmfi_scanner", "2026-10-25_120003", "close", "manual", "1d")
        wait_idle(st)
        self.assertIn("already queued", b)
        self.assertNotIn("already queued", c)


class TestCandleSchedules(unittest.TestCase):
    def test_schedule_keeps_candles_and_dispatch_passes_them(self):
        import schedules as sc
        tmp = Path(tempfile.mkdtemp())
        try:
            store = sc.ScheduleStore(tmp / "s.json", sv.ZoneInfo("America/Los_Angeles"))
            now = dt.datetime(2026, 10, 26, 20, 0, tzinfo=UTC)
            jobs = store.add_many(["fmfi"], "latest", "close", "weekdays", "", ["13:10", "09:40"], now=now,
                                  candles="15m")
            self.assertEqual({j.get("candles") for j in jobs}, {"15m"})
            daily = store.add(["fmfi"], "latest", "close", "weekdays", "", "10:00", now=now)
            self.assertNotIn("candles", daily)
            calls = []
            sc.dispatch(jobs[0], lambda *a: calls.append(a) or [], lambda: (None, ""), lambda *a: None, lambda m: None)
            sc.dispatch(daily, lambda *a: calls.append(a) or [], lambda: (None, ""), lambda *a: None, lambda m: None)
            self.assertEqual(calls, [(["fmfi"], "latest", "close", "scheduled", "15m"),
                                     (["fmfi"], "latest", "close", "scheduled")])
        finally:
            shutil.rmtree(tmp)
