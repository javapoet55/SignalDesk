"""Run with a local Chromium install and the optional Playwright test dependency."""

import datetime as dt
import os
import unittest
from pathlib import Path

from paper_trading.ledger import Ledger
from test_paper_trading import Feed
from test_server import LiveServer

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(
    sync_playwright and os.getenv("PAPER_BROWSER_TEST"), "optional browser suite"
)
class BrowserTests(unittest.TestCase):
    def test_real_session_desktop_and_mobile(self):
        host = LiveServer()
        self.addCleanup(host.close)
        from paper_trading.market import latest_close

        stamp = int(dt.datetime(2026, 11, 25, 23, tzinfo=dt.UTC).timestamp())
        feed = Feed(stamp)
        feed.closing_quote = lambda ticker, now: {
            "price": "10",
            "stamp": latest_close(now),
            "source": "Finviz daily close",
            "session": "2026-11-25",
        }
        feed.history = lambda ticker: [
            {"o": 9, "h": 11, "l": 8, "c": 10, "v": 100, "stamp": stamp}
        ]
        host.app.paper = Ledger(Path(host.tmp) / "paper.sqlite3", feed, lambda: stamp)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 860})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{host.port}/paper")
            page.locator("[name=username]").fill("admin")
            page.locator("[name=password]").fill("pw-123")
            page.locator("form.login-form button[type=submit]").click()
            page.get_by_role("link", name="Paper Trading", exact=True).first.click()
            page.get_by_label("Portfolio name", exact=True).fill("Browser test")
            page.get_by_role("button", name="Create portfolio", exact=True).click()
            page.get_by_role("heading", name="Portfolio performance").wait_for()
            page.get_by_role("button", name="New signal", exact=True).click()
            page.get_by_label("Strategy Name", exact=True).wait_for()
            page.get_by_label("Symbol", exact=True).fill("TEST")
            page.get_by_text("$10.00 · 2026-11-25 NYSE close").wait_for()
            self.assertTrue(
                page.get_by_role(
                    "button", name="Execute paper trade", exact=True
                ).is_disabled()
            )
            page.get_by_role("radio", name="Dollars").check()
            page.get_by_label("Dollar amount (USD)").fill("20")
            page.get_by_role("radio", name="Percent").check()
            page.get_by_label("Percentage of").select_option("cash_pct")
            self.assertEqual(
                page.locator("#signal input[type=radio]:checked").count(), 1
            )
            page.get_by_label("Percentage (%)").fill("101")
            page.get_by_role("button", name="Preview signal").click()
            page.get_by_text("Percentage must be at most 100.").wait_for()
            page.get_by_role("radio", name="Quantity").check()
            page.get_by_label("Share quantity").fill("2")
            page.get_by_label("Gain from buy price (%)").fill("10")
            page.get_by_label("Stop price (USD)", exact=True).fill("9")
            page.get_by_label("Sell limit price (USD)").fill("8.50")
            page.get_by_role("button", name="Preview signal").click()
            screenshot = os.getenv("PAPER_QA_SCREENSHOT")
            if screenshot:
                page.locator("#candles svg").wait_for()
                page.screenshot(path=screenshot, full_page=True)
            page.get_by_role(
                "button", name="Execute paper trade and authorize conditional sale"
            ).click()
            page.get_by_text("Simulated", exact=True).first.wait_for()
            page.locator("#tabs").get_by_role("button", name="Signal Manager").click()
            page.get_by_text("Armed", exact=True).wait_for()
            page.get_by_role("button", name="Cancel exit").click()
            page.get_by_text("Conditional sale canceled.").wait_for()
            page.set_viewport_size({"width": 390, "height": 844})
            for name in [
                "Open Positions",
                "Order History",
                "Performance Analytics",
                "Trading Journal",
                "Portfolio Settings",
                "Signal Manager",
                "Overview",
            ]:
                page.locator("#tabs").get_by_role(
                    "button", name=name, exact=True
                ).click()
                page.locator("#content .pbox").first.wait_for()
            first = page.locator("#portfolio").input_value()
            page.locator("#tabs").get_by_role(
                "button", name="Portfolio Settings"
            ).click()
            page.get_by_label("Portfolio name", exact=True).fill("Second portfolio")
            page.get_by_role("button", name="Create portfolio", exact=True).click()
            page.get_by_role("heading", name="Portfolio performance").wait_for()
            self.assertNotEqual(first, page.locator("#portfolio").input_value())
            page.locator("#portfolio").select_option(first)
            page.get_by_role("heading", name="Current positions").wait_for()
            page.locator("#content").get_by_text("TEST", exact=True).first.wait_for()
            self.assertEqual(errors, [])
            self.assertLessEqual(
                page.evaluate(
                    "document.querySelector('.paper').getBoundingClientRect().right"
                ),
                391,
            )
            browser.close()
