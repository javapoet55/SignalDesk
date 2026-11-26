"""Exercise real archive files, filters, drill-downs and downloads in the history workspace."""

import datetime as dt
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import run_history
from test_portal_workspace import sync_playwright
from test_server import LiveServer


class HistoryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.host = LiveServer()
        self.addCleanup(self.host.close)
        self.runs = []
        names = ("momentum_scanner", "daily_scanner", "trend_scanner")
        for i in range(15):
            when = dt.datetime(2026, 11, 26, 15, 2, tzinfo=dt.UTC) - dt.timedelta(
                days=i
            )
            name = names[i % 3]
            rid = when.strftime("%Y-%m-%d_%H%M%S_") + name
            folder = self.host.app.strategies.results_dir / rid
            folder.mkdir()
            ok = i % 4 != 1
            record = {
                "id": rid,
                "strategy": name,
                "status": "ok" if ok else "error",
                "started_utc": when.isoformat(),
                "finished_utc": (
                    when + dt.timedelta(minutes=i + 1, seconds=21)
                ).isoformat(),
                "counts": {"BUY": 2} if ok else {},
            }
            (folder / "meta.json").write_text(json.dumps(record))
            (folder / "output.txt").write_text("Saved output for " + rid)
            (folder / "tickers.txt").write_text(
                "Run: 2026-11-26 07:00 PST\n"
                + ("SPY,AAPL,UNIQUE" if i == 12 else "SPY,AAPL,AMD")
            )
            if ok:
                (folder / "signals.csv").write_text(
                    "Ticker,Status\nSPY,BUY\nAAPL,BUY\n"
                )
            self.runs.append(record)

    def test_archive_filters_and_private_data(self):
        for route in (
            "/strategies/history/panel",
            "/strategies/history/performance?rid=" + self.runs[0]["id"],
        ):
            res, _ = self.host.request("GET", route, auth=None)
            self.assertEqual(res.status, 303)
        for query, count in (
            ("?q=unique", 1),
            ("?strategy=daily_scanner&status=error", 2),
            ("?range=custom&start=2026-11-12&end=2026-11-13", 2),
            ("?page=2", 5),
        ):
            res, data = self.host.request("GET", "/strategies/history/panel" + query)
            self.assertEqual(res.status, 200)
            self.assertEqual(data.count(b"tr data-select"), count)
        self.assertEqual(run_history.metrics(self.runs)[:3], (15, 11, 4))
        self.assertIsNone(run_history.seconds({"started_utc": "invalid"}))

    @unittest.skipUnless(
        sync_playwright and os.getenv("PORTAL_BROWSER_TEST"), "optional Chrome suite"
    )
    def test_render_and_controls(self):
        rid = self.runs[0]["id"]
        days = [
            (dt.date(2026, 9, 1) + dt.timedelta(days=i)).isoformat() for i in range(42)
        ]
        job = {
            "id": "abc00000-0000-0000-0000-000000000001",
            "inputs": {
                "run": rid,
                "strategy": "momentum_scanner",
                "symbol": "SPY",
                "fee_bps": 1,
            },
            "result": {
                "days": days,
                "parameters": [],
                "engine": "Fixture",
                "full_period": {
                    "equity": [100000 + i * 400 + (i % 4) * 500 for i in range(42)],
                    "benchmark": [100000 + i * 220 for i in range(42)],
                    "trades": 0,
                    "hold": 5,
                },
            },
            "error": None,
        }

        def service(path):
            return job if path == "/runs/" + rid + "/latest-backtest" else None

        with (
            patch.dict(
                os.environ,
                VBT_SERVICE_URL="http://test.invalid",
                VBT_SERVICE_TOKEN="fixture",
            ),
            patch("backtests.service", side_effect=service),
            sync_playwright() as pw,
        ):
            browser = pw.chromium.launch(channel="chrome")
            page = browser.new_page(
                color_scheme="dark", viewport={"width": 1672, "height": 941}
            )
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            base = f"http://127.0.0.1:{self.host.port}"
            page.goto(base + "/login")
            page.locator("[name=username]").fill("admin")
            page.locator("[name=password]").fill("pw-123")
            page.locator("form.login-form button[type=submit]").click()
            page.goto(base + "/strategies")
            page.get_by_role("link", name="Run History", exact=True).click()
            root = page.locator("#rh-root")
            root.locator("svg.vb-plot").wait_for()
            for width in (1672, 1024, 390):
                page.set_viewport_size({"width": width, "height": 941})
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), width + 1
                )
                if os.getenv("STUDIO_SCREENSHOTS"):
                    page.screenshot(
                        path=str(
                            Path(os.environ["STUDIO_SCREENSHOTS"])
                            / f"run-history-{width}.png"
                        ),
                        full_page=True,
                    )
            page.set_viewport_size({"width": 1672, "height": 941})
            root.get_by_role("button", name="Signals (2)").click()
            self.assertEqual(root.locator(".rh-symbols").inner_text(), "SPY, AAPL")
            root.get_by_role("button", name="Logs", exact=True).click()
            self.assertIn(rid, root.locator("pre").inner_text())
            with page.expect_download() as downloaded:
                root.get_by_role("link", name="Download TXT").click()
            self.assertEqual(Path(downloaded.value.path()).read_text(), "SPY,AAPL\n")
            root.get_by_role("navigation", name="Results pages").get_by_role(
                "link", name="2", exact=True
            ).click()
            root.get_by_text("Showing 11–15 of 15 runs").wait_for()
            root.locator("tr[data-select] a[data-history-link]").nth(1).click()
            root.locator("[data-copy='" + self.runs[11]["id"] + "']").wait_for()
            root.locator("[name=q]").fill("UNIQUE")
            root.get_by_text("Showing 1–1 of 1 runs").wait_for()
            root.locator("[name=q]").fill("")
            root.get_by_text("Showing 1–10 of 15 runs").wait_for()
            root.locator("[name=status]").select_option("error")
            root.get_by_text("Showing 1–4 of 4 runs").wait_for()
            root.get_by_role("button", name="Refresh").click()
            root.locator("text=No completed backtest is linked to this run.").wait_for()
            self.assertFalse(errors, errors)
            browser.close()
