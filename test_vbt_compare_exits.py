"""Comparing several profit targets and stop losses in one submit: parsing, submission, table and page."""

import copy
import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import backtests
import vectorbt_workspace as vb
from test_vbt_auto_refresh import DONE, INFO, KEY, LiveServer, sync_playwright
from test_vbt_stop_loss import DAYS, EngineResults, engine

KEYS = [f"abc00000-0000-0000-0000-{n:012d}" for n in range(1, 21)]


class Parsing(unittest.TestCase):
    def values(self, text, parse=backtests.profit_target):
        return backtests.percent_values(text, parse)

    def test_one_value_a_list_and_blanks(self):
        self.assertEqual(self.values(None), [])
        self.assertEqual(self.values("  "), [])
        self.assertEqual(self.values("2"), [2.0])
        self.assertEqual(self.values(" 2, 4 ,,2.0, 0.5 "), [2.0, 4.0, 0.5])   # order kept, repeats dropped
        self.assertEqual(self.values("3,5,10", backtests.stop_loss), [3.0, 5.0, 10.0])

    def test_one_bad_value_rejects_the_whole_entry(self):
        for text in ("2,abc", "2,0", "2,-1", "2;4"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "positive profit target"):
                self.values(text)
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            self.values("5,100", backtests.stop_loss)

    def test_pairs(self):
        pairs = backtests.exit_pairs
        self.assertEqual(pairs([], []), [(None, None)])
        self.assertEqual(pairs([2.0], []), [(2.0, None)])
        self.assertEqual(pairs([2.0, 4.0], [3.0, 10.0]), [(2.0, 3.0), (2.0, 10.0), (4.0, 3.0), (4.0, 10.0)])
        with self.assertRaisesRegex(ValueError, "Enter a profit target to use a stop loss"):
            pairs([], [5.0])
        self.assertEqual(len(pairs([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0, 5.0])), 20)
        with self.assertRaisesRegex(ValueError, r"3 profit targets with 7 stop losses is 21 backtests\. Compare at most 20"):
            pairs([1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])


class Submission(unittest.TestCase):
    """The replay is prepared once and each pairing is sent as its own job with untouched copies of the data."""

    def submit(self, fail_at=None, fail_replay=None, **fields):
        sent, prepared = [], []

        def dataset(*args, **kwargs):
            prepared.append((args, kwargs))
            if fail_replay is not None and len(prepared) > fail_replay:
                raise ValueError("This strategy only works on daily price bars.")
            return dict(strategy="s", run="r", symbol="ABC", holds=args[3], fee_bps=args[4], source="Historical Python replay",
                        **({"candle_days": kwargs["candle_days"]} if "candle_days" in kwargs else {}),
                        bars=[dict(day=d, open=1, close=1, signal=False) for d in DAYS], observed_days=len(DAYS))

        def service(path, data=None):
            if fail_at is not None and len(sent) == fail_at:
                raise ValueError("The backtest service is unavailable. Check the private service connection.")
            sent.append(copy.deepcopy(data))
            return {"id": KEYS[len(sent) - 1]}

        class Root:
            def __truediv__(self, other):
                return SimpleNamespace(is_symlink=lambda: False)

        st = SimpleNamespace(results_dir=Root())
        form = dict(dict(rid="2026-10-01_000000_daily", symbols="ABC", holds="5", commission="0.1", start="2026-10-01",
                         end="2026-10-14"), **fields)
        with patch.object(backtests, "dataset", dataset), patch.object(backtests, "service", service):
            try:
                keys = vb.submit_all(st, form, lambda _: [])
            except ValueError as err:
                return err, sent, prepared
        return keys, sent, prepared

    def test_single_backtest_is_unchanged(self):
        keys, sent, prepared = self.submit(profit_target_pct="2", stop_loss_pct="10")
        self.assertEqual(keys, KEYS[:1])
        self.assertEqual(len(prepared), 1)
        self.assertEqual((sent[0]["profit_target_pct"], sent[0]["stop_loss_pct"], sent[0]["exit_on_target"]), (2.0, 10.0, True))

        keys, sent, _ = self.submit()
        self.assertEqual(keys, KEYS[:1])
        self.assertEqual((sent[0]["profit_target_pct"], sent[0]["stop_loss_pct"]), (None, None))
        self.assertNotIn("exit_on_target", sent[0])
        self.assertEqual(sent[0]["holds"], [5])

    def test_every_pairing_is_submitted_once_from_one_replay(self):
        keys, sent, prepared = self.submit(profit_target_pct="2, 4", stop_loss_pct="3,5,10")
        self.assertEqual(keys, KEYS[:6])
        self.assertEqual(len(prepared), 1)                       # the slow replay ran once
        self.assertEqual([(d["profit_target_pct"], d["stop_loss_pct"]) for d in sent],
                         [(2.0, 3.0), (2.0, 5.0), (2.0, 10.0), (4.0, 3.0), (4.0, 5.0), (4.0, 10.0)])
        first = {k: v for k, v in sent[0].items() if k not in ("profit_target_pct", "stop_loss_pct", "preparation_seconds")}
        for other in sent[1:]:
            self.assertEqual({k: v for k, v in other.items()
                              if k not in ("profit_target_pct", "stop_loss_pct", "preparation_seconds")}, first)
        self.assertEqual(first["holds"], [5])
        self.assertTrue(first["exit_on_target"])

    def test_targets_without_stops_and_the_wrapper(self):
        keys, sent, _ = self.submit(profit_target_pct="2,4,6")
        self.assertEqual([(d["profit_target_pct"], d["stop_loss_pct"]) for d in sent], [(2.0, None), (4.0, None), (6.0, None)])
        self.assertEqual(len(keys), 3)

    def test_rejections_happen_before_any_work(self):
        for fields, message in ((dict(stop_loss_pct="3,5"), "Enter a profit target to use a stop loss"),
                                (dict(profit_target_pct="1,2,3,4,5", stop_loss_pct="1,2,3,4,5"), "is 25 backtests"),
                                (dict(profit_target_pct="2,x"), "positive profit target")):
            with self.subTest(fields=fields):
                err, sent, prepared = self.submit(**fields)
                self.assertIsInstance(err, ValueError)
                self.assertRegex(str(err), message)
                self.assertEqual((sent, prepared), ([], []))

    def test_a_failure_part_way_says_how_many_were_submitted(self):
        err, sent, _ = self.submit(fail_at=2, profit_target_pct="2", stop_loss_pct="3,5,10")
        self.assertRegex(str(err), r"unavailable.* 2 of 3 backtests were submitted before this")
        self.assertEqual(len(sent), 2)
        err, sent, _ = self.submit(fail_at=0, profit_target_pct="2", stop_loss_pct="3,5,10")
        self.assertNotIn("submitted before", str(err))


class BarSizes(Submission):
    """Compare bars: one replay per bar size, then every target and stop pairing on each."""

    def test_parsing(self):
        bars = backtests.compare_bars
        self.assertEqual([bars(v) for v in (None, "", " , ", "1", "1, 2,4", "4,2,4,1,")], [[], [], [], [1], [1, 2, 4], [4, 2, 1]])
        for bad in ("0", "6", "1,x", "1.5"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "price bars of 1–5 trading days"):
                bars(bad)

    def test_one_replay_per_bar_size_and_every_pairing_on_each(self):
        keys, sent, prepared = self.submit(compare_bars="1, 2, 4", profit_target_pct="3,4", stop_loss_pct="5")
        self.assertEqual(keys, KEYS[:6])
        self.assertEqual([kwargs.get("candle_days", 1) for _, kwargs in prepared], [1, 2, 4])
        self.assertEqual([(d.get("candle_days", 1), d["profit_target_pct"], d["stop_loss_pct"]) for d in sent],
                         [(1, 3.0, 5.0), (1, 4.0, 5.0), (2, 3.0, 5.0), (2, 4.0, 5.0), (4, 3.0, 5.0), (4, 4.0, 5.0)])

    def test_bars_alone_and_the_list_replaces_the_dropdown(self):
        keys, sent, prepared = self.submit(compare_bars="1,4", candle_days="3")
        self.assertEqual(len(keys), 2)
        self.assertEqual([kwargs.get("candle_days", 1) for _, kwargs in prepared], [1, 4])
        self.assertEqual([(d["profit_target_pct"], d["stop_loss_pct"]) for d in sent], [(None, None)] * 2)
        # Blank list: the dropdown still decides, exactly as before.
        _, sent, prepared = self.submit(compare_bars="", candle_days="3")
        self.assertEqual((len(sent), prepared[0][1]["candle_days"]), (1, 3))
        _, _, prepared = self.submit(compare_bars=" ")
        self.assertNotIn("candle_days", prepared[0][1])

    def test_limits_and_date_range(self):
        err, sent, prepared = self.submit(compare_bars="1,2,3", profit_target_pct="2,3,4,5", stop_loss_pct="3,5")
        self.assertRegex(str(err), r"3 bar sizes with 8 profit target and stop loss pairings is 24 backtests\. Compare at most 20")
        self.assertEqual((sent, prepared), ([], []))
        err, sent, prepared = self.submit(compare_bars="1,2", start="", end="")
        self.assertRegex(str(err), "Choose a date range to use 2–5 day price bars")
        self.assertEqual((sent, prepared), ([], []))
        err, _, prepared = self.submit(compare_bars="1,7")
        self.assertRegex(str(err), "price bars of 1–5 trading days")
        self.assertEqual(prepared, [])

    def test_a_bar_size_the_strategy_cannot_use_reports_what_was_already_submitted(self):
        err, sent, prepared = self.submit(fail_replay=1, compare_bars="1,2", profit_target_pct="3", stop_loss_pct="5,8")
        self.assertRegex(str(err), r"2-day bars: This strategy only works on daily price bars\. 2 of 4 backtests were submitted")
        self.assertEqual(len(sent), 2)
        err, sent, _ = self.submit(fail_replay=0, compare_bars="2,4")
        self.assertEqual(str(err), "This strategy only works on daily price bars.")
        self.assertEqual(sent, [])


class Links(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(vb.compare_keys(",".join(KEYS[:3])), KEYS[:3])
        self.assertEqual(vb.compare_keys(",".join(KEYS[:2] + KEYS[:1])), KEYS[:2])     # repeats dropped
        for bad in ("", KEYS[0], ",".join(KEYS + ["abc00000-0000-0000-0000-000000000099"]), KEYS[0] + ",../etc",
                    KEYS[0] + ",<script>", None):
            with self.subTest(bad=bad):
                self.assertEqual(vb.compare_keys(bad), [])

    def test_labels(self):
        self.assertEqual([vb.percent_label(v) for v in (None, 2, 2.0, 2.5, 10)], ["None", "2%", "2%", "2.5%", "10%"])


class Table(EngineResults):
    """Real engine results for the five-symbol fixture: 2% target with a 10% stop, a 5% stop and no stop."""

    def job(self, stop):
        extra = {} if stop is None else dict(stop_loss_pct=stop)
        request = self.request(**extra)
        result = engine().calculate(request)
        request.pop("series")
        symbols = [f["symbol"] for f in result["full_period"]["fills"]]
        return dict(inputs=dict(request, symbols=symbols, stop_loss_pct=stop), result=result, error=None)

    def rows(self):
        jobs = {KEYS[0]: self.job(10), KEYS[1]: self.job(None),
                KEYS[2]: dict(inputs=dict(self.job(5)["inputs"]), result=None, error=None),
                KEYS[3]: dict(inputs=dict(self.job(5)["inputs"], stop_loss_pct=3), result=None,
                              error="Missing a daily bar <b>"),
                }

        def fetch(key):
            if key not in jobs:
                raise backtests.JobNotFound("gone")
            return jobs[key]

        with patch("dow_benchmark.equity", return_value=[]):
            return vb.compare_rows(KEYS[:5], fetch)

    def test_rows_statuses_and_best(self):
        rows = self.rows()
        self.assertEqual([r["status"] for r in rows], ["Completed", "Completed", "Queued or running", "Failed", "Not found"])
        with_stop, no_stop = rows[0]["m"], rows[1]["m"]
        self.assertEqual((with_stop["trades"], with_stop["wins"], with_stop["losses"]), (5, 1, 4))
        self.assertAlmostEqual(with_stop["total"], -7.2)
        self.assertAlmostEqual(with_stop["expectancy"], -1440.0)
        # Without a stop only TIME loses (-600); BOTH and HIT reach the target (+400 each); STOP and GAPDOWN close flat or lower.
        self.assertGreater(no_stop["expectancy"], with_stop["expectancy"])
        self.assertEqual(vb.best_row(rows), KEYS[1])
        self.assertIsNone(vb.best_row(rows[2:]))

    def test_table_html(self):
        rows = self.rows()
        table = vb.compare_table(rows, KEYS[:5])
        self.assertEqual(table.count("<tr"), 6)
        self.assertEqual(table.count("class=vb-best"), 1)
        self.assertEqual(table.count("Best expectancy"), 1)
        best = table[table.index("<tr class=vb-best>"):]
        self.assertTrue(best.startswith("<tr class=vb-best><th scope='row'>2%</th><td>None</td>"))
        first = table[table.index("<tbody>"):table.index("<tr class=vb-best>")]
        self.assertIn("<th scope='row'>2%</th><td>10%</td><td class='down'>-7.20%</td><td>20.00%</td><td>5</td>", first)
        self.assertIn("<td class='down'>−$1,440.00</td>", first)
        self.assertIn(f"/vectorbt?job={KEYS[0]}&amp;compare=", first)
        self.assertIn("Queued or running", table)
        self.assertIn("Failed<small>Missing a daily bar &lt;b&gt;</small>", table)
        self.assertNotIn("<b>", table.replace("<b class=vb-badge>", ""))
        self.assertIn("<th scope='row'>—</th><td>—</td>" + "<td>—</td>" * 5 + "<td>Not found</td><td>—</td>", table)

    def test_bar_size_column_appears_only_when_bar_sizes_differ(self):
        rows = self.rows()
        same = vb.compare_table(rows, KEYS[:5])
        self.assertNotIn("Price bars", same)
        rows[1]["info"] = dict(rows[1]["info"], candle_days=4)
        table = vb.compare_table(rows, KEYS[:5])
        self.assertIn("<thead><tr><th>Price bars</th><th>Profit target</th>", table)
        self.assertIn("<th scope='row'>Daily</th><td>2%</td><td>10%</td>", table)
        self.assertIn("<tr class=vb-best><th scope='row'>4-day</th><td>2%</td><td>None</td>", table)
        self.assertIn("<th scope='row'>—</th><td>—</td><td>—</td>", table)          # the job that was not found
        self.assertEqual(table.count("<th scope='row'>"), 5)
        self.assertEqual([vb.bar_label(v) for v in (None, 1, "1", 2, 5)], ["Daily", "Daily", "Daily", "2-day", "5-day"])

    def test_summary(self):
        summary = vb.compare_summary(self.rows())
        for text in ("<dt>Strategy</dt><dd>daily</dd>", "<dt>Symbols</dt><dd>5 symbols</dd>",
                     f"<dt>Dates</dt><dd>{DAYS[0]} – ", "<dt>Maximum hold</dt><dd>7 trading days</dd>",
                     "<dt>Initial capital</dt><dd>$100,000</dd>"):
            self.assertIn(text, summary)
        self.assertEqual(vb.compare_summary([dict(key="k", status="Not found", m={}, info={}, error="")]), "")


class Page(Table):
    def render(self, query, fetch=None, configured=True):
        sent = {}
        host = SimpleNamespace(_html=lambda status, html: sent.update(status=status, html=html),
                               _text=lambda status, text: sent.update(status=status, html=text))
        jobs = {KEYS[0]: self.job(10), KEYS[1]: self.job(None)}
        pending = dict(inputs=dict(jobs[KEYS[0]]["inputs"], stop_loss_pct=5), result=None, error=None)

        def service(path, data=None):
            key = path.rsplit("/", 1)[1]
            if fetch:
                return fetch(key)
            return jobs.get(key, pending)

        env = dict(VBT_SERVICE_URL="http://x", VBT_SERVICE_TOKEN="t") if configured else {}
        with patch.object(backtests, "service", service), patch.dict("os.environ", env, clear=not configured), \
                patch("dow_benchmark.equity", return_value=[]), patch("server.render_tabs", return_value=""):
            vb.compare(host, query)
        return sent

    def test_finished_comparison(self):
        page = self.render({"jobs": ",".join(KEYS[:2])})
        self.assertEqual(page["status"], 200)
        html = page["html"]
        self.assertIn("<h1>Compare profit targets and stop losses</h1>", html)
        self.assertIn("The highlighted row has the highest expectancy", html)
        self.assertIn("const VB_PENDING=false,VB_POLL_MS=30000,VB_POLL_LIMIT=120;", html)
        self.assertEqual(html.count("class=vb-best"), 1)

    def test_running_comparison_refreshes(self):
        html = self.render({"jobs": ",".join(KEYS[:3])})["html"]
        self.assertIn("2 of 3 backtests finished. This page refreshes every 30 seconds until the rest are done.", html)
        self.assertIn("const VB_PENDING=true,", html)

    def test_bad_links_and_outages(self):
        self.assertEqual(self.render({"jobs": KEYS[0]})["status"], 400)
        self.assertEqual(self.render({})["status"], 400)

        def down(key):
            raise ValueError("The backtest service is unavailable.")

        html = self.render({"jobs": ",".join(KEYS[:2])}, fetch=down)["html"]
        self.assertIn("Backtest service unavailable. Refresh to retry.", html)
        self.assertNotIn("<table>", html)
        html = self.render({"jobs": ",".join(KEYS[:2])}, configured=False)["html"]
        self.assertIn("The backtest service is not configured.", html)

    def test_no_trades_anywhere(self):
        def failed(key):
            return dict(inputs=dict(strategy="s", symbol="ABC", profit_target_pct=2, stop_loss_pct=5), result=None, error="boom")

        html = self.render({"jobs": ",".join(KEYS[:2])}, fetch=failed)["html"]
        self.assertIn("No backtest in this comparison finished with trades.", html)
        self.assertNotIn("class=vb-best", html)


class Form(unittest.TestCase):
    def test_fields_take_lists_and_keep_them_after_an_error(self):
        html = vb.controls([], "", {}, [], True, dict(profit_target_pct="2, 4", stop_loss_pct="3,5,10"))
        self.assertIn("name='profit_target_pct' inputmode='decimal'", html)
        self.assertIn("value='2, 4'", html)
        self.assertIn("value='3,5,10'", html)
        self.assertNotIn("type='number'", html)
        self.assertIn("id='vb-compare-help'", html)
        self.assertIn("<label>Compare bars<input name='compare_bars' inputmode='numeric'", html)
        self.assertIn("value='1, 2, 4'", vb.controls([], "", {}, [], True, dict(compare_bars="1, 2, 4")))
        bar_pattern = re.search(r"name='compare_bars' inputmode='numeric' pattern='([^']+)'", html).group(1)
        for good in ("1", "1,2,4", " 1, 2 , 5 ", "2,4,"):
            self.assertTrue(re.fullmatch(bar_pattern, good), good)
        for bad in ("0", "6", "1;2", "12", "x", ",1"):
            self.assertFalse(re.fullmatch(bar_pattern, bad), bad)
        pattern = re.search(r"name='stop_loss_pct' inputmode='decimal' pattern='([^']+)'", html).group(1)
        for good in ("2", "2.5", " 2, 4 ", ".5,10", "5,2,3,4,", "2, 4 , "):
            self.assertTrue(re.fullmatch(pattern, good), good)
        for bad in ("abc", "2;4", ",2", "2,,4", ",", "-1"):
            self.assertFalse(re.fullmatch(pattern, bad), bad)

    def test_saved_single_values_still_prefill(self):
        html = vb.controls([], "", dict(profit_target_pct=2.0, stop_loss_pct=10.0, fee_bps=0, holds=[7]), [], True, {})
        self.assertIn("name='profit_target_pct' inputmode='decimal' pattern='" + vb.PERCENT_LIST + "' value='2.0'", html)
        self.assertIn("Maximum hold (trading days)", html)


@unittest.skipUnless(sync_playwright and os.getenv("PORTAL_BROWSER_TEST"), "optional Chrome suite")
class Browser(unittest.TestCase):
    def test_form_to_comparison_to_report_and_back(self):
        server = LiveServer()
        self.addCleanup(server.close)
        lookups = []
        pair = [KEY, KEY[:-1] + "2"]

        def service(path, data=None):
            if path == "/jobs":
                return []
            lookups.append(path)
            late = path.endswith(pair[1]) and len(lookups) < 6
            return {"inputs": dict(INFO, profit_target_pct=2, stop_loss_pct=5 if path.endswith(pair[1]) else 10),
                    "error": None, "elapsed_seconds": 1, "result": None if late else DONE}

        with (patch.dict(os.environ, VBT_SERVICE_URL="http://vbt.invalid", VBT_SERVICE_TOKEN="fixture"),
              patch("backtests.service", side_effect=service), patch("dow_benchmark.equity", return_value=[]),
              patch("vectorbt_workspace.submit_all", return_value=pair) as submit,
              patch("vectorbt_workspace.POLL_SECONDS", 1), sync_playwright() as pw):
            browser = pw.chromium.launch(channel="chrome")
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            base = f"http://127.0.0.1:{server.port}"
            page.goto(base + "/vectorbt?job=" + KEY)
            page.locator("[name=username]").fill("admin")
            page.locator("[name=password]").fill("pw-123")
            page.locator("form.login-form button[type=submit]").click()
            page.goto(base + "/vectorbt?job=" + KEY)
            button, help_text = page.locator(".vb-run"), page.locator("#vb-compare-help")
            self.assertIn("Run Backtest", button.inner_text())
            self.assertTrue(help_text.is_hidden())
            page.locator("[name=profit_target_pct]").fill("2, 4")
            self.assertIn("Compare 2 Backtests", button.inner_text())
            self.assertEqual(page.locator("#vb-hold-label").inner_text(), "Maximum hold (trading days)")
            self.assertEqual(page.locator("[name=holds]").input_value(), "7")
            page.locator("[name=stop_loss_pct]").fill("3,5,10")
            self.assertIn("Compare 6 Backtests", button.inner_text())
            self.assertIn("Runs 6 backtests on the same signals and prices", help_text.inner_text())
            page.locator("[name=stop_loss_pct]").fill("1,2,3,4,5,6,7,8,9,10,11")
            self.assertIn("That is 22 backtests. Compare at most 20 at a time.", help_text.inner_text())
            page.locator("[name=stop_loss_pct]").fill("2,3,4,5,")       # a comma left at the end is fine
            self.assertTrue(page.locator("[name=stop_loss_pct]").evaluate("field => field.checkValidity()"))
            self.assertIn("Compare 8 Backtests", button.inner_text())
            page.locator("[name=stop_loss_pct]").fill("3,x")          # the browser blocks a malformed list
            self.assertFalse(page.locator("[name=stop_loss_pct]").evaluate("field => field.checkValidity()"))
            page.locator("[name=stop_loss_pct]").fill("")
            page.locator("[name=profit_target_pct]").fill("2")
            self.assertIn("Run Backtest", button.inner_text())
            self.assertTrue(help_text.is_hidden())
            page.locator("[name=compare_bars]").fill("1, 2, 4")          # bar sizes multiply the count
            self.assertIn("Compare 3 Backtests", button.inner_text())
            self.assertIn("Runs 3 backtests, one for each bar size, and shows them side by side.", help_text.inner_text())
            self.assertTrue(page.locator("[name=start]").evaluate("field => field.required"))   # longer bars need dates
            page.locator("[name=stop_loss_pct]").fill("5, 10")
            self.assertIn("Compare 6 Backtests", button.inner_text())
            self.assertIn("one for each bar size, profit target and stop loss combination", help_text.inner_text())
            page.locator("[name=compare_bars]").fill("7")
            self.assertFalse(page.locator("[name=compare_bars]").evaluate("field => field.checkValidity()"))
            page.locator("[name=compare_bars]").fill("")
            self.assertFalse(page.locator("[name=start]").evaluate("field => field.required"))
            # The fixture has no saved strategy runs, which disables the button; the server is patched, so enable it.
            page.evaluate("document.querySelector('.vb-run').disabled=false;"
                          "document.querySelector('[name=rid]').innerHTML='<option value=r selected>r</option>'")
            del lookups[:]
            button.click()
            page.wait_for_url("**/vectorbt/compare?jobs=*")
            self.assertEqual(submit.call_args.args[1]["stop_loss_pct"], "5, 10")
            self.assertIn("1 of 2 backtests finished", page.locator("body").inner_text())
            page.get_by_text("The highlighted row has the highest expectancy").wait_for(timeout=20000)
            self.assertNotIn("poll=", page.url)
            self.assertEqual(page.locator("tr.vb-best").count(), 1)
            self.assertEqual(page.locator(".vb-compare tbody tr").count(), 2)
            self.assertEqual(page.locator(".vb-compare tbody tr").nth(1).locator("td").first.inner_text(), "5%")
            page.locator(".vb-compare tbody tr").nth(1).get_by_text("Open report").click()
            page.wait_for_url("**/vectorbt?job=" + pair[1] + "*")
            page.get_by_text("Back to the comparison of 2 backtests").click()
            page.wait_for_url("**/vectorbt/compare?jobs=*")
            self.assertEqual(page.locator(".vb-compare tbody tr").count(), 2)
            self.assertFalse(errors)
            browser.close()


if __name__ == "__main__":
    unittest.main()
