"""Tests for finviz_agent.py (standard library only).

Run:  python -m unittest -v test_finviz_agent.py
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import random
import re
import tempfile
import unittest
import urllib.error
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

import finviz_agent as fa

SECRET = "tok_SuperSecret-123/=+"  # contains characters that change when URL-encoded

SHORT_HEADERS = ["No.", "Ticker", "Company", "Sector", "Industry", "Market Cap", "Perf Week", "Perf Month",
                 "Perf Quart", "Perf Half", "Perf Year", "Perf YTD", "Change from Open", "Gap", "Avg Volume",
                 "Rel Volume", "Price", "Change", "Volume", "Prev Close"]
LONG_HEADERS = ["No.", "Ticker", "Company", "Sector", "Industry", "Market Cap", "Performance (Week)",
                "Performance (Month)", "Performance (Quarter)", "Performance (Half Year)", "Performance (Year)",
                "Performance (YTD)", "Change from Open", "Gap", "Average Volume", "Relative Volume", "Price",
                "Change", "Volume", "Previous Close"]


def make_csv(stocks, style="short", pct_sign=True, fraction=False, prev_close=True):
    """stocks: list of (ticker, change_pct or None, week_pct or None, prev_close[, company])."""
    headers = list(SHORT_HEADERS if style == "short" else LONG_HEADERS)
    if not prev_close:
        headers = headers[:-1]

    def pct(v):
        if v is None:
            return "-"
        if fraction:
            return f"{v / 100:.6f}"
        return f"{v:.2f}%" if pct_sign else f"{v:.2f}"

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for n, s in enumerate(stocks, 1):
        ticker, ch, wk, prev = s[:4]
        company = s[4] if len(s) > 4 else f"{ticker} Corp"
        price = round(prev * (1 + (ch or 0) / 100), 2)
        row = [n, ticker, company, "Technology", "Software", "1234.56", pct(wk), pct(1.0), pct(2.0), pct(3.0),
               pct(4.0), pct(5.0), pct(0.5), pct(0.1), "1.2M", "1.50", f"{price:.2f}", pct(ch), "2,345,678"]
        if prev_close:
            row.append(f"{prev:.2f}")
        w.writerow(row)
    return buf.getvalue()


def random_stocks(n, seed):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        ch = None if rng.random() < 0.05 else round(rng.uniform(-30, 60), 2)
        wk = None if rng.random() < 0.05 else round(rng.uniform(-40, 90), 2)
        out.append((f"T{seed}{i:03d}", ch, wk, round(rng.uniform(1, 400), 2)))
    return out


def brute_force_top(stocks, idx, n, include_nonpositive=False):
    vals = [(s[0], s[idx]) for s in stocks if s[idx] is not None]
    if not include_nonpositive:
        vals = [v for v in vals if v[1] > 0]
    vals.sort(key=lambda v: (-v[1], v[0]))
    return [t for t, _ in vals[:n]]


class FakeResponse:
    def __init__(self, body: str, ctype="text/csv"):
        self._body = body.encode("utf-8")
        self.headers = {"Content-Type": ctype}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(url, code, reason="err", headers=None):
    return urllib.error.HTTPError(url, code, reason, headers or {}, None)


class FakeOpener:
    """Scripted urlopen: each call pops the next action (response or exception)."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.urls = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        action = self.actions.pop(0)
        if callable(action):
            action = action(req.full_url)
        if isinstance(action, Exception):
            raise action
        return action


def client_with(opener, **kw):
    sleeps = []
    c = fa.FinvizClient(SECRET, urlopen=opener, sleep=sleeps.append, clock=lambda: 0.0, **kw)
    return c, sleeps


# --------------------------------------------------------------------------
class TestParsing(unittest.TestCase):
    def test_parse_number(self):
        self.assertEqual(fa.parse_number("12.34%"), (12.34, True))
        self.assertEqual(fa.parse_number("-3.5%"), (-3.5, True))
        self.assertEqual(fa.parse_number("1,234.50"), (1234.5, False))
        self.assertEqual(fa.parse_number("$7.25"), (7.25, False))
        for missing in ["", "-", "N/A", "  "]:
            self.assertEqual(fa.parse_number(missing)[0], None)
        self.assertIsNone(fa.parse_number("abc")[0])

    def test_both_header_styles_map_every_column(self):
        stocks = random_stocks(20, 1)
        for style in ("short", "long"):
            p = fa.parse_export(make_csv(stocks, style=style), ["ticker", "change", "perf_week"])
            self.assertEqual(set(p.mapping), set(fa.COLUMN_BY_KEY), style)
            self.assertEqual(len(p.rows), 20)

    def test_missing_required_column_is_reported_with_headers(self):
        text = "Ticker,Company\nAAA,Alpha\n"
        with self.assertRaises(fa.ParseError) as cm:
            fa.parse_export(text, ["ticker", "change"])
        self.assertIn("change", str(cm.exception))
        self.assertIn("Company", str(cm.exception))

    def test_empty_export(self):
        with self.assertRaises(fa.ParseError):
            fa.parse_export("", ["ticker"])

    def test_header_only_export_gives_zero_rows(self):
        p = fa.parse_export(",".join(SHORT_HEADERS) + "\n", ["ticker", "change"])
        self.assertEqual(p.rows, [])
        self.assertIsNone(p.change_check)

    def test_duplicate_tickers_dropped(self):
        text = make_csv([("AAA", 5, 1, 10), ("AAA", 9, 1, 10), ("BBB", 3, 1, 10)])
        p = fa.parse_export(text, ["ticker", "change"])
        self.assertEqual([r.ticker for r in p.rows], ["AAA", "BBB"])
        self.assertEqual(p.duplicates_dropped, 1)
        self.assertEqual(p.rows[0].values["change"], 5.0)  # first occurrence kept


class TestUnits(unittest.TestCase):
    def test_percent_sign(self):
        p = fa.parse_export(make_csv(random_stocks(10, 2)), ["ticker", "change"])
        self.assertEqual(p.unit_scale, 1.0)
        self.assertTrue(p.unit_verified)

    def test_fractions_detected_and_converted(self):
        stocks = [("A", 12.5, 3.0, 20.0), ("B", -8.0, 1.0, 50.0), ("C", 30.0, 2.0, 5.0), ("D", 4.0, 2.0, 100.0)]
        p = fa.parse_export(make_csv(stocks, fraction=True), ["ticker", "change", "perf_week"])
        self.assertEqual(p.unit_scale, 100.0)
        self.assertTrue(p.unit_verified)
        self.assertAlmostEqual(p.rows[0].values["change"], 12.5, places=4)
        self.assertAlmostEqual(p.rows[0].values["perf_week"], 3.0, places=4)

    def test_plain_percent_numbers_without_sign(self):
        stocks = [("A", 12.5, 3.0, 20.0), ("B", -8.0, 1.0, 50.0), ("C", 30.0, 2.0, 5.0)]
        p = fa.parse_export(make_csv(stocks, pct_sign=False), ["ticker", "change"])
        self.assertEqual(p.unit_scale, 1.0)
        self.assertTrue(p.unit_verified)

    def test_unverifiable_units_are_flagged(self):
        stocks = [("A", 12.5, 3.0, 20.0)]
        p = fa.parse_export(make_csv(stocks, pct_sign=False), ["ticker", "change"])
        self.assertFalse(p.unit_verified)
        self.assertIn("UNVERIFIED", p.unit_evidence)

    def test_change_cross_check(self):
        stocks = random_stocks(50, 3)
        p = fa.parse_export(make_csv(stocks), ["ticker", "change"])
        cc = p.change_check
        self.assertEqual(cc["rows_checked"], sum(1 for s in stocks if s[1] is not None))
        self.assertEqual(cc["agreement_pct"], 100.0)
        # corrupt one row: change no longer matches price/prev close
        text = make_csv(stocks).replace(f"{stocks[0][1]:.2f}%", "99.00%", 1)
        cc2 = fa.parse_export(text, ["ticker", "change"]).change_check
        self.assertEqual(cc2["rows_agreeing"], cc["rows_agreeing"] - 1)

    def test_no_prev_close_column(self):
        p = fa.parse_export(make_csv(random_stocks(5, 4), prev_close=False), ["ticker", "change"])
        self.assertIsNone(p.change_check)
        self.assertTrue(p.unit_verified)  # % sign still proves the unit


class TestRanking(unittest.TestCase):
    def test_matches_brute_force_on_random_screens(self):
        for seed in range(25):
            stocks = random_stocks(random.Random(seed).randint(0, 120), seed)
            p = fa.parse_export(make_csv(stocks), ["ticker", "change", "perf_week"])
            for tf_key, idx in (("change", 1), ("week", 2)):
                for incl in (False, True):
                    got = [r.ticker for r in fa.rank_rows(p.rows, fa.TIMEFRAMES[tf_key], 10, incl).top]
                    self.assertEqual(got, brute_force_top(stocks, idx, 10, incl), (seed, tf_key, incl))

    def test_fewer_than_n_gainers(self):
        stocks = [("A", 2.0, 0, 10), ("B", -1.0, 0, 10), ("C", 0.0, 0, 10), ("D", None, 0, 10)]
        p = fa.parse_export(make_csv(stocks), ["ticker", "change"])
        rk = fa.rank_rows(p.rows, fa.TIMEFRAMES["change"], 10, False)
        self.assertEqual([r.ticker for r in rk.top], ["A"])
        self.assertEqual((rk.total_rows, rk.with_value, rk.eligible), (4, 3, 1))

    def test_ties_break_by_ticker(self):
        stocks = [("ZZZ", 5.0, 0, 10), ("AAA", 5.0, 0, 10), ("MMM", 5.0, 0, 10)]
        p = fa.parse_export(make_csv(stocks), ["ticker", "change"])
        self.assertEqual([r.ticker for r in fa.rank_rows(p.rows, fa.TIMEFRAMES["change"], 2, False).top],
                         ["AAA", "MMM"])


class TestScreens(unittest.TestCase):
    def test_preset_url_keeps_filters_drops_display_params(self):
        url = ("https://elite.finviz.com/screener.ashx?v=111&f=cap_smallover,sec_technology|healthcare"
               "&ft=4&o=-marketcap&r=21&c=1,2&auth=LEAKED&s=ta_newhigh")
        s = fa.screen_from_preset("Mine", url)
        self.assertEqual(s.params, {"f": "cap_smallover,sec_technology|healthcare", "ft": "4", "s": "ta_newhigh"})
        self.assertNotIn("LEAKED", s.source)

    def test_percent_encoded_preset_url(self):
        url = "https://finviz.com/screener.ashx?v=111&f=cap_smallover%2Csh_price_o5"
        self.assertEqual(fa.screen_from_preset("X", url).params["f"], "cap_smallover,sh_price_o5")

    def test_bad_preset_urls(self):
        for url in ["https://example.com/screener.ashx?f=x", "https://finviz.com/quote.ashx?t=AAPL",
                    "https://finviz.com/screener.ashx?v=111", "finviz.com/screener.ashx?f=x",
                    "https://finviz.com/screenerstocks?f=x", "https://finviz.com/screener/stocks?v=111"]:
            with self.assertRaises(fa.ConfigError, msg=url):
                fa.screen_from_preset("Bad", url)

    def test_new_screener_address_accepted(self):
        for url in ["https://elite.finviz.com/screener/stocks?v=111&f=cap_small,sh_price_o5&ft=4&o=-change",
                    "https://finviz.com/screener/stocks/?f=cap_small,sh_price_o5",
                    "https://finviz.com/screener?f=cap_small,sh_price_o5"]:
            s = fa.screen_from_preset("New", url)
            self.assertEqual(s.params["f"], "cap_small,sh_price_o5", url)
            self.assertNotIn("o", s.params)

    def test_no_filter_error_lists_parameters_found(self):
        with self.assertRaises(fa.ConfigError) as cm:
            fa.screen_from_preset("X", "https://finviz.com/screener/stocks?v=111&preset=abc")
        self.assertIn("preset", str(cm.exception))

    def test_export_params(self):
        s = fa.screen_from_preset("Mine", "https://finviz.com/screener.ashx?v=111&f=a,b&o=ticker")
        p = fa.build_export_params(s, fa.TIMEFRAMES["week"])
        self.assertEqual(p["v"], "152")
        self.assertEqual(p["o"], "-perf1w")
        self.assertEqual(p["f"], "a,b")
        self.assertIn("66", p["c"].split(","))
        self.assertIn("81", p["c"].split(","))

    def test_build_screens_all_signals(self):
        screens = fa.build_screens({})
        self.assertEqual(len(screens), len(fa.SIGNALS))
        self.assertEqual(len(fa.SIGNALS), 33)
        self.assertEqual(len({s.slug for s in screens}), len(screens))

    def test_include_exclude_and_unknown(self):
        cfg = {"builtin_signals": {"include": ["top gainers", "New High", "Unusual Volume"], "exclude": ["new high"]}}
        self.assertEqual([s.name for s in fa.build_screens(cfg)], ["Top Gainers", "Unusual Volume"])
        with self.assertRaises(fa.ConfigError):
            fa.build_screens({"builtin_signals": {"include": ["Moon Shots"]}})

    def test_duplicate_names_and_disabled_presets(self):
        cfg = {
            "saved_presets": [
                {"name": "Top Gainers", "url": "https://finviz.com/screener.ashx?f=a"},
                {"name": "Off", "url": "https://finviz.com/screener.ashx?f=b", "enabled": False},
            ],
            "builtin_signals": {"include": ["Top Gainers"]},
        }
        names = [s.name for s in fa.build_screens(cfg)]
        self.assertEqual(names, ["Top Gainers", "Top Gainers [finviz signal]"])

    def test_only_filter(self):
        self.assertEqual([s.name for s in fa.build_screens({}, ["new high"])], ["New High"])
        with self.assertRaises(fa.ConfigError):
            fa.build_screens({}, ["nope"])

    def test_timeframes(self):
        self.assertEqual([t.key for t in fa.parse_timeframes("change, week,change")], ["change", "week"])
        with self.assertRaises(fa.ConfigError):
            fa.parse_timeframes("decade")


class TestClient(unittest.TestCase):
    PARAMS = {"f": "cap_smallover,sec_technology|healthcare", "v": "152"}

    def test_success_url_and_auth(self):
        op = FakeOpener([FakeResponse(make_csv(random_stocks(3, 5)))])
        c, _ = client_with(op)
        text = c.export_csv(self.PARAMS)
        self.assertIn("Ticker", text)
        url = op.urls[0]
        self.assertTrue(url.startswith("https://elite.finviz.com/export.ashx?"))
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(q["auth"], SECRET)
        self.assertEqual(q["f"], self.PARAMS["f"])
        self.assertIn("f=cap_smallover,sec_technology|healthcare", url)  # commas/pipes kept readable

    def test_404_falls_back_to_second_path_and_remembers(self):
        body = make_csv(random_stocks(3, 6))
        op = FakeOpener([lambda u: http_error(u, 404), FakeResponse(body), FakeResponse(body)])
        c, _ = client_with(op)
        c.export_csv(self.PARAMS)
        c.export_csv(self.PARAMS)
        self.assertIn("/export.ashx?", op.urls[0])
        self.assertIn("/export?", op.urls[1])
        self.assertIn("/export?", op.urls[2])  # remembered

    def test_both_paths_404(self):
        op = FakeOpener([lambda u: http_error(u, 404), lambda u: http_error(u, 404)])
        c, _ = client_with(op)
        with self.assertRaises(fa.FetchError):
            c.export_csv(self.PARAMS)

    def test_429_retry_honours_retry_after(self):
        body = make_csv(random_stocks(3, 7))
        op = FakeOpener([lambda u: http_error(u, 429, headers={"Retry-After": "7"}), FakeResponse(body)])
        c, sleeps = client_with(op, min_interval=0)
        self.assertIn("Ticker", c.export_csv(self.PARAMS))
        self.assertIn(7.0, sleeps)

    def test_retries_exhausted(self):
        op = FakeOpener([lambda u: http_error(u, 503)] * 3)
        c, sleeps = client_with(op, retries=2, min_interval=0)
        with self.assertRaises(fa.FetchError):
            c.export_csv(self.PARAMS)
        self.assertEqual(len(op.urls), 3)

    def test_auth_errors(self):
        for action in (lambda u: http_error(u, 401), lambda u: http_error(u, 403),
                       FakeResponse("<!DOCTYPE html><html><body>Login</body></html>", "text/html"),
                       FakeResponse("\n  <html>login</html>", "text/plain")):
            c, _ = client_with(FakeOpener([action]))
            with self.assertRaises(fa.AuthError):
                c.export_csv(self.PARAMS)

    def test_network_error_message_is_redacted(self):
        enc = urllib.parse.quote_plus(SECRET)
        err = urllib.error.URLError(f"failed for https://elite.finviz.com/export.ashx?auth={enc}")
        op = FakeOpener([err, err])
        c, _ = client_with(op, retries=1, min_interval=0)
        with self.assertRaises(fa.FetchError) as cm:
            c.export_csv(self.PARAMS)
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertNotIn(enc, str(cm.exception))

    def test_throttle(self):
        # 1st request stamps t=0.0; 2nd request checks at t=0.2 (must wait 0.8), then stamps t=1.0
        times = iter([0.0, 0.2, 1.0])
        sleeps = []
        body = make_csv(random_stocks(2, 8))
        c = fa.FinvizClient(SECRET, urlopen=FakeOpener([FakeResponse(body)] * 2),
                            sleep=sleeps.append, clock=lambda: next(times), min_interval=1.0)
        c.export_csv(self.PARAMS)
        c.export_csv(self.PARAMS)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.8)

    def test_certificate_error_aborts_without_retry(self):
        import ssl
        cert = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        op = FakeOpener([urllib.error.URLError(cert)])
        c, sleeps = client_with(op)
        with self.assertRaises(fa.CertificateError) as cm:
            c.export_csv(self.PARAMS)
        self.assertEqual(len(op.urls), 1)  # no pointless retries
        self.assertIn("certifi", str(cm.exception))  # the fix is named on every platform
        self.assertNotIn(SECRET, str(cm.exception))

    def test_default_ssl_context_verifies(self):
        import ssl
        ctx, source = fa.make_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertIn("Python default", source)

    def test_missing_token(self):
        with self.assertRaises(fa.AuthError):
            fa.FinvizClient("")

    def test_redactor(self):
        r = fa.Redactor([SECRET])
        text = f"a {SECRET} b {urllib.parse.quote(SECRET, safe='')} c ?auth=anything&x=1"
        out = r(text)
        self.assertNotIn(SECRET, out)
        self.assertNotIn(urllib.parse.quote(SECRET, safe=""), out)
        self.assertIn("auth=***&x=1", out)


class _TextGrabber(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts += 1

    def handle_data(self, data):
        self.text.append(data)


class TestEndToEnd(unittest.TestCase):
    def _write_case(self, root: Path):
        cfg = {
            "top_n": 10,
            "timeframes": ["change", "week"],
            "saved_presets": [
                {"name": "My Momentum", "url": "https://elite.finviz.com/screener.ashx?v=111&f=sh_price_o5&o=-change"},
                {"name": "Few Gainers", "url": "https://elite.finviz.com/screener.ashx?f=sh_price_u1"},
                {"name": "Missing File", "url": "https://elite.finviz.com/screener.ashx?f=x"},
            ],
            "builtin_signals": {"include": ["Top Gainers", "New High"]},
        }
        (root / "screens.json").write_text(json.dumps(cfg))
        data = root / "data"
        data.mkdir()
        shared = ("SHARED", 77.0, 88.0, 10.0, "<script>alert(1)</script> & Co")
        cases = {
            "my-momentum": random_stocks(80, 11) + [shared],
            "few-gainers": [("UP1", 1.5, -2.0, 3.0), ("DN1", -4.0, 5.0, 2.0), ("FLAT", 0.0, 0.0, 1.0)],
            "top-gainers": random_stocks(40, 12) + [shared],
            "new-high": random_stocks(25, 13),
        }
        styles = {"my-momentum": "short", "few-gainers": "long", "top-gainers": "long", "new-high": "short"}
        for slug, stocks in cases.items():
            (data / f"{slug}.csv").write_text(make_csv(stocks, style=styles[slug]))
        return cases

    def test_offline_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = self._write_case(root)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = fa.main(["--config", str(root / "screens.json"), "--offline", str(root / "data"),
                                "--out", str(root / "reports")])
            self.assertEqual(code, 0)
            run_dirs = [p for p in (root / "reports").iterdir() if p.is_dir()]
            self.assertEqual(len(run_dirs), 1)
            run = run_dirs[0]
            self.assertTrue((root / "reports" / "latest_report.html").is_file())

            # CSV matches brute force for every screen and timeframe
            with (run / "top_gainers.csv").open(newline="") as fh:
                rows = list(csv.DictReader(fh))
            slug_by_name = {"My Momentum": "my-momentum", "Few Gainers": "few-gainers",
                            "Top Gainers": "top-gainers", "New High": "new-high"}
            for name, slug in slug_by_name.items():
                for tf, idx in (("change", 1), ("week", 2)):
                    got = [r["ticker"] for r in rows if r["screen"] == name and r["timeframe"] == tf]
                    ranks = [int(r["rank"]) for r in rows if r["screen"] == name and r["timeframe"] == tf]
                    self.assertEqual(got, brute_force_top(cases[slug], idx, 10), (name, tf))
                    self.assertEqual(ranks, list(range(1, len(got) + 1)))
            self.assertEqual([r["ticker"] for r in rows if r["screen"] == "Few Gainers" and r["timeframe"] == "change"],
                             ["UP1"])

            # tickers.txt: line 1 = run date/time, line 2 = every report ticker once, comma-separated
            lines = (run / "tickers.txt").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertRegex(lines[0], r"^Run: \d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+$")
            expected = list(dict.fromkeys(r["ticker"] for r in rows))  # unique, report order
            self.assertEqual(lines[1].split(","), expected)
            self.assertEqual(lines[1].count("SHARED"), 1)  # in two screens, listed once
            self.assertNotIn(" ", lines[1])
            self.assertEqual((root / "reports" / "latest_tickers.txt").read_text(),
                             (run / "tickers.txt").read_text())
            # cross-check against the HTML itself: same set of tickers as the report's quote links
            html_tickers = set(re.findall(r"quote\.ashx\?t=([^'\"&]+)", (run / "report.html").read_text()))
            self.assertEqual(set(expected), html_tickers)

            summary = json.loads((run / "summary.json").read_text())
            status = {s["name"]: s["status"] for s in summary["screens"]}
            self.assertEqual(status["Missing File"], "error")
            self.assertEqual(summary["screens_ok"], 4)
            overlap = summary["overlap"]["change"]
            self.assertEqual(overlap[0]["ticker"], "SHARED")
            self.assertEqual(len(overlap[0]["screens"]), 2)

            page = (run / "report.html").read_text()
            grab = _TextGrabber()
            grab.feed(page)
            self.assertEqual(grab.scripts, 0, "company name must be escaped, not executed")
            self.assertIn("&lt;script&gt;", page)
            for name in slug_by_name:
                self.assertIn(name, page)
            self.assertIn("Missing File", page)

    def test_live_path_with_fake_finviz_never_leaks_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_case(root)
            cfg = json.loads((root / "screens.json").read_text())
            cfg["saved_presets"] = cfg["saved_presets"][:1]
            (root / "screens.json").write_text(json.dumps(cfg))
            seen_params = []

            def fake_urlopen(req, timeout=None, **kw):
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
                seen_params.append(q)
                if q.get("s") == "ta_newhigh":
                    raise http_error(req.full_url, 500, reason=f"boom auth={SECRET}")
                return FakeResponse(make_csv(random_stocks(30, len(seen_params))))

            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"FINVIZ_API_TOKEN": SECRET}), \
                    mock.patch.object(fa.urllib.request, "urlopen", fake_urlopen), \
                    mock.patch.object(fa.time, "sleep", lambda s: None), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = fa.main(["--config", str(root / "screens.json"), "--out", str(root / "reports"),
                                "--save-raw", "--delay", "0"])
            self.assertEqual(code, 0)
            self.assertTrue(all(p["auth"] == SECRET and p["v"] == "152" and p["o"] == "-change" for p in seen_params))
            self.assertEqual(seen_params[0]["f"], "sh_price_o5")
            blobs = [out.getvalue(), err.getvalue()]
            for path in (root / "reports").rglob("*"):
                if path.is_file():
                    blobs.append(path.read_text(encoding="utf-8"))
            for blob in blobs:
                self.assertNotIn(SECRET, blob)
                self.assertNotIn(urllib.parse.quote_plus(SECRET), blob)
            summary = next((root / "reports").glob("*/summary.json"))
            st = {s["name"]: s["status"] for s in json.loads(summary.read_text())["screens"]}
            self.assertEqual(st["New High"], "error")
            self.assertEqual(st["Top Gainers"], "ok")

    def test_auth_failure_aborts_with_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_case(root)

            def fake_urlopen(req, timeout=None, **kw):
                return FakeResponse("<html>please log in</html>", "text/html")

            err = io.StringIO()
            with mock.patch.dict(os.environ, {"FINVIZ_API_TOKEN": SECRET}), \
                    mock.patch.object(fa.urllib.request, "urlopen", fake_urlopen), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = fa.main(["--config", str(root / "screens.json"), "--out", str(root / "reports"),
                                "--delay", "0"])
            self.assertEqual(code, fa.EXIT_AUTH)
            self.assertIn("AUTH ERROR", err.getvalue())

    def test_missing_token_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_case(root)
            env = {k: v for k, v in os.environ.items() if k not in fa.TOKEN_ENV_VARS}
            with mock.patch.dict(os.environ, env, clear=True), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = fa.main(["--config", str(root / "screens.json"), "--out", str(root / "reports")])
            self.assertEqual(code, fa.EXIT_AUTH)

    def test_certificate_failure_exit_5(self):
        import ssl
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_case(root)

            def fake_urlopen(req, timeout=None, **kw):
                raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))

            err = io.StringIO()
            with mock.patch.dict(os.environ, {"FINVIZ_API_TOKEN": SECRET}), \
                    mock.patch.object(fa.urllib.request, "urlopen", fake_urlopen), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = fa.main(["--config", str(root / "screens.json"), "--out", str(root / "reports")])
            self.assertEqual(code, fa.EXIT_TLS)
            self.assertIn("CERTIFICATE ERROR", err.getvalue())
            self.assertFalse((root / "reports").exists())  # stopped before writing a useless report

    def test_tickers_txt_time_zone_and_empty(self):
        import time as _t
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
            _t.tzset()
            self.assertEqual(fa.render_tickers_txt(["AAA", "BBB"], "2026-10-26T19:00:03+00:00"),
                             "Run: 2026-10-26 12:00 PDT\nAAA,BBB\n")
            self.assertEqual(fa.render_tickers_txt([], "2026-12-01T20:30:00+00:00"),
                             "Run: 2026-12-01 12:30 PST\n\n")
        _t.tzset()  # restore the original zone after the patch is undone

    def test_dry_run_hides_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_case(root)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = fa.main(["--config", str(root / "screens.json"), "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("auth=***", out.getvalue())
            self.assertIn("My Momentum", out.getvalue())

    def test_verify_healthy(self):
        def fake_urlopen(req, timeout=None, **kw):
            return FakeResponse(make_csv(random_stocks(40, 21)))

        out = io.StringIO()
        with mock.patch.dict(os.environ, {"FINVIZ_API_TOKEN": SECRET}), \
                mock.patch.object(fa.urllib.request, "urlopen", fake_urlopen), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = fa.main(["--config", str(Path(fa.__file__).with_name("screens.json")), "--verify"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("RESULT: healthy", out.getvalue())
        self.assertNotIn(SECRET, out.getvalue())

    def test_shipped_config_is_valid(self):
        cfg = fa.load_config(Path(fa.__file__).with_name("screens.json"))
        screens = fa.build_screens(cfg)
        self.assertEqual(len(screens), 33)  # example preset ships disabled
        self.assertEqual(fa.parse_timeframes(cfg["timeframes"])[0].key, "change")


if __name__ == "__main__":
    unittest.main()


class TestPresetKeep(unittest.TestCase):
    """A preset can keep every stock (or its own top N) while every other screen keeps the usual top N;
    tickers.txt is the merged list with duplicates removed."""

    def test_usual_top_25_with_keep_all(self):
        """top_n 25 in the config: every screen keeps 25 unless its preset says otherwise."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {"top_n": 25, "saved_presets": [
                {"name": "Market Top Gainers", "url": "https://elite.finviz.com/screener.ashx?f=ta_change_u", "keep": "all"},
                {"name": "Momentum", "url": "https://elite.finviz.com/screener.ashx?f=sh_price_o5"}],
                "builtin_signals": {"include": ["Top Gainers"]}}
            (root / "screens.json").write_text(json.dumps(cfg))
            data = root / "data"; data.mkdir()
            market = [(t, abs(c or 1.0) + 0.01, w, p) for t, c, w, p in random_stocks(240, 31)]
            momentum, top = random_stocks(80, 32), random_stocks(60, 33)
            for slug, stocks in (("market-top-gainers", market), ("momentum", momentum), ("top-gainers", top)):
                (data / f"{slug}.csv").write_text(make_csv(stocks))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(fa.main(["--config", str(root / "screens.json"), "--offline", str(data),
                                          "--out", str(root / "reports")]), 0)
            run = [p for p in (root / "reports").iterdir() if p.is_dir()][0]
            summary = json.loads((run / "summary.json").read_text())
            kept = {s["name"]: len(s["rankings"]["change"]["top"]) for s in summary["screens"]}
            up = lambda st: len([x for x in st if x[1] is not None and x[1] > 0])
            self.assertEqual(kept, {"Market Top Gainers": 240, "Momentum": min(25, up(momentum)),
                                    "Top Gainers": min(25, up(top))})
            self.assertGreater(up(momentum), 25)
            self.assertIn("Top 25 per screen; Market Top Gainers: all", (run / "report.html").read_text())

    def test_parse_keep(self):
        self.assertEqual([fa.parse_keep(v) for v in (None, "", " ", "all", "ALL", "25", 7)], [None] * 3 + [0, 0, 25, 7])
        for bad in ("0", "-3", "lots", "10001", "2.5"):
            with self.assertRaises(fa.ConfigError, msg=bad):
                fa.parse_keep(bad)
        with self.assertRaises(fa.ConfigError) as cm:
            fa.build_screens({"saved_presets": [{"name": "X", "url": "https://elite.finviz.com/screener.ashx?f=a",
                                                 "keep": "lots"}], "builtin_signals": {"enabled": False}})
        self.assertIn("Preset 'X'", str(cm.exception))

    def test_keep_all_plus_top_ten_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {"top_n": 10, "saved_presets": [
                {"name": "Market Top Gainers", "url": "https://elite.finviz.com/screener.ashx?f=ta_change_u",
                 "keep": "all"},
                {"name": "Momentum", "url": "https://elite.finviz.com/screener.ashx?f=sh_price_o5"},
                {"name": "Top Five", "url": "https://elite.finviz.com/screener.ashx?f=sh_price_o10", "keep": "5"}],
                "builtin_signals": {"include": ["Top Gainers", "New High"]}}
            (root / "screens.json").write_text(json.dumps(cfg))
            data = root / "data"; data.mkdir()
            market = random_stocks(240, 21)                     # all up: kept in full
            market = [(t, abs(c or 1.0) + 0.01, w, p) for t, c, w, p in market]
            momentum = random_stocks(60, 22) + [market[0]]      # overlaps the market list
            topfive = random_stocks(30, 23)
            top = random_stocks(40, 24) + [market[1], momentum[0]]
            newhigh = random_stocks(25, 25)
            for slug, stocks in (("market-top-gainers", market), ("momentum", momentum), ("top-five", topfive),
                                 ("top-gainers", top), ("new-high", newhigh)):
                (data / f"{slug}.csv").write_text(make_csv(stocks))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = fa.main(["--config", str(root / "screens.json"), "--offline", str(data),
                                "--out", str(root / "reports")])
            self.assertEqual(code, 0)
            run = [p for p in (root / "reports").iterdir() if p.is_dir()][0]
            got = (run / "tickers.txt").read_text().splitlines()[1].split(",")

            def ranked(stocks, n=None):
                up = sorted([s for s in stocks if s[1] is not None and s[1] > 0], key=lambda s: (-s[1], s[0]))
                return [s[0] for s in (up if n is None else up[:n])]
            want = []
            for lst in (ranked(market), ranked(momentum, 10), ranked(topfive, 5), ranked(top, 10), ranked(newhigh, 10)):
                want += [t for t in lst if t not in want]
            self.assertEqual(got, want)
            self.assertEqual(len(ranked(market)), 240)
            self.assertEqual(len(got), len(set(got)))          # no duplicates
            self.assertLess(len(got), 240 + 10 + 5 + 10 + 10)    # overlaps were merged
            summary = json.loads((run / "summary.json").read_text())
            self.assertEqual({s["name"]: s["keep"] for s in summary["screens"]},
                             {"Market Top Gainers": "all", "Momentum": "top 10", "Top Five": "top 5",
                              "Top Gainers": "top 10", "New High": "top 10"})
            self.assertEqual(summary["keep"], {"Market Top Gainers": "all", "Top Five": "top 5"})
            report = (run / "report.html").read_text()
            self.assertIn("Top 10 per screen; Market Top Gainers: all; Top Five: top 5", report)
            self.assertIn("240 eligible &middot; keeping all", report)
