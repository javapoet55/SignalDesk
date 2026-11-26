"""Exercise real portal navigation against an isolated filesystem and HTTP server."""
import os
import unittest
from pathlib import Path

from test_server import LiveServer

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright and os.getenv("PORTAL_BROWSER_TEST"), "optional Chrome suite")
class PortalWorkspaceTests(unittest.TestCase):
    def test_navigation_and_unsaved_run_configuration(self):
        host = LiveServer()
        self.addCleanup(host.close)
        base = f"http://127.0.0.1:{host.port}"
        strategies = host.app.strategies
        strategies.sandbox_error = ""
        fixture = {}
        for name in ("trend_scan", "momentum_scan"):
            filename = name + ".py"
            (strategies.scripts_dir / filename).write_text("# Offline navigation fixture\n")
            fixture[name] = {"file": filename, "kind": "script"}
        strategies._save_meta(fixture)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome")
            page = browser.new_page(color_scheme="dark")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/strategies")
            self.assertEqual(page.locator("#studio-rail").count(), 0)
            page.locator("[name=username]").fill("admin")
            page.locator("[name=password]").fill("pw-123")
            page.locator("form.login-form button[type=submit]").click()
            page.goto(base + "/strategies")
            tabs = page.locator("nav[aria-label='Strategy workspace']")
            page.locator(".srow input").first.uncheck()
            tabs.get_by_text("Run Configuration").click()
            page.locator(".run-filters input").first.fill("17")
            tabs.get_by_text("VectorBT PRO").click()
            page.get_by_text("VectorBT PRO is unavailable.", exact=False).wait_for()
            page.go_back()
            self.assertEqual(page.locator(".run-filters input").first.input_value(), "17")
            self.assertEqual(page.locator(".srow input:checked").count(), 1)
            for width in (1440, 1024, 390):
                page.set_viewport_size({"width": width, "height": 900})
                for key in ("library", "configure", "scheduled", "history", "backtests", "settings"):
                    tab = tabs.locator(f"[data-workspace-tab={key}]")
                    tab.click()
                    self.assertEqual(tab.get_attribute("aria-current"), "page")
                    self.assertTrue(page.locator("#studio-title").is_visible())
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width + 1, key)
                    folder = os.getenv("STUDIO_SCREENSHOTS")
                    if folder:
                        page.screenshot(path=str(Path(folder) / f"{width}-{key}.png"))
            page.goto(base + "/strategies?tab=scheduled")
            page.get_by_role("button", name="Create schedule").click()
            self.assertEqual(page.locator("#when").input_value(), "weekdays")
            page.keyboard.press("Escape")
            page.get_by_role("button", name="☰ Menu").click()
            self.assertTrue(page.locator(".studio-rail").is_visible())
            page.keyboard.press("Escape")
            page.get_by_role("button", name="Day").click()
            self.assertEqual(page.locator("html").get_attribute("data-theme"), "light")
            for route in ("/", "/paper", "/journal", "/notes", "/notebook", "/tradingview", "/strategies/archive"):
                self.assertEqual(page.goto(base + route).status, 200)
                self.assertEqual(page.locator("#studio-rail").count(), 1)
            self.assertFalse(errors, errors)
            browser.close()
