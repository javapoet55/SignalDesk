"""Authenticated workspace, submission contract, exports and responsive charts."""

import datetime as dt
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import LiveServer

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(
    sync_playwright and os.getenv("PORTAL_BROWSER_TEST"), "optional Chrome suite"
)
class WorkspaceFlow(unittest.TestCase):
    def test_run_and_inspect_report(self):
        server = LiveServer()
        self.addCleanup(server.close)
        rid = "2026-10-30_160000_daily"
        folder = server.app.strategies.results_dir / rid
        folder.mkdir()
        days = [
            (dt.date(2026, 10, 1) + dt.timedelta(days=i)).isoformat() for i in range(30)
        ]
        bars = {d: {"ABC": {"o": 100 + i, "c": 100 + i}} for i, d in enumerate(days)}
        meta = {
            "strategy": "daily",
            "status": "ok",
            "mode": "close",
            "asof": days[0],
            "finished_utc": days[0] + "T20:00:00+00:00",
        }
        (folder / "meta.json").write_text(json.dumps(meta))
        (folder / "signals.csv").write_text("Ticker,Status\nABC,BUY\nXYZ,BUY\n")
        (folder / "history_cache.json").write_text(json.dumps({"history": bars}))
        meta["asof"] = days[-1]
        prior = server.app.strategies.results_dir / "2026-10-01_160000_daily"
        prior.mkdir()
        for name in ("meta.json", "signals.csv"):
            (prior / name).write_bytes((folder / name).read_bytes())
        (folder / "meta.json").write_text(json.dumps(meta))
        key = "abc00000-0000-0000-0000-000000000001"
        req = {
            "strategy": "daily",
            "run": rid,
            "symbol": "ABC",
            "fee_bps": 10,
            "holds": [1, 5, 10],
            "source": "Archived signals",
        }
        vals = [100000 + i * 250 - (i % 5) * 640 for i in range(30)]
        full = {
            "equity": vals,
            "hold": 1,
            "trades": 1,
            "benchmark": [100000 + 100 * i for i in range(30)],
            "fills": [
                {
                    "entry": days[1],
                    "exit": days[2],
                    "buy": 101,
                    "sell": 102,
                    "pnl": 500,
                    "pct": 0.5,
                    "hold": 1,
                }
            ],
        }
        result = {"days": days, "parameters": [], "engine": "QA", "full_period": full}
        submitted = []

        def service(path, data=None):
            if data is not None:
                submitted.append(data)
                return {"id": key}
            if path == "/jobs":
                return [
                    {
                        "id": key,
                        "strategy": "daily",
                        "symbol": "ABC",
                        "created": "2026-11-27T00:52:11.587990+00:00",
                        "status": "Completed",
                    }
                ]
            return {"inputs": req, "result": result, "error": None, "elapsed_seconds": 30.1}

        with (
            patch.dict(
                os.environ,
                VBT_SERVICE_URL="http://test.invalid",
                VBT_SERVICE_TOKEN="fixture",
            ),
            patch("backtests.service", side_effect=service),
            patch("dow_benchmark._download", return_value={d: 50000 + i * 125 for i, d in enumerate(days)}),
            patch("dow_benchmark._refresh_at", 0.0),
            patch("dow_benchmark._closes", {}),
            sync_playwright() as pw,
        ):
            browser = pw.chromium.launch(channel="chrome")
            page = browser.new_page(
                viewport={"width": 1536, "height": 1024}, color_scheme="dark"
            )
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            base = f"http://127.0.0.1:{server.port}"
            page.goto(base + "/vectorbt")
            page.locator("[name=username]").fill("admin")
            page.locator("[name=password]").fill("pw-123")
            page.locator("form.login-form button[type=submit]").click()
            page.locator(".studio-rail").get_by_text("VectorBT PRO").click()
            page.locator("#saved-backtests summary").click()
            self.assertEqual(page.locator("#saved-backtests tbody td").last.inner_text(), "26-NOV-2026 @ 16:52:11 PST")
            page.get_by_role("link", name="Backtest history", exact=True).click()
            page.get_by_role("heading", name="Backtest history", exact=True).wait_for()
            self.assertEqual(page.locator(".card.wrap tbody td").last.inner_text(), "26-NOV-2026 @ 16:52:11 PST")
            self.assertEqual(page.locator(".studio-tabs").count(), 0)
            self.assertEqual(page.locator(".studio-rail nav [aria-current=page]").inner_text(), "VectorBT PRO")
            page.get_by_role("link", name="daily", exact=True).click()
            page.wait_for_url(f"**/vectorbt?job={key}")
            self.assertEqual(page.locator("svg.vb-plot").count(), 2)
            self.assertEqual(page.locator(".vb-kpi").count(), 5)
            self.assertEqual(page.locator("svg.vb-plot").first.locator("polyline").count(), 3)
            graph = page.locator("svg.vb-plot").first
            for name, series in (("Buy & hold", "basket"), ("Dow Jones", "dow")):
                toggle = page.get_by_role("checkbox", name=name)
                toggle.uncheck()
                self.assertFalse(graph.locator(f"g[data-series={series}]").is_visible())
                self.assertTrue(graph.locator("g[data-series=strategy]").is_visible())
                toggle.check()
                self.assertTrue(graph.locator(f"g[data-series={series}]").is_visible())
            help_text = page.locator("details.vb-info").filter(has_text="Profit factor")
            help_text.locator("summary").focus()
            page.keyboard.press("Enter")
            self.assertTrue(help_text.locator("p").is_visible())
            self.assertIn("absolute total losses", help_text.locator("p").inner_text())
            self.assertEqual(page.get_by_label("Hold (trading days)").input_value(), "1")
            page.get_by_label("Hold (trading days)").fill("5")
            self.assertEqual(page.get_by_text("Commission", exact=True).count(), 0)
            self.assertEqual(page.get_by_label("Commission (%)").count(), 0)
            page.get_by_label("Profit target (%)").fill("2")
            # A hold typed by hand is kept and becomes the maximum hold; it stays editable.
            self.assertEqual(page.get_by_label("Maximum hold (trading days)").input_value(), "5")
            self.assertTrue(page.get_by_label("Maximum hold (trading days)").is_editable())
            page.get_by_label("Profit target (%)").fill("")
            self.assertEqual(page.get_by_label("Hold (trading days)").input_value(), "5")
            page.get_by_label("Profit target (%)").fill("2")
            page.get_by_role("button", name="Run Backtest").click()
            page.wait_for_url("**/vectorbt?job=*")
            self.assertEqual(submitted[0]["holds"], [5])
            self.assertTrue(submitted[0]["exit_on_target"])
            self.assertEqual(submitted[0]["entry_basis"], "signal_close")
            self.assertEqual(submitted[0]["fee_bps"], 10)
            self.assertEqual(submitted[0]["profit_target_pct"], 2)
            self.assertGreaterEqual(submitted[0]["preparation_seconds"], 0)
            timing_row = page.locator("dl > div:has(> dt:text-is('Engine')) + div")
            self.assertEqual(timing_row.locator("dt").inner_text(), "Time taken")
            self.assertEqual(timing_row.locator("dd").inner_text(), "30 seconds")
            for group, title in (("all", "total"), ("wins", "winning"), ("losses", "losing")):
                with page.expect_popup() as opened:
                    page.get_by_role("link", name=f"View {title} trades as text").click()
                popup = opened.value
                popup.wait_for_load_state()
                self.assertIn(f"trades={group}", popup.url)
                listing = popup.locator("body").inner_text()
                if group == "losses":
                    self.assertIn("No closed trades", listing)
                else:
                    self.assertIn("ABC", listing)
                    self.assertIn(days[1], listing)
                    self.assertIn(days[2], listing)
                popup.close()
            self.assertEqual(submitted[0]["bars"][0]["signal"], True)
            page.get_by_text("YTD", exact=True).click()
            self.assertIn("window=YTD", page.url)
            for kind in ("csv", "pdf"):
                response = page.request.get(
                    base + f"/vectorbt?job={key}&download={kind}"
                )
                self.assertEqual(response.status, 200)
                self.assertIn(b"ABC" if kind == "csv" else b"%PDF", response.body())
            for width in (1536, 1024, 390):
                page.set_viewport_size({"width": width, "height": 1024})
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), width + 1
                )
                if os.getenv("STUDIO_SCREENSHOTS"):
                    page.screenshot(
                        path=str(
                            Path(os.environ["STUDIO_SCREENSHOTS"]) / f"vb-{width}.png"
                        ),
                        full_page=True,
                    )
            page.locator(".vb-picker summary").click()
            page.get_by_label("Symbols", exact=True).fill("ABC,BAD")
            page.locator(".vb-picker summary").click()
            page.get_by_role("button", name="Run Backtest").click()
            self.assertIn(
                "buy from the selected", page.get_by_role("alert").inner_text()
            )
            self.assertEqual(len(submitted), 1)
            page.locator(".vb-picker summary").click()
            page.get_by_label("Symbols", exact=True).fill("ABC")
            page.locator(".vb-picker summary").click()
            page.locator(".vb-dates summary").click()
            date_start = page.get_by_label("Historical start")
            date_end = page.get_by_label("Historical end")
            self.assertEqual(date_end.get_attribute("max"), days[-1])
            date_start.fill("2026-10-09")
            self.assertTrue(date_end.evaluate("input => input.validity.valueMissing"))
            date_end.fill("2026-10-31")
            self.assertTrue(date_end.evaluate("input => input.validity.rangeOverflow"))
            date_end.fill("2026-10-01")
            self.assertIn("2026-10-01 – 2026-10-09", page.locator(".vb-dates summary").inner_text())
            page.locator(".vb-dates summary").click()
            page.get_by_label("Hold (trading days)").fill("2,4")
            with patch(
                "backtests.calendar_replay",
                return_value=(submitted[0]["bars"][:7], "fixture", {}),
            ) as replay:
                page.get_by_role("button", name="Run Backtest").click()
                page.wait_for_url("**/vectorbt?job=*")
                self.assertEqual(
                    replay.call_args.args[-2:], ("2026-10-01", "2026-10-09")
                )
                self.assertEqual(submitted[1]["holds"], [2, 4])
            page.locator(".vb-picker summary").click()
            page.locator(".vb-options input[value=ALL]").check()
            page.locator(".vb-picker summary").click()
            from types import SimpleNamespace

            with patch.object(
                server.app.strategies, "finviz", SimpleNamespace(cache_dir=folder)
            ):
                page.get_by_role("button", name="Run Backtest").click()
                page.wait_for_load_state("networkidle")
                self.assertEqual(set(submitted[-1]["prices"]), {"ABC", "XYZ"})
            for selection in ("ABC,XYZ", "ALL"):
                page.locator(".vb-picker summary").click()
                page.get_by_label("Symbols", exact=True).fill(selection)
                page.locator(".vb-picker summary").click()
                page.locator(".vb-dates summary").click()
                page.get_by_label("Historical start").fill("2026-07-01")
                page.get_by_label("Historical end").fill("2026-10-30")
                page.locator(".vb-dates summary").click()
                series = {s: submitted[0]["bars"] for s in ("ABC", "XYZ")}
                with patch(
                    "backtests.calendar_replay", return_value=(series, "fixture", {})
                ) as replay:
                    page.get_by_role("button", name="Run Backtest").click()
                    page.wait_for_load_state("networkidle")
                    self.assertEqual(page.get_by_role("alert").count(), 0)
                    self.assertEqual(
                        replay.call_args.args[-3:],
                        (["ABC", "XYZ"], "2026-07-01", "2026-10-30"),
                    )
                    self.assertEqual(submitted[-1]["series"], series)
            from backtests import ReplayCoverageError

            page.locator(".vb-picker summary").click()
            page.get_by_label("Symbols", exact=True).fill("ABC,XYZ")
            page.locator(".vb-picker summary").click()
            page.locator(".vb-dates summary").click()
            page.get_by_label("Historical start").fill("2026-10-09")
            page.get_by_label("Historical end").fill("2026-10-01")
            page.locator(".vb-dates summary").click()
            page.get_by_label("Hold (trading days)").fill("2")
            page.get_by_label("Profit target (%)").fill("2")
            sessions = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6, 7, 8, 9)]
            error = ReplayCoverageError({"XYZ": {sessions[0]}}, ["ABC", "XYZ"], sessions)
            with patch("backtests.calendar_replay", side_effect=error):
                page.get_by_role("button", name="Run Backtest").click()
                page.wait_for_selector(".vb-recovery")
            alternatives = page.locator(".vb-recovery form")
            self.assertEqual(alternatives.count(), 2)
            self.assertEqual(alternatives.first.locator("[name=start]").input_value(), "2026-10-02")
            self.assertEqual(alternatives.first.locator("[name=symbols]").input_value(), "ABC,XYZ")
            with patch("backtests.calendar_replay", return_value=(submitted[0]["bars"][:7], "fixture", {})) as replay:
                alternatives.last.get_by_role("button").click()
                page.wait_for_url("**/vectorbt?job=*")
                self.assertEqual(replay.call_args.args[-3:], ("ABC", "2026-10-01", "2026-10-09"))
                self.assertEqual(submitted[-1]["holds"], [2])
                self.assertEqual(submitted[-1]["profit_target_pct"], 2)
            self.assertFalse(errors)
            browser.close()
