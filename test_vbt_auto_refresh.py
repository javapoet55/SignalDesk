"""A queued backtest reloads itself until the result arrives (optional Chrome suite)."""

import os
import unittest
from unittest.mock import patch

from test_server import LiveServer

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

KEY = "abc00000-0000-0000-0000-000000000001"
DAYS = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6)]
INFO = dict(run="2026-10-30_160000_daily", strategy="daily", symbol="ABC", holds=[1], fee_bps=0, source="Archived signals")
FULL = dict(equity=[100000, 100000, 101000, 101000], hold=1, trades=1,
            fills=[dict(entry=DAYS[1], exit=DAYS[2], buy=10, sell=10.1, pnl=1000.0, pct=1.0, hold=1)])
DONE = dict(engine="test", days=DAYS, parameters=[], full_period=FULL)


class PendingFlag(unittest.TestCase):
    def test_only_a_queued_job_polls(self):
        server = LiveServer()
        self.addCleanup(server.close)
        for result, error, flag in ((None, None, "true"), (DONE, None, "false"), (None, "failed", "false")):
            def service(path, data=None):
                return [] if path == "/jobs" else {"inputs": INFO, "result": result, "error": error, "elapsed_seconds": 1}

            with (patch.dict(os.environ, VBT_SERVICE_URL="http://vbt.invalid", VBT_SERVICE_TOKEN="fixture"),
                  patch("backtests.service", side_effect=service), patch("dow_benchmark.equity", return_value=[])):
                _, body = server.request("GET", "/vectorbt?job=" + KEY)
            self.assertIn(f"const VB_PENDING={flag};", body.decode())


@unittest.skipUnless(sync_playwright and os.getenv("PORTAL_BROWSER_TEST"), "optional Chrome suite")
class AutoRefresh(unittest.TestCase):
    def test_result_appears_without_a_click_and_edits_pause_polling(self):
        server = LiveServer()
        self.addCleanup(server.close)
        lookups = []
        ready_after = [2]

        def service(path, data=None):
            if path == "/jobs":
                return []
            lookups.append(path)
            return {"inputs": INFO, "error": None, "elapsed_seconds": 1,
                    "result": DONE if len(lookups) > ready_after[0] else None}

        with (patch.dict(os.environ, VBT_SERVICE_URL="http://vbt.invalid", VBT_SERVICE_TOKEN="fixture"),
              patch("backtests.service", side_effect=service), patch("dow_benchmark.equity", return_value=[]),
              sync_playwright() as pw):
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
            self.assertIn("Queued or running", page.locator("body").inner_text())
            page.get_by_text("Backtest completed.").wait_for(timeout=20000)
            self.assertNotIn("poll=", page.url)  # the counter is removed once the result is shown
            self.assertEqual(page.locator("svg.vb-plot").count(), 2)
            # While the form is being edited the page must not reload underneath the user.
            del lookups[:]
            ready_after[0] = 10 ** 6
            page.goto(base + "/vectorbt?job=" + KEY)
            page.get_by_label("Hold (trading days)").fill("7")
            seen = len(lookups)
            page.wait_for_timeout(7000)
            self.assertEqual(len(lookups), seen)
            self.assertEqual(page.get_by_label("Hold (trading days)").input_value(), "7")
            # The polling stops at the limit instead of running forever.
            page.goto(base + "/vectorbt?job=" + KEY + "&poll=120")
            seen = len(lookups)
            page.wait_for_timeout(5000)
            self.assertEqual(len(lookups), seen)
            self.assertFalse(errors)
            browser.close()
