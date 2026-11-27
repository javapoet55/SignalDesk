"""Strategies fixes from the module audit: runs cut off by a restart or by an unexpected error must
not stay marked "running" for ever."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import strategies as sm
from test_strategies import wait_idle

ZONE = ZoneInfo("America/Los_Angeles")


def result(root, rid, **meta):
    folder = root / "strategies" / "results" / rid
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps(dict(dict(id=rid, strategy="fmfi", mode="close"), **meta)))
    (folder / "output.txt").write_text("Strategy: fmfi\n")
    return folder


class Base(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data, True)
        self.reports = self.data / "reports"
        self.reports.mkdir()
        self.logs = []

    def manager(self):
        return sm.StrategyManager(self.data, self.reports, ZONE, {"STRATEGY_TIMEOUT_SEC": "30"}, log=self.logs.append)


class RestartClosesInterruptedRuns(Base):
    def test_only_runs_still_marked_running_are_closed(self):
        cut = result(self.data, "2026-11-26_120000_fmfi", status="running", started_utc="2026-11-26T20:00:00+00:00")
        done = result(self.data, "2026-11-25_120000_fmfi", status="ok", finished_utc="2026-11-25T20:01:00+00:00")
        failed = result(self.data, "2026-11-24_120000_fmfi", status="error", error="no scan")
        stopped = result(self.data, "2026-11-23_120000_fmfi", status="cancelled")
        stray = self.data / "strategies" / "results" / "notes"
        stray.mkdir()
        (stray / "meta.json").write_text(json.dumps(dict(status="running")))
        before = {p: (p / "meta.json").read_bytes() for p in (done, failed, stopped, stray)}

        st = self.manager()

        meta = sm.read_meta(cut)
        self.assertEqual((meta["status"], meta["error"]), ("error", "Interrupted by a server restart"))
        self.assertEqual(meta["started_utc"], "2026-11-26T20:00:00+00:00")          # the rest is kept
        self.assertIn("finished_utc", meta)
        log = (cut / "output.txt").read_text()
        self.assertTrue(log.startswith("Strategy: fmfi\n"))
        self.assertIn("the server restarted while this run was in progress", log)
        for folder, raw in before.items():
            self.assertEqual((folder / "meta.json").read_bytes(), raw, folder.name)
        self.assertEqual(sum("closed after a restart" in line for line in self.logs), 1)
        # It no longer blocks deletion, and a second start changes nothing more.
        again = (cut / "meta.json").read_bytes()
        self.manager()
        self.assertEqual((cut / "meta.json").read_bytes(), again)
        self.assertIn("deleted", st.delete_result(cut.name))
        self.assertFalse(cut.exists())

    def test_a_linked_folder_is_never_followed(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "meta.json").write_text(json.dumps(dict(status="running")))
        results = self.data / "strategies" / "results"
        results.mkdir(parents=True)
        (results / "2026-11-26_120000_fmfi").symlink_to(outside, target_is_directory=True)
        self.manager()
        self.assertEqual(json.loads((outside / "meta.json").read_text()), {"status": "running"})

    def test_a_fresh_install_starts_cleanly(self):
        st = self.manager()
        self.assertEqual(st.results(), [])
        self.assertFalse(self.logs and any("interrupted" in line for line in self.logs))


class UnexpectedFailureClosesTheRun(Base):
    def test_the_result_is_marked_failed_and_the_worker_keeps_going(self):
        st = self.manager()
        self.assertIsNone(st.sandbox_error)
        st.upload_strategy("mine.py", b"def finviz_signals(tickers, asof, cache):\n    return []\n")
        calls = []

        def broken(scan):
            calls.append(scan)
            raise RuntimeError("disk went away")

        st._resolve_scan = broken
        st.enqueue("mine", "latest", "close", "manual")
        wait_idle(st)
        first = st.results()[0]
        self.assertEqual((first["status"], first["error"]), ("error", "RuntimeError: disk went away"))
        self.assertIn("finished_utc", first)
        self.assertIn("Stopped by an unexpected error: RuntimeError: disk went away",
                      (st.results_dir / first["id"] / "output.txt").read_text())
        self.assertIsNone(st.current)
        # The worker is still alive: the next job runs and reports its own, ordinary outcome.
        del st._resolve_scan
        st.enqueue("mine", "latest", "close", "manual")
        wait_idle(st)
        outcomes = sorted((m["status"], m.get("error")) for m in st.results())
        self.assertEqual(outcomes, [("error", "RuntimeError: disk went away"), ("error", "no scan")])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
