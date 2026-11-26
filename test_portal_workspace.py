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
        for name in ("crsi_cross55","crsi_scanner","daily_scanner","fmfi_scanner","macdgen_v5","nqqe_banker","nqqe_scanner","swing_scanner"):
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
            page.locator("#row-all").uncheck()
            page.locator(".srow input").first.check()
            self.assertTrue(page.locator(".top2").is_visible())
            self.assertTrue(page.locator(".sd-right").is_visible())
            self.assertLessEqual(page.locator(".sd-right h2 .sd-icon").first.bounding_box()["width"], 24)
            self.assertEqual(page.locator(".sd-metric").count(), 4)
            page.get_by_role("radio", name="Custom time").check()
            self.assertEqual(page.evaluate("new FormData(document.querySelector('#go')).get('when')"), "once")
            page.get_by_role("radio", name="Run now").check()
            tabs.get_by_text("Configure & Run").click()
            page.locator(".run-filters input").first.fill("17")
            tabs.get_by_text("Backtest History").click()
            page.get_by_text("VectorBT PRO is unavailable.", exact=False).wait_for()
            page.go_back()
            self.assertEqual(page.locator(".run-filters input").first.input_value(), "17")
            self.assertEqual(page.locator(".srow input:checked").count(), 1)
            page.get_by_role("radio", name="Schedule (recurring)").check()
            page.locator("#time").fill("13:00")
            page.locator("#gobtn").click()
            page.wait_for_url("**/strategies#activity")
            self.assertEqual(len(host.app.schedules.jobs("strategies")), 1)
            tabs.get_by_text("Configure & Run").click()
            page.get_by_role("radio", name="Run now").check()
            for width in (1536, 1024, 390):
                page.set_viewport_size({"width": width, "height": 1024})
                for key in ("configure", "scheduled", "history", "backtests", "versions", "settings"):
                    tab = tabs.locator(f"[data-workspace-tab={key}]")
                    tab.click()
                    self.assertEqual(tab.get_attribute("aria-current"), "page")
                    heading = page.locator({"settings":"#ss-root h1", "history":"#rh-root h1"}.get(key,".sd-hero h1"))
                    heading.wait_for(state="visible")
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width + 1, key)
                    folder = os.getenv("STUDIO_SCREENSHOTS")
                    if folder:
                        page.screenshot(path=str(Path(folder) / f"{width}-{key}.png"))
            page.goto(base + "/strategies?tab=scheduled")
            page.get_by_role("button", name="Create schedule").click()
            self.assertEqual(page.locator("[name=when]:checked").input_value(), "weekdays")
            page.keyboard.press("Escape")
            self.assertTrue(page.get_by_role("button", name="Menu").is_visible())
            page.get_by_role("button", name="Day").click()
            self.assertEqual(page.locator("html").get_attribute("data-theme"), "light")
            for route in ("/", "/paper", "/journal", "/notes", "/notebook", "/tradingview", "/strategies/archive"):
                self.assertEqual(page.goto(base + route).status, 200)
                self.assertEqual(page.locator("#studio-rail").count(), 1)
            for width in (2560, 1024, 390):
                page.set_viewport_size({"width": width, "height": 940})
                page.goto(base + "/")
                rail = page.locator("#studio-rail")
                expected = rail.locator("nav a").all_text_contents()
                for label, module_tabs in (("Strategies", "Strategy workspace"), ("VectorBT PRO", "Strategies Studio"), ("Paper Trading", "Paper trading")):
                    if width < 761:
                        page.get_by_role("button", name="Menu").click()
                    rail.get_by_role("link", name=label, exact=True).click()
                    self.assertEqual(rail.locator("nav a").all_text_contents(), expected)
                    self.assertEqual(rail.locator("[aria-current=page]").inner_text(), label)
                    local_tabs = page.get_by_role("navigation", name=module_tabs, exact=True)
                    self.assertTrue(local_tabs.is_visible())
                    if width > 760:
                        self.assertTrue(rail.is_visible())
                        self.assertLessEqual(rail.bounding_box()["x"] + rail.bounding_box()["width"], local_tabs.bounding_box()["x"])
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width + 1)
                    if os.getenv("STUDIO_SCREENSHOTS"):
                        page.screenshot(path=str(Path(os.environ["STUDIO_SCREENSHOTS"]) / f"navigation-{width}-{label}.png"))
            self.assertFalse(errors, errors)
            browser.close()
