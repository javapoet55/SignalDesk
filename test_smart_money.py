"""Smart Money Strength scanner (testdata/smart_money_strength.py) and the agent's TIMEFRAMES option."""
import contextlib
import csv
import datetime as dt
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import strategies as sm  # noqa: E402
import test_strategies as ts  # noqa: E402

SMS_FILE = HERE / "testdata" / "smart_money_strength.py"


def load():
    spec = importlib.util.spec_from_file_location("sms_under_test", SMS_FILE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SMS = load()
ET = dt.timezone(dt.timedelta(hours=-4))  # New York in late October (EDT)

# Real TradingView bars, NASDAQ:AAPL, Oct 30 2026, regular session (TradingView MCP get_ohlcv).
TV = {
    "30m": dict(t=[1793367000, 1793368800, 1793370600, 1793372400, 1793374200, 1793376000, 1793377800, 1793379600,
                   1793381400, 1793383200, 1793385000, 1793386800, 1793388600],
                o=[346.63, 345.29, 346.3, 348.495, 347.69, 346.94, 347.495, 346.98, 346.56, 344.59, 343.14, 343.29, 343.52],
                h=[348.7696, 346.48, 349.19, 348.7, 347.88, 347.72, 347.97, 347.2199, 346.6899, 344.705, 343.84, 343.86, 346],
                l=[344.13, 344.89, 346.19, 347.41, 346.73, 345.6711, 346.83, 346.245, 344.31, 334.35, 342.35, 342.61, 343.14],
                c=[345.33, 346.33, 348.48, 347.69, 346.925, 347.5, 346.955, 346.55, 344.585, 343.1342, 343.275, 343.52, 344.8],
                v=[5151340, 2278836, 2769647, 1556060, 1757246, 2116418, 1804285, 1944115, 2139627, 3052252, 2007794,
                   2042505, 5131352]),
    "1h": dict(t=[1793367000, 1793370600, 1793374200, 1793377800, 1793381400, 1793385000, 1793388600],
               o=[346.63, 346.3, 347.69, 347.495, 346.56, 343.14, 343.52], h=[348.7696, 349.19, 347.88, 347.97, 346.6899, 343.86, 346],
               l=[344.13, 346.19, 345.6711, 346.245, 334.35, 342.35, 343.14], c=[346.33, 347.69, 347.5, 346.55, 343.1342, 343.52, 344.8],
               v=[7430176, 4325707, 3873664, 3748400, 5191879, 4050299, 5131352]),
    "4h": dict(t=[1793367000, 1793381400], o=[346.63, 346.56], h=[349.19, 346.6899], l=[344.13, 334.35], c=[346.55, 344.8],
               v=[19377947, 14373530]),
}


def tv_bars(name):
    d = TV[name]
    out = []
    for i, t in enumerate(d["t"]):
        w = dt.datetime.fromtimestamp(t, ET)
        out.append({"day": w.date().isoformat(), "start": w.hour * 60 + w.minute, "o": d["o"][i], "h": d["h"][i],
                    "l": d["l"][i], "c": d["c"][i], "v": float(d["v"][i])})
    return out


class TestAgainstTradingView(unittest.TestCase):
    def test_hours_and_four_hours_match_tradingview(self):
        m30 = tv_bars("30m")
        for name, minutes in (("1h", 60), ("4h", 240)):
            got = SMS.regroup(m30, minutes)
            want = tv_bars(name)
            self.assertEqual(len(got), len(want), name)
            for g, w in zip(got, want):
                self.assertEqual((g["day"], g["start"]), (w["day"], w["start"]), name)
                for k in "ohlcv":
                    self.assertAlmostEqual(g[k], w[k], places=6, msg=f"{name} {k}")


def mk(day, start, c, v=1000.0, spread=0.001):
    return {"day": day, "start": start, "o": c, "h": c * (1 + spread), "l": c * (1 - spread), "c": c, "v": v}


class TestStrength(unittest.TestCase):
    def series(self, trend_up=True, days=40):
        """Steadily rising (or falling) prices on every timeframe; the last bar is the as-of day."""
        sgn = 1 if trend_up else -1
        daily = []
        d0 = dt.date(2026, 9, 1)
        p = 100.0
        dates = []
        d = d0
        while len(dates) < days:
            if d.weekday() < 5:
                dates.append(d.isoformat())
            d += dt.timedelta(days=1)
        m1, m30 = [], []
        for i, day in enumerate(dates):
            for k in range(13):
                p *= 1 + sgn * 0.001
                m30.append(mk(day, 570 + 30 * k, p))
            if i >= days - 3:  # 1-minute path that walks between this day's 30-minute closes
                prev = m30[-14]["c"] if len(m30) > 13 else m30[-13]["c"] / (1 + sgn * 0.001)
                for k in range(13):
                    target = m30[-13 + k]["c"]
                    for j in range(30):
                        q = prev + (target - prev) * (j + 1) / 30
                        m1.append(mk(day, 570 + 30 * k + j, q))
                    prev = target
            daily.append({"day": day, "o": p, "h": p * 1.002 if trend_up else p * 1.05,
                          "l": p * 0.95 if trend_up else p * 0.998,
                          "c": p, "v": 1e6})
        # one price for "close" everywhere: the last daily close
        last = daily[-1]["c"]
        if m1:
            m1[-1] = dict(m1[-1], c=last)
        m30[-1] = dict(m30[-1], c=last)
        return daily, m1, m30

    def test_all_up_is_100_all_down_is_minus_100(self):
        close, tr, notes = SMS.strength_for(*self.series(True))
        self.assertEqual(tr, {tf: 1 for tf in SMS.TF_ORDER}, notes)
        close, tr, notes = SMS.strength_for(*self.series(False))
        self.assertEqual(tr, {tf: -1 for tf in SMS.TF_ORDER}, notes)

    def test_rows_status_86_is_buy_71_is_not(self):
        daily, m1, m30 = self.series(True)
        asof = daily[-1]["day"]

        def hist(bars):
            return {f"{b['day']} {b['start'] // 60:02d}:{b['start'] % 60:02d}": {"X": b} for b in bars}
        cache = {"tickers": ["X"], "history": {b["day"]: {"X": b} for b in daily},
                 "timeframes": {"1m": hist(m1), "30m": hist(m30)}}
        rows = self.run_rows(cache, asof)
        self.assertEqual((rows["X"]["Strength"], rows["X"]["Status"], rows["X"]["Up_TFs"]), (100, "BUY", 7))
        # daily: close between EMA20 and VWAP -> mixed (0): 6 of 7 -> 86 -> still BUY
        last = dict(daily[-1]); last["h"] = last["c"] * 1.5  # VWAP (h+l+c)/3 now above the close
        cache["history"][asof] = {"X": last}
        rows = self.run_rows(cache, asof)
        self.assertEqual((rows["X"]["Strength"], rows["X"]["Status"], rows["X"]["D"]), (86, "BUY", "━"))
        # daily below both EMA and VWAP -> -1: 6 - 1 = 5 -> 71 -> not a BUY
        last = dict(daily[-1]); last["c"] = last["l"] = daily[-25]["c"] * 0.5; last["h"] = last["c"] * 1.01
        cache["history"][asof] = {"X": last}
        rows = self.run_rows(cache, asof)
        self.assertEqual(rows["X"]["D"], "▼")
        self.assertLess(rows["X"]["Strength"], 85)
        self.assertEqual(rows["X"]["Status"], "NO SIGNAL")

    def test_missing_minute_bars_count_as_zero_and_say_so(self):
        daily, _, _ = self.series(True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rows = SMS.finviz_signals(["X", "NOPE"], daily[-1]["day"],
                                      {"tickers": ["X"], "history": {b["day"]: {"X": b} for b in daily}})
        r = {x["Ticker"]: x for x in rows}
        self.assertEqual((r["X"]["Strength"], r["X"]["Status"]), (14, "NO SIGNAL"))  # only D counts
        self.assertEqual(r["NOPE"]["Status"], "NOT IN PRICE CACHE")
        self.assertIn("minute bars are missing", out.getvalue())

    def test_vwap_restarts_each_session(self):
        bars = [mk("2026-10-29", 570, 100.0, v=1e6), mk("2026-10-30", 570, 10.0, v=100.0),
                mk("2026-10-30", 600, 20.0, v=300.0)]
        hlc3 = lambda b: (b["h"] + b["l"] + b["c"]) / 3  # noqa: E731
        want = (hlc3(bars[1]) * 100 + hlc3(bars[2]) * 300) / 400  # only Oct 30
        self.assertAlmostEqual(SMS.session_vwap(bars), want)
        self.assertIsNone(SMS.session_vwap([mk("2026-10-30", 570, 10.0, v=0.0)]))

    def test_pine_ema_seed(self):
        self.assertIsNone(SMS.pine_ema(list(range(19))))
        self.assertAlmostEqual(SMS.pine_ema(list(range(20))), 9.5)
        self.assertAlmostEqual(SMS.pine_ema(list(range(20)) + [30.0]), 30 * 2 / 21 + 9.5 * 19 / 21)

    def run_rows(self, cache, asof):
        with contextlib.redirect_stdout(io.StringIO()):
            return {r["Ticker"]: r for r in SMS.finviz_signals(list(cache["tickers"]), asof, cache)}


def market_through_oct30():
    """The test market (ends Oct 23) extended to Oct 30, so daily and minute bars share their last day."""
    market = {d: dict(bars) for d, bars in ts.MARKET.items()}
    last = market[max(market)]
    for day in ("2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"):
        market[day] = {s: dict(b, c=round(b["c"] * 1.01, 4), h=round(b["h"] * 1.012, 4)) for s, b in last.items()}
        last = market[day]
    return market


class TestAgentTimeframes(unittest.TestCase):
    def setUp(self):
        self.fake = ts.FakeFinviz(market=market_through_oct30())
        self.data = Path(tempfile.mkdtemp())
        reports = self.data / "reports"; reports.mkdir(parents=True)
        self.tickers = ["S%03d" % i for i in range(12)] + ["NOPE"]
        ts.make_scan(reports, "2026-10-30_133008", self.tickers, run_line="Run: 2026-10-30 13:30 PDT")
        env = {"FINVIZ_API_TOKEN": ts.FV_TOKEN, "FINVIZ_BASE_URL": self.fake.url, "FINVIZ_PRICE_INTERVAL": "0",
               "STRATEGY_TIMEOUT_SEC": "120"}
        self.st = sm.StrategyManager(self.data, reports, sm.dt.timezone.utc, env, log=lambda m: None)
        self.st.finviz.client._sleep = lambda s: None

    def tearDown(self):
        self.fake.close(); shutil.rmtree(self.data, ignore_errors=True)

    def run_one(self, name):
        self.st.enqueue(name, "latest", "close", "manual"); ts.wait_idle(self.st)
        res = [r for r in self.st.results() if r["strategy"] == name][0]
        return res, self.st.results_dir / res["id"]

    def test_upload_run_and_independent_recompute(self):
        self.assertIn("custom strategy", self.st.upload_strategy("smart_money_strength.py", SMS_FILE.read_bytes()))
        self.assertEqual(self.st.meta()["smart_money_strength"]["timeframes"], ["1m", "30m", "1d"])
        res, out = self.run_one("smart_money_strength")
        log = (out / "output.txt").read_text()
        self.assertEqual(res["status"], "ok", log)
        self.assertEqual(res["asof"], "2026-10-30")
        self.assertIn("Getting 1-minute prices", log)
        self.assertIn("Getting 30-minute prices", log)
        cache = json.loads((out / "history_cache.json").read_text())
        self.assertEqual(sorted(cache["timeframes"]), ["1m", "30m"])
        for tf in ("1m", "30m"):
            keys = sorted(cache["timeframes"][tf])
            self.assertTrue(keys and keys[-1][:10] <= "2026-10-30" and keys[0][11:16] >= "09:30", tf)
            self.assertTrue(all("09:30" <= k[11:16] < "16:00" for k in keys), tf)  # regular session only
        with (out / "signals.csv").open() as fh:
            rows = {r["Ticker"]: r for r in csv.DictReader(fh)}
        self.assertEqual(rows["NOPE"]["Status"], "NOT IN PRICE CACHE")
        fresh = load()  # recompute from the saved cache with a separately loaded copy
        with contextlib.redirect_stdout(io.StringIO()):
            again = {r["Ticker"]: r for r in fresh.finviz_signals(self.tickers, "2026-10-30", cache)}
        for t in self.tickers:
            self.assertEqual(rows[t]["Strength"], str(again[t]["Strength"]), t)
            self.assertEqual(rows[t]["Status"], again[t]["Status"], t)
        self.assertTrue(any(rows[t]["1M"] != "━" for t in self.tickers[:12]), "intraday trends were computed")

    def test_without_timeframes_no_minute_downloads(self):
        self.st.upload_strategy("plain.py", b"def finviz_signals(t, a, c):\n    return [{'Ticker': x} for x in t]\n")
        res, out = self.run_one("plain")
        self.assertEqual(res["status"], "ok")
        self.assertFalse(any("p=i" in r for r in self.fake.requests))
        self.assertNotIn("timeframes", json.loads((out / "history_cache.json").read_text()))

    def test_bad_timeframes_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self.st.upload_strategy("bad.py", b"TIMEFRAMES = ['2h']\ndef finviz_signals(t, a, c):\n    return []\n")
        self.assertIn("TIMEFRAMES must be", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
