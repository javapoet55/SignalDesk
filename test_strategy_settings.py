"""Check Settings through the same authenticated routes used by the portal."""

import os
import unittest
from pathlib import Path

import strategy_settings as settings
from test_portal_workspace import sync_playwright
from test_server import _REAL_PROTECTED_CHANGE_LABEL, LiveServer


class StrategySettingsTests(unittest.TestCase):
    def setUp(self):
        self.host = LiveServer()
        self.addCleanup(self.host.close)
        self.manager = self.host.app.strategies
        self.source = b"# offline fixture"
        self.manager._save_meta({"momentum": {"file": "momentum.py", "kind": "script"}})
        (self.manager.scripts_dir / "momentum.py").write_bytes(self.source)

    def test_snapshot_and_reviewed_purge(self):
        settings.change(self.manager, "snapshot", {"name": "momentum"}, "admin")
        (snapshot,) = self.manager.archive_entries("versions")
        self.assertEqual(
            self.manager.vault.read("versions", snapshot["id"])[1], self.source
        )
        self.assertEqual(snapshot["metadata"]["snapshot_by"], "admin")
        self.manager.delete_strategy("momentum")
        original = self.manager.archive_entries("trash")
        self.manager.vault.save("trash", "second", {}, self.source)
        with self.assertRaisesRegex(ValueError, "changed"):
            settings.change(
                self.manager, "purge", {"review": settings.token(original)}, "admin"
            )
        reviewed = self.manager.archive_entries("trash")
        self.assertEqual(len(reviewed), 2)
        settings.change(
            self.manager,
            "purge",
            {"review": settings.token(reviewed), "entry": original[0]["id"]},
            "admin",
        )
        self.assertEqual(
            [r["name"] for r in self.manager.archive_entries("trash")], ["second"]
        )
        self.assertEqual(len(self.manager.archive_entries("versions")), 1)
        for action in ("reset", "purge", "snapshot"):
            self.assertTrue(
                _REAL_PROTECTED_CHANGE_LABEL(settings.ROOT + action, b"", "")
            )

    def test_preferences_and_panel_auth(self):
        response, _ = self.host.request("GET", settings.ROOT + "panel", auth=None)
        self.assertIn(response.status, (303, 401))
        response, _ = self.host.request(
            "POST",
            settings.ROOT + "data",
            form={"mode": "intraday", "candles": "15m"},
            headers={"Origin": "https://outside.invalid"},
        )
        self.assertEqual(response.status, 403)
        for action, fields in (
            ("alerts", {"failure": "on"}),
            ("data", {"mode": "intraday", "candles": "15m"}),
        ):
            response, _ = self.host.request("POST", settings.ROOT + action, form=fields)
            self.assertEqual(response.getheader("Location"), "/strategies?tab=settings")
        self.assertEqual(
            settings.prefs(self.manager),
            {"mode": "intraday", "candles": "15m", "alerts": ["failure"]},
        )
        with self.assertRaises(ValueError):
            settings.change(
                self.manager, "data", {"mode": "intraday", "candles": "0d"}, "admin"
            )
        self.manager.save_selection(["momentum"], "latest", "close", "now")
        settings.change(self.manager, "reset", {}, "admin")
        self.assertEqual(settings.prefs(self.manager), {})
        self.assertEqual(self.manager.selection()["names"], ["momentum"])

    @unittest.skipUnless(
        sync_playwright and os.getenv("PORTAL_BROWSER_TEST"), "optional Chrome suite"
    )
    def test_render_and_controls(self):
        manager = self.manager
        for i in range(3):
            manager.vault.save(
                "versions",
                "momentum",
                {"description": f"Snapshot {i + 1}", "snapshot_by": "admin"},
                self.source + str(i).encode(),
            )
        manager.vault.save("trash", "old_scanner", {}, self.source)
        base = f"http://127.0.0.1:{self.host.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome")
            page = browser.new_page(
                color_scheme="dark", viewport={"width": 1536, "height": 1024}
            )
            page.goto(base + "/login")
            page.locator("[name=username]").fill("admin")
            page.locator("[name=password]").fill("pw-123")
            page.locator("form.login-form button[type=submit]").click()
            page.goto(base + "/strategies?tab=settings")
            root = page.locator("#ss-root")
            root.wait_for()
            for width in (1536, 1024, 390):
                page.set_viewport_size({"width": width, "height": 1024})
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), width + 1
                )
                self.assertEqual(root.locator(".ss-card:visible").count(), 6)
                folder = os.getenv("STUDIO_SCREENSHOTS")
                if folder:
                    page.screenshot(
                        path=str(Path(folder) / f"settings-detail-{width}.png"),
                        full_page=True,
                    )
            page.set_viewport_size({"width": 1536, "height": 1024})
            root.get_by_role("button", name="View", exact=True).last.click()
            self.assertTrue(root.locator("dialog[open]").is_visible())
            page.keyboard.press("Escape")
            page.keyboard.press("Control+k")
            root.locator("#ss-search").fill("Recycle Bin")
            self.assertFalse(
                root.get_by_role("heading", name="Data Configuration").is_visible()
            )
            root.locator("#ss-search").fill("")
            root.locator("summary[aria-label='Open notifications']").click()
            self.assertTrue(root.locator(".ss-bell[open]").is_visible())
            root.locator("summary[aria-label='Open notifications']").click()
            root.get_by_role("switch", name="Run completion").uncheck()
            root.get_by_role("button", name="Save notifications").click()
            root.wait_for()
            self.assertFalse(
                root.get_by_role("switch", name="Run completion").is_checked()
            )
            root.get_by_label("Price Basis", exact=True).select_option("intraday")
            root.get_by_label("Default Candles", exact=True).select_option("5d")
            root.get_by_role("button", name="Save defaults").click()
            page.get_by_role("navigation", name="Strategy workspace").get_by_text("Configure & Run").click()
            self.assertEqual(page.locator("#go [name=mode]").input_value(), "intraday")
            self.assertEqual(page.locator("#go [name=cn]").input_value(), "5")
            browser.close()
