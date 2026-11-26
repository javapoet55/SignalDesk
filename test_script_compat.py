"""Whole-script strategies written for the Mac (like daily_scanner.py): they import the
cache_io helper, decide "today" from the clock, and write several CSVs."""
import csv
import datetime as dt
import importlib
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import strategy_runner as sr  # noqa: E402
import test_strategies as ts  # noqa: E402

# Same shape as daily_scanner.py: cache_io import, a clock-based day, a self-download when the day is
# missing from the cache, a cache save, and two result CSVs (+ .txt) under OUT.
MAC_SCRIPT = '''
from cache_io import atomic_save_cache, load_cache_file
import csv
from datetime import datetime, timedelta
OUT        = '/Users/sri/stock data'
CACHE_FILE = f'{OUT}/history_cache.json'

def last_trading_day():
    d = datetime.utcnow().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime('%Y-%m-%d')

def main():
    scan_dt = last_trading_day()
    cache = load_cache_file(CACHE_FILE)
    if scan_dt not in cache['history']:
        open(f'{OUT}/FETCHED_{scan_dt}.flag', 'w').write('tried to download')
        raise SystemExit(0)
    cache['note'] = 'saved'
    atomic_save_cache(CACHE_FILE, cache)
    tag = f"{scan_dt}_{datetime.now().strftime('%H%M%S')}"
    days = sorted(cache['history'])
    last, prev = cache['history'][days[-1]], cache['history'][days[-2]]
    up = [t for t in cache['tickers'] if t in last and t in prev and last[t]['c'] > prev[t]['c']]
    down = [t for t in cache['tickers'] if t in last and t in prev and last[t]['c'] < prev[t]['c']]
    for name, picks, sig in (('up', up[:3], 'TOP'), ('down', down[:2], 'BUY')):
        with open(f'{OUT}/daily_{name}_signals_{tag}.csv', 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['Ticker', 'Date', 'Signal'] + (['Extra'] if name == 'down' else []))
            w.writeheader()
            w.writerows({'Ticker': t, 'Date': scan_dt, 'Signal': sig, **({'Extra': 1} if name == 'down' else {})}
                        for t in picks)
        open(f'{OUT}/daily_{name}_signals_{tag}.txt', 'w').write(', '.join(picks))
'''


class TestMacScripts(unittest.TestCase):
    setUp = ts.TestScriptStrategies.setUp          # same server set-up as the existing script tests,
    tearDown = ts.TestScriptStrategies.tearDown    # without running those tests a second time
    run_one = ts.TestScriptStrategies.run_one
    page = ts.TestScriptStrategies.page

    def test_cache_io_import_today_and_two_csvs(self):
        msg = self.st.upload_strategy("mac_scanner.py", MAC_SCRIPT.encode())
        self.assertIn("(whole script)", msg)
        res, out = self.run_one("mac_scanner")
        log = (out / "output.txt").read_text()
        self.assertEqual(res["asof"], "2026-10-23")
        self.assertIn("Dates: the script's 'today' is the as-of day 2026-10-23", log)
        self.assertEqual(list(out.glob("FETCHED_*")), [], log)        # never tried to download a day itself
        self.assertEqual(json.loads((out / "script_cache.json").read_text())["note"], "saved")  # cache_io saved
        rows = list(csv.DictReader((out / "signals.csv").open()))
        own = {p.name.split("_")[1]: list(csv.DictReader(p.open())) for p in out.glob("daily_*_signals_*.csv")}
        self.assertEqual(sorted(own), ["down", "up"])
        self.assertEqual(len(rows), len(own["up"]) + len(own["down"]))
        self.assertEqual({r["Source"].split("_")[1] for r in rows}, {"up", "down"})
        self.assertTrue(all(r["Date"] == "2026-10-23" for r in rows))
        self.assertIn("combined into signals.csv", log)
        self.assertEqual(res["counts"], {"rows": len(rows)})
        import server
        self.assertEqual(sorted(server.signal_tickers(out)), sorted({r["Ticker"] for r in rows}))
        page = self.page(res["id"])
        for r in rows:
            self.assertIn(r["Ticker"], page)


class TestPinToday(unittest.TestCase):
    def mod(self, src):
        m = types.ModuleType("m")
        exec(src, m.__dict__)
        return m

    def test_every_import_style(self):
        m = self.mod("from datetime import date, datetime\nimport datetime as dtm\n"
                     "def f():\n    return (date.today(), datetime.now().date(), datetime.utcnow().date(),\n"
                     "            datetime.today().date(), dtm.date.today(), datetime.now(dtm.timezone.utc).date(),\n"
                     "            dtm.datetime.now().date())\n")
        self.assertEqual(sorted(sr.pin_today(m, "2026-10-23")), ["date", "datetime", "dtm"])
        self.assertEqual(set(m.f()), {dt.date(2026, 10, 23)})
        plain = self.mod("import datetime\ndef f():\n    return datetime.date.today(), datetime.datetime.utcnow().date()\n")
        self.assertEqual(sr.pin_today(plain, "2026-10-23"), ["datetime"])
        self.assertEqual(set(plain.f()), {dt.date(2026, 10, 23)})
        self.assertIs(dt.date.today().__class__, dt.date)  # nothing outside the script changes
        self.assertNotEqual(dt.date.today(), dt.date(2026, 10, 23))

    def test_time_of_day_and_dates_still_work(self):
        m = self.mod("from datetime import datetime, timedelta\n")
        sr.pin_today(m, "2026-10-23")
        now, real = m.datetime.now(), dt.datetime.now()
        self.assertEqual((now.hour, now.minute), (real.hour, real.minute))
        self.assertEqual((m.datetime.utcnow().date() - m.timedelta(days=1)).isoformat(), "2026-10-22")
        self.assertEqual(m.datetime.strptime("2026-01-02", "%Y-%m-%d").year, 2026)
        self.assertEqual(sr.pin_today(self.mod("x = 1\n"), "2026-10-23"), [])


class TestCombine(unittest.TestCase):
    def test_combine_and_fallback(self):
        d = Path(tempfile.mkdtemp())
        try:
            a, b, c = d / "a.csv", d / "b.csv", d / "c.csv"
            a.write_text("Ticker,X\nAAA,1\n"); b.write_text("Ticker,Y\nBBB,2\nCCC,3\n"); c.write_text("Name\nzzz\n")
            self.assertEqual(sr.combine_csvs([a, b], d / "out.csv"), 3)
            rows = list(csv.DictReader((d / "out.csv").open()))
            self.assertEqual([(r["Ticker"], r["X"], r["Y"], r["Source"]) for r in rows],
                             [("AAA", "1", "", "a"), ("BBB", "", "2", "b"), ("CCC", "", "3", "b")])
            self.assertIsNone(sr.combine_csvs([a, c], d / "out2.csv"))  # no Ticker column: not combined
            self.assertFalse((d / "out2.csv").exists())
        finally:
            shutil.rmtree(d)

    def test_cache_io_is_shipped(self):
        cio = importlib.import_module("cache_io")
        self.assertTrue(callable(cio.load_cache_file) and callable(cio.atomic_save_cache))


if __name__ == "__main__":
    unittest.main()
