"""VectorBT PRO indicator page: form parsing, saved-price reading, engine results, API models, page and routes."""

import csv
import io
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import backtests
import vbt_indicators as vi
from test_server import LiveServer

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

RID = "2026-10-30_160000_daily"
KEY = "abc00000-0000-0000-0000-0000000000aa"


def sessions(count):
    from paper_trading.market import exchange
    return [s.date().isoformat() for s in exchange().sessions_in_range("2026-01-02", "2026-10-30")][-count:]


def history(count=120, symbols=("ABC", "XYZ")):
    out = {}
    for i, day in enumerate(sessions(count)):
        out[day] = {}
        for n, symbol in enumerate(symbols):
            close = 50 + 10 * n + i * 0.5 + 3 * math.sin(i / 3)
            out[day][symbol] = dict(o=close - 0.4, h=close + 1.2, l=close - 1.1, c=close, v=1000 + 10 * i)
    return out


def make_run(root, hist, rid=RID, **meta):
    folder = Path(root) / rid
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps(dict(dict(strategy="daily", status="ok", mode="close", candles="1d",
                                                           asof=max(hist)), **meta)))
    (folder / "history_cache.json").write_text(json.dumps({"history": hist}))
    return SimpleNamespace(results_dir=Path(root))


def engine():
    try:
        import vectorbtpro  # noqa: F401
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest("Licensed VectorBT PRO runtime required") from exc
    from vbt_service import indicators
    return indicators


class Catalog(unittest.TestCase):
    def test_site_and_service_limits_match(self):
        from vbt_service import indicators as service  # Loads without the licensed runtime.
        site = {name: tuple((key, default, low, high, whole) for key, _, default, low, high, whole in item["params"])
                for name, item in vi.CATALOG.items()}
        self.assertEqual(site, service.PARAMS)
        self.assertEqual((vi.MAX_SYMBOLS, vi.MAX_INDICATORS, vi.MAX_BARS),
                         (service.MAX_SYMBOLS, service.MAX_INDICATORS, service.MAX_BARS))
        self.assertEqual(set(service.RUNNERS), set(service.PARAMS))

    def test_service_normalize(self):
        from vbt_service.indicators import normalize
        self.assertEqual(normalize("sma", None), {"window": 20})
        self.assertEqual(normalize("bbands", {"alpha": 2.5}), {"window": 20, "alpha": 2.5})
        self.assertEqual(normalize("obv", {}), {})
        for name, params, message in (("zzz", {}, "Unknown indicator"), ("rsi", {"window": 1}, "between 2 and 100"),
                                      ("rsi", {"window": 14.5}, "whole number"), ("rsi", {"span": 3}, "does not take span"),
                                      ("sma", {"window": True}, "must be a number"),
                                      ("macd", {"fast_window": 26, "slow_window": 26}, "shorter than the slow")):
            with self.subTest(name=name, params=params), self.assertRaisesRegex(ValueError, message):
                normalize(name, params)


class FormParsing(unittest.TestCase):
    def test_defaults_lists_and_every_indicator(self):
        self.assertEqual(vi.parse_specs({"use_sma": "1", "sma_window": "20, 50,20"}),
                         [dict(name="sma", params={"window": 20}), dict(name="sma", params={"window": 50})])
        self.assertEqual(vi.parse_specs({"use_rsi": "1", "rsi_window": ""}), [dict(name="rsi", params={"window": 14})])
        everything = vi.parse_specs({"use_" + name: "1" for name in vi.CATALOG})
        self.assertEqual([s["name"] for s in everything], list(vi.CATALOG))
        self.assertEqual(everything[2]["params"], {"window": 20, "alpha": 2.0})
        self.assertEqual(everything[4]["params"], {"fast_window": 12, "slow_window": 26, "signal_window": 9})
        self.assertEqual(everything[6]["params"], {})
        # An unticked indicator's boxes are ignored, even when they hold nonsense.
        self.assertEqual(len(vi.parse_specs({"use_rsi": "1", "sma_window": "abc"})), 1)

    def test_rejections(self):
        for fields, message in (
                ({}, "Tick at least one indicator"),
                ({"use_sma": "1", "sma_window": "1"}, "Simple moving average.*whole number from 2 to 250"),
                ({"use_sma": "1", "sma_window": "20,x"}, "whole number from 2 to 250"),
                ({"use_rsi": "1", "rsi_window": "14,21"}, "Relative Strength Index.*whole number"),
                ({"use_bbands": "1", "bbands_alpha": "9"}, "Bollinger Bands.*a number from 0.5 to 5"),
                ({"use_macd": "1", "macd_fast_window": "30"}, "fast window must be shorter"),
                ({"use_sma": "1", "sma_window": ",".join(str(n) for n in range(10, 23))}, "That is 13 indicators"),
        ):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, message):
                vi.parse_specs(fields)

    def test_symbols(self):
        self.assertEqual(vi.parse_symbols(" abc, XYZ ,abc,brk.b "), ["ABC", "XYZ", "BRK.B"])
        for text, message in (("", "at least one symbol"), (",".join(f"T{i}" for i in range(11)), "at most 10"),
                              ("A B", "not a valid symbol"), ("<script>", "not a valid symbol")):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, message):
                vi.parse_symbols(text)


class SavedPrices(unittest.TestCase):
    def prepare(self, hist=None, **fields):
        with tempfile.TemporaryDirectory() as tmp:
            st = make_run(tmp, hist or history())
            return vi.prepare(st, dict(dict(rid=RID, symbols="ABC", use_sma="1"), **fields))

    def test_default_range_sends_all_history_and_shows_six_months(self):
        hist = history(200)
        days = sorted(hist)
        request = self.prepare(hist)
        self.assertEqual((request["kind"], request["price_run"], request["source"]), ("indicators", RID, "Finviz daily prices"))
        bars = request["series"]["ABC"]
        self.assertEqual([b["day"] for b in bars], days)           # everything, so long averages are warmed up
        self.assertEqual(request["display_from"], days[-126])
        self.assertEqual(set(bars[0]), {"day", "open", "high", "low", "close", "volume"})
        self.assertEqual(bars[-1]["close"], hist[days[-1]]["ABC"]["c"])

    def test_chosen_range_in_either_order_keeps_earlier_bars_for_warm_up(self):
        hist = history()
        days = sorted(hist)
        for start, end in ((days[80], days[100]), (days[100], days[80])):
            request = self.prepare(hist, start=start, end=end)
            self.assertEqual(request["display_from"], days[80])
            self.assertEqual(request["series"]["ABC"][-1]["day"], days[100])   # nothing after the end day
            self.assertEqual(request["series"]["ABC"][0]["day"], days[0])

    def test_bad_values_become_missing_and_long_history_is_capped(self):
        hist = history(120)
        days = sorted(hist)
        hist[days[5]]["ABC"].update(h="x", v=-1, o=float("nan"))
        hist[days[6]]["ABC"]["c"] = None                           # no close: the day is left out
        del hist[days[7]]["ABC"]
        bars = self.prepare(hist)["series"]["ABC"]
        by_day = {b["day"]: b for b in bars}
        self.assertEqual((by_day[days[5]]["high"], by_day[days[5]]["volume"], by_day[days[5]]["open"]), (None, None, None))
        self.assertNotIn(days[6], by_day)
        self.assertNotIn(days[7], by_day)
        with patch.object(vi, "MAX_BARS", 50):
            self.assertEqual(len(self.prepare(history(120))["series"]["ABC"]), 50)

    def test_rejections(self):
        hist = history()
        days = sorted(hist)
        for fields, message in ((dict(symbols="ABC,NOPE"), "No saved daily prices for NOPE"),
                                (dict(start=days[3]), "Choose both dates"),
                                (dict(start="10/01/2026", end=days[9]), "YYYY-MM-DD"),
                                (dict(start="2027-01-04", end="2027-02-01"), "saved prices end before 2027-01-04"),
                                (dict(rid="../etc"), "Choose a saved strategy run"),
                                (dict(rid="2026-10-29_160000_other"), "Choose a saved strategy run")):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, message):
                self.prepare(hist, **fields)
        with tempfile.TemporaryDirectory() as tmp:
            st = make_run(tmp, hist, candles="30m")
            with self.assertRaisesRegex(ValueError, "Choose a saved strategy run with daily prices"):
                vi.prepare(st, dict(rid=RID, symbols="ABC", use_sma="1"))
            (Path(tmp) / RID / "history_cache.json").write_text("{not json")
            (Path(tmp) / RID / "meta.json").write_text(json.dumps(dict(status="ok", mode="close")))
            with self.assertRaisesRegex(ValueError, "could not be read"):
                vi.prepare(st, dict(rid=RID, symbols="ABC", use_sma="1"))

    def test_the_run_folder_is_not_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = make_run(tmp, history())
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (Path(tmp) / RID).iterdir()}
            vi.prepare(st, dict(rid=RID, symbols="ABC,XYZ", **{"use_" + n: "1" for n in vi.CATALOG}))
            self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (Path(tmp) / RID).iterdir()}, before)


class Engine(unittest.TestCase):
    """Runs the worker's calculation. Skipped where no VectorBT runtime is installed."""

    def request(self, hist=None, symbols="ABC,XYZ", **fields):
        with tempfile.TemporaryDirectory() as tmp:
            st = make_run(tmp, hist or history())
            return vi.prepare(st, dict(dict(rid=RID, symbols=symbols), **(fields or {"use_" + n: "1" for n in vi.CATALOG})))

    def test_every_indicator_against_hand_calculations(self):
        compute = engine().compute
        request = self.request()
        result = compute(json.loads(json.dumps(request)))             # as the worker receives it
        self.assertTrue(result["engine"].startswith("VectorBT PRO"))
        self.assertEqual(list(result["symbols"]), ["ABC", "XYZ"])
        data = result["symbols"]["ABC"]
        bars = request["series"]["ABC"]
        closes = [b["close"] for b in bars]
        shown = [b["day"] for b in bars if b["day"] >= request["display_from"]]
        self.assertEqual(data["days"], shown)
        self.assertEqual(data["bars_used"], len(bars))
        self.assertEqual(data["close"], [round(c, 6) for c in closes[-len(shown):]])
        lines = {e["name"]: e for e in data["indicators"]}
        self.assertFalse([e for e in data["indicators"] if "error" in e])
        for entry in data["indicators"]:
            for values in entry["outputs"].values():
                self.assertEqual(len(values), len(shown))
        sma = sum(closes[-20:]) / 20
        self.assertAlmostEqual(lines["sma"]["outputs"]["sma"][-1], sma, places=5)
        deviation = math.sqrt(sum((c - sma) ** 2 for c in closes[-20:]) / 20)
        bands = lines["bbands"]["outputs"]
        self.assertAlmostEqual(bands["middle"][-1], sma, places=5)
        self.assertAlmostEqual(bands["upper"][-1], sma + 2 * deviation, places=4)
        self.assertAlmostEqual(bands["lower"][-1], sma - 2 * deviation, places=4)
        ema, k = closes[0], 2 / 21
        for close in closes[1:]:
            ema = close * k + ema * (1 - k)
        self.assertAlmostEqual(lines["ema"]["outputs"]["ema"][-1], ema, delta=0.02)
        macd = lines["macd"]["outputs"]
        self.assertAlmostEqual(macd["hist"][-1], macd["macd"][-1] - macd["signal"][-1], places=5)
        rsi = [v for v in lines["rsi"]["outputs"]["rsi"] if v is not None]
        atr = [v for v in lines["atr"]["outputs"]["atr"] if v is not None]
        self.assertGreater(len(rsi), len(shown) - 20)
        self.assertTrue(all(0 <= v <= 100 for v in rsi))
        self.assertGreater(len(atr), len(shown) - 20)
        self.assertTrue(all(v > 0 for v in atr))
        volumes = [b["volume"] for b in bars]
        obv = volumes[0]
        for i in range(1, len(bars)):
            obv += volumes[i] if closes[i] > closes[i - 1] else -volumes[i] if closes[i] < closes[i - 1] else 0
        self.assertAlmostEqual(lines["obv"]["outputs"]["obv"][-1], obv, places=2)
        # The result must be storable as JSON: no NaN.
        json.dumps(result, allow_nan=False)

    def test_warm_up_gaps_are_empty_not_wrong(self):
        compute = engine().compute
        hist = history(30)
        result = compute(self.request(hist, "ABC", use_sma="1", sma_window="20", start=sorted(hist)[0], end=sorted(hist)[-1]))
        values = result["symbols"]["ABC"]["indicators"][0]["outputs"]["sma"]
        self.assertEqual(values[:19], [None] * 19)
        self.assertTrue(all(v is not None for v in values[19:]))

    def test_one_failing_indicator_does_not_stop_the_others(self):
        compute = engine().compute
        hist = history()
        for day in hist:
            hist[day]["ABC"].update(h=None, v=None)
        result = compute(self.request(hist, "ABC"))
        lines = {e["name"]: e for e in result["symbols"]["ABC"]["indicators"]}
        self.assertIn("needs the daily high for every session", lines["atr"]["error"])
        self.assertIn("needs the daily volume for every session", lines["obv"]["error"])
        self.assertIn("outputs", lines["rsi"])
        self.assertNotIn("outputs", lines["atr"])

    def test_worker_sends_indicator_requests_here_and_backtests_to_the_engine(self):
        engine()
        try:
            from vbt_service import jobs
        except ModuleNotFoundError as exc:      # psycopg or redis is not installed here
            raise unittest.SkipTest(str(exc)) from exc
        request = self.request(symbols="ABC", use_rsi="1")
        self.assertIn("ABC", jobs.run_request(request)["symbols"])
        with patch("vbt_service.engine.calculate", return_value={"ok": 1}) as calculate:
            self.assertEqual(jobs.run_request({"holds": [1]}), {"ok": 1})
            self.assertEqual(jobs.run_request({"kind": "something else"}), {"ok": 1})
        self.assertEqual(calculate.call_count, 2)


class ApiModel(unittest.TestCase):
    def setUp(self):
        try:
            from vbt_service import api
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(str(exc)) from exc
        self.api = api

    def test_a_prepared_request_is_accepted_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = vi.prepare(make_run(tmp, history()), dict(rid=RID, symbols="ABC,XYZ", **{"use_" + n: "1" for n in vi.CATALOG}))
        model = self.api.IndicatorRequest(**json.loads(json.dumps(request)))
        dumped = model.model_dump()
        self.assertEqual(dumped["indicators"], request["indicators"])
        self.assertEqual(dumped["series"], request["series"])
        self.assertEqual(dumped["display_from"], request["display_from"])

    def test_bounds(self):
        from pydantic import ValidationError
        bars = [dict(day="2026-10-01", close=1), dict(day="2026-10-02", close=2)]
        good = dict(kind="indicators", price_run="r", source="s", display_from="2026-10-01", indicators=[dict(name="rsi")],
                    series={"ABC": bars})
        self.api.IndicatorRequest(**good)
        for change in (dict(kind="backtest"), dict(indicators=[]), dict(indicators=[dict(name="rsi")] * 13),
                       dict(indicators=[dict(name="rsi", params=dict(window=1))]), dict(indicators=[dict(name="nope")]),
                       dict(series={}), dict(series={"abc": bars}), dict(series={"ABC": bars[:1]}),
                       dict(series={f"T{i}": bars for i in range(11)}), dict(series={"ABC": bars * 301}),
                       dict(series={"ABC": [dict(day="2026-10-01", close=-1), bars[1]]}), dict(display_from="yesterday")):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.api.IndicatorRequest(**dict(good, **change))

    def test_history_listing_leaves_indicator_jobs_out(self):
        import inspect
        source = inspect.getsource(self.api.history)
        self.assertIn("request->>'kind' IS DISTINCT FROM 'indicators'", source)
        # The per-run lookup matches on 'run'; indicator requests store 'price_run' so they can never match.
        self.assertNotIn("run", self.api.IndicatorRequest.model_fields)


def finished_job(error=None, pending=False):
    """A job as the service returns it, built from a real engine run where a runtime exists."""
    with tempfile.TemporaryDirectory() as tmp:
        request = vi.prepare(make_run(tmp, history()), dict(rid=RID, symbols="ABC,XYZ", **{"use_" + n: "1" for n in vi.CATALOG}))
    inputs = {k: v for k, v in request.items() if k != "series"}
    inputs["symbols"] = list(request["series"])
    if error or pending:
        return dict(inputs=inputs, result=None, error=error, elapsed_seconds=None)
    return dict(inputs=inputs, result=engine().compute(request), error=None, elapsed_seconds=1)


class Rendering(unittest.TestCase):
    def test_result_tables_charts_and_csv(self):
        job = finished_job()
        html = vi.result_html(job, KEY)
        self.assertIn("<h2>Latest values</h2>", html)
        for heading in ("SMA 20", "EMA 20", "Bollinger 20, 2 upper band", "Bollinger 20, 2 lower band", "RSI 14", "MACD 12/26/9",
                        "MACD 12/26/9 signal", "MACD 12/26/9 histogram", "ATR 14", "OBV"):
            self.assertIn(f"<th>{heading}</th>", html)
        self.assertEqual(html.count("<details class='vb-card vi-symbol'"), 2)
        self.assertEqual(html.count("<details class='vb-card vi-symbol' open>"), 1)
        self.assertEqual(html.count("<svg class='vb-plot vi-plot'"), 2 * 5)       # price, RSI, MACD, ATR, OBV for each symbol
        self.assertEqual(html.count("class='vi-guide'"), 2 * 3)                  # RSI 30 and 70, MACD zero
        self.assertNotIn("nan", html.lower())
        self.assertIn(f"/vectorbt/indicators?job={KEY}&amp;download=csv", html)

        rows = list(csv.reader(io.StringIO(vi.export_csv(job["result"]["symbols"]).decode())))
        self.assertEqual(rows[0][:4], ["Symbol", "Date", "Close", "SMA 20"])
        days = job["result"]["symbols"]["ABC"]["days"]
        self.assertEqual(len(rows), 1 + 2 * len(days))
        self.assertEqual(rows[1][:2], ["ABC", days[0]])
        self.assertEqual(float(rows[len(days)][3]), job["result"]["symbols"]["ABC"]["indicators"][0]["outputs"]["sma"][-1])

    def test_gaps_errors_pending_and_failures(self):
        chart = vi.chart(["d1", "d2", "d3", "d4", "d5"], [("x", [None, 1, 2, None, 5], "red", 1)])
        self.assertEqual(chart.count("<polyline"), 1)     # 1-2 is a line
        self.assertEqual(chart.count("<circle"), 1)       # the lone 5 is a dot, never joined across the gap
        self.assertIn("Not enough price history", vi.chart(["d1"], [("x", [None], "red", 1)]))
        data = dict(days=["2026-10-01", "2026-10-02"], close=[1.0, 2.0], indicators=[
            dict(name="atr", params={"window": 14}, error="needs the daily high <b> for every session"),
            dict(name="rsi", params={"window": 14}, outputs={"rsi": [None, 55.0]})])
        section = vi.symbol_section("ABC", data, True)
        self.assertIn("ATR 14 could not be calculated: needs the daily high &lt;b&gt; for every session", section)
        self.assertIn("<h3>RSI 14</h3>", section)
        table = vi.latest_table({"ABC": data})
        self.assertIn("<th>RSI 14</th>", table)
        self.assertNotIn("ATR", table)
        self.assertIn("Calculating…", vi.result_html(finished_job(pending=True), KEY))
        failed = vi.result_html(finished_job(error="'holds' <x>"), KEY)
        self.assertIn("could not be calculated. &#x27;holds&#x27; &lt;x&gt;", failed)
        self.assertIn("redeploy it and run this again", failed)
        self.assertEqual(vi.result_html(None, KEY), "")

    def test_form_defaults_typed_values_and_saved_job(self):
        runs = [dict(id=RID, strategy="daily_scan", asof="2026-10-30")]
        form = vi.controls(runs, RID, vi.selected(None, {}), True, "2026-10-30")
        self.assertIn("name='use_sma' value='1' checked", form)
        self.assertIn("name='use_rsi' value='1' checked", form)
        self.assertIn("name='use_macd' value='1'>", form)
        self.assertIn("name='sma_window' inputmode='decimal' size='7' value='20, 50'", form)
        self.assertIn("max='2026-10-30'", form)
        typed = vi.controls(runs, RID, vi.selected({"symbols": "A<B", "use_obv": "1", "sma_window": "5,9"}, {}), True, None)
        self.assertIn("value='A&lt;B'", typed)
        self.assertIn("name='use_obv' value='1' checked", typed)
        self.assertNotIn("name='use_sma' value='1' checked", typed)
        self.assertIn("value='5,9'", typed)
        inputs = dict(price_run=RID, symbols=["ABC", "XYZ"], indicators=[
            dict(name="sma", params={"window": 20}), dict(name="sma", params={"window": 50}),
            dict(name="bbands", params={"window": 10, "alpha": 2.5})])
        saved = vi.controls(runs, RID, vi.selected(None, inputs), True, None)
        self.assertIn("value='ABC,XYZ'", saved)
        self.assertIn("name='sma_window' inputmode='decimal' size='7' value='20, 50'", saved)
        self.assertIn("name='bbands_alpha' inputmode='decimal' size='7' value='2.5'", saved)
        self.assertNotIn("name='use_rsi' value='1' checked", saved)
        self.assertIn("disabled>▶", vi.controls([], "", {}, True, None))
        self.assertIn("disabled>▶", vi.controls(runs, RID, {}, False, None))


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LiveServer()
        cls.env = patch.dict(os.environ, VBT_SERVICE_URL="http://vbt.invalid", VBT_SERVICE_TOKEN="fixture")
        cls.env.start()
        hist = history()
        results = cls.server.app.strategies.results_dir
        results.mkdir(parents=True, exist_ok=True)
        make_run(results, hist)
        cls.days = sorted(hist)
        cls.origin = {"Origin": f"http://127.0.0.1:{cls.server.port}"}

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.server.close()

    def form(self, **extra):
        return dict(dict(rid=RID, symbols="abc, xyz", use_sma="1", sma_window="20,50", use_rsi="1", rsi_window="14"), **extra)

    def test_empty_page_and_link_from_backtesting(self):
        with patch("backtests.service", return_value=[]):
            response, body = self.server.request("GET", "/vectorbt/indicators")
            self.assertEqual(response.status, 200)
            page = body.decode()
            self.assertIn("<h1>VectorBT PRO Indicators</h1>", page)
            self.assertIn(f"<option value='{RID}' selected>", page)
            self.assertIn("const VB_PENDING=false,VB_POLL_MS=3000,VB_POLL_LIMIT=40;", page)
            _, body = self.server.request("GET", "/vectorbt")
            self.assertIn("href='/vectorbt/indicators'>Indicators</a>", body.decode())
        response, _ = self.server.request("GET", "/vectorbt/indicators?job=not-a-job")
        self.assertEqual(response.status, 400)

    def test_submit_sends_one_request_and_redirects(self):
        sent = []

        def service(path, data=None):
            sent.append((path, data))
            return {"id": KEY}

        with patch("backtests.service", side_effect=service):
            response, _ = self.server.request("POST", "/vectorbt/indicators/run", form=self.form(), headers=self.origin)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/vectorbt/indicators?job=" + KEY)
        self.assertEqual(len(sent), 1)
        path, data = sent[0]
        self.assertEqual(path, "/indicators")
        self.assertEqual(list(data["series"]), ["ABC", "XYZ"])
        self.assertEqual([(s["name"], s["params"]["window"]) for s in data["indicators"]], [("sma", 20), ("sma", 50), ("rsi", 14)])
        self.assertEqual(data["display_from"], self.days[-min(126, len(self.days))])
        json.dumps(data, allow_nan=False)

    def test_form_errors_redisplay_what_was_typed_without_calling_the_service(self):
        with patch("backtests.service") as service:
            response, body = self.server.request("POST", "/vectorbt/indicators/run", headers=self.origin,
                                                 form=self.form(symbols="abc,nope", sma_window="20, 50"))
            self.assertEqual(response.status, 400)
            page = body.decode()
            self.assertIn("No saved daily prices for NOPE in this run.", page)
            self.assertIn("value='abc,nope'", page)
            self.assertIn("name='use_sma' value='1' checked", page)
            self.assertFalse(service.called)
            response, body = self.server.request("POST", "/vectorbt/indicators/run", headers=self.origin, form=dict(rid=RID, symbols="ABC"))
            self.assertIn("Tick at least one indicator.", body.decode())
        response, _ = self.server.request("POST", "/vectorbt/indicators/run", form=self.form(),
                                          headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status, 403)                     # a form posted from another site is refused

    def test_service_outage_on_submit_and_view(self):
        down = ValueError("The backtest service is unavailable. Check the private service connection.")
        with patch("backtests.service", side_effect=down):
            response, body = self.server.request("POST", "/vectorbt/indicators/run", form=self.form(), headers=self.origin)
            self.assertEqual(response.status, 400)
            self.assertIn("The backtest service is unavailable.", body.decode())
            response, body = self.server.request("GET", "/vectorbt/indicators?job=" + KEY)
            self.assertEqual(response.status, 200)
            self.assertIn("Backtest service unavailable. Refresh to retry.", body.decode())
        with patch("backtests.service", side_effect=backtests.JobNotFound("gone")):
            _, body = self.server.request("GET", "/vectorbt/indicators?job=" + KEY)
            self.assertIn("This indicator result was not found.", body.decode())

    def test_a_backtest_job_is_not_shown_as_indicators_and_pending_polls(self):
        backtest = dict(inputs=dict(strategy="daily", symbol="ABC", run=RID, holds=[1]), result={"x": 1}, error=None)
        with patch("backtests.service", return_value=backtest):
            _, body = self.server.request("GET", "/vectorbt/indicators?job=" + KEY)
            self.assertIn("That link is a backtest, not an indicator result.", body.decode())
            response, _ = self.server.request("GET", f"/vectorbt/indicators?job={KEY}&download=csv")
            self.assertEqual(response.status, 400)
        with patch("backtests.service", return_value=finished_job(pending=True)):
            _, body = self.server.request("GET", "/vectorbt/indicators?job=" + KEY)
            page = body.decode()
            self.assertIn("const VB_PENDING=true,VB_POLL_MS=3000", page)
            self.assertIn("Calculating…", page)
            self.assertIn("value='ABC,XYZ'", page)
            response, _ = self.server.request("GET", f"/vectorbt/indicators?job={KEY}&download=csv")
            self.assertEqual(response.status, 400)

    def test_finished_result_page_and_csv(self):
        job = finished_job()
        with patch("backtests.service", return_value=job):
            response, body = self.server.request("GET", "/vectorbt/indicators?job=" + KEY)
            page = body.decode()
            self.assertEqual(response.status, 200)
            self.assertIn("const VB_PENDING=false,", page)
            self.assertIn("<h2>Latest values</h2>", page)
            days = job["result"]["symbols"]["ABC"]["days"]
            self.assertIn(f"name='start' type='date' max='2026-10-30' value='{days[0]}'", page)
            response, body = self.server.request("GET", f"/vectorbt/indicators?job={KEY}&download=csv")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/csv; charset=utf-8")
            self.assertIn(f'filename="indicators-{KEY}.csv"', response.getheader("Content-Disposition"))
            self.assertTrue(body.decode().startswith("Symbol,Date,Close,SMA 20,"))

    def test_other_pages_do_not_list_indicator_jobs_as_backtests(self):
        # The service filters them out of /jobs; the backtest pages render whatever /jobs returns.
        listing = [dict(id=KEY, strategy="daily", symbol="ABC", status="Completed", created="2026-10-30T20:00:00+00:00")]
        with patch("backtests.service", return_value=listing):
            response, body = self.server.request("GET", "/strategies/backtests")
            self.assertEqual(response.status, 200)
            self.assertIn(KEY, body.decode())


@unittest.skipUnless(sync_playwright and os.getenv("PORTAL_BROWSER_TEST"), "optional Chrome suite")
class Browser(unittest.TestCase):
    def test_form_submit_poll_and_result(self):
        server = LiveServer()
        self.addCleanup(server.close)
        results = server.app.strategies.results_dir
        results.mkdir(parents=True, exist_ok=True)
        make_run(results, history())
        done = finished_job()
        state = dict(lookups=0, sent=None)

        def service(path, data=None):
            if path == "/indicators":
                state["sent"] = data
                return {"id": KEY}
            if path == "/jobs":
                return []
            state["lookups"] += 1
            return done if state["lookups"] > 2 else dict(done, result=None)

        with (patch.dict(os.environ, VBT_SERVICE_URL="http://vbt.invalid", VBT_SERVICE_TOKEN="fixture"),
              patch("backtests.service", side_effect=service), patch("vbt_indicators.POLL_SECONDS", 1),
              sync_playwright() as pw):
            browser = pw.chromium.launch(channel="chrome")
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            base = f"http://127.0.0.1:{server.port}"
            page.goto(base + "/vectorbt/indicators")
            page.locator("[name=username]").fill("admin")
            page.locator("[name=password]").fill("pw-123")
            page.locator("form.login-form button[type=submit]").click()
            page.goto(base + "/vectorbt")
            page.get_by_role("link", name="Indicators", exact=True).click()
            page.wait_for_url("**/vectorbt/indicators")
            macd = page.locator(".vi-choice", has_text="MACD")
            self.assertIn("vi-off", macd.get_attribute("class"))
            page.locator("[name=macd_fast_window]").fill("10")           # typing a parameter ticks its indicator
            self.assertTrue(page.locator("[name=use_macd]").is_checked())
            self.assertNotIn("vi-off", macd.get_attribute("class"))
            page.locator("[name=symbols]").fill("abc, xyz")
            page.locator(".vb-run").click()
            page.wait_for_url("**/vectorbt/indicators?job=*")
            self.assertEqual([s["name"] for s in state["sent"]["indicators"]], ["sma", "sma", "rsi", "macd"])
            self.assertEqual(state["sent"]["indicators"][3]["params"]["fast_window"], 10)
            page.get_by_text("Latest values").wait_for(timeout=20000)
            self.assertNotIn("poll=", page.url)
            self.assertEqual(page.locator(".vi-symbol").count(), 2)
            self.assertTrue(page.locator(".vi-symbol").first.locator("svg").first.is_visible())
            self.assertFalse(page.locator(".vi-symbol").nth(1).locator("svg").first.is_visible())
            page.locator(".vi-symbol").nth(1).locator("summary").click()
            self.assertTrue(page.locator(".vi-symbol").nth(1).locator("svg").first.is_visible())
            self.assertEqual(page.locator("[name=symbols]").input_value(), "ABC,XYZ")
            with page.expect_download() as download:
                page.get_by_text("Download CSV").click()
            self.assertEqual(download.value.suggested_filename, f"indicators-{KEY}.csv")
            if os.environ.get("VI_SCREENSHOT"):
                page.screenshot(path=os.environ["VI_SCREENSHOT"], full_page=True)
            self.assertFalse(errors)
            browser.close()


if __name__ == "__main__":
    unittest.main()
