"""A failed historical replay tells the user why it stopped."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import backtests


class ReplayFailureReason(unittest.TestCase):
    def manager(self, root, body):
        (root / "s.py").write_text(body)
        return SimpleNamespace(scripts_dir=root, timeout=900, mem_mb=512,
                               meta=lambda: {"s": {"kind": "generic", "file": "s.py"}})

    def replay(self, body, days=6, sandbox=None):
        from strategy_runner import main

        def run_inline(args, cwd, log, *_):
            import contextlib

            with open(log, "a") as handle, contextlib.redirect_stderr(handle), contextlib.redirect_stdout(handle):
                return main(args), ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "c.json"
            cache.write_text(json.dumps({"history": {f"2026-11-{d:02d}": {"ABC": {"o": 10, "c": 10}}
                                                     for d in range(2, 2 + days)}}))
            with (patch("strategies.run_sandboxed", side_effect=sandbox or run_inline),
                  patch("strategies._chown_nobody")):
                return backtests.replay(self.manager(root, body), {"strategy": "s", "asof": "2026-11-30"}, cache, "ABC", 4)

    def test_strategy_error_names_the_symbol_date_and_cause(self):
        body = ("def finviz_signals(tickers, day, cache):\n"
                "    return [dict(Ticker='ABC', Status='ERROR', Error='High below low')]\n")
        with self.assertRaisesRegex(
                ValueError, r"^Historical Python replay stopped\. Strategy could not evaluate ABC on 2026-11-04: High below low\.$"):
            self.replay(body)

    def test_crash_inside_the_strategy_is_reported(self):
        body = "def finviz_signals(tickers, day, cache):\n    return {}['missing']\n"
        with self.assertRaisesRegex(ValueError, r"replay stopped\. KeyError: 'missing'\."):
            self.replay(body)

    def test_timeout_memory_and_silent_failures(self):
        ok = "def finviz_signals(tickers, day, cache):\n    return []\n"
        with self.assertRaisesRegex(ValueError, "ran longer than 300 seconds"):
            self.replay(ok, sandbox=lambda *a: (-1, ""))

        def out_of_memory(args, cwd, log, *_):
            Path(log).write_text("Traceback (most recent call last):\n  File x\nMemoryError\n")
            return 3, ""

        with self.assertRaisesRegex(ValueError, r"ran out of memory \(limit 512 MB\)"):
            self.replay(ok, sandbox=out_of_memory)
        with self.assertRaisesRegex(ValueError, "failed or timed out"):
            self.replay(ok, sandbox=lambda *a: (1, ""))  # nothing was logged

    def test_long_reasons_are_trimmed_and_success_is_unchanged(self):
        def noisy(args, cwd, log, *_):
            Path(log).write_text("ValueError: " + "x" * 5000 + "\n")
            return 3, ""

        with self.assertRaises(ValueError) as caught:
            self.replay("def finviz_signals(t, d, c):\n    return []\n", sandbox=noisy)
        self.assertLess(len(str(caught.exception)), 360)
        signals, days, version, warmup = self.replay(
            "def finviz_signals(t, d, c):\n    return [dict(Ticker='ABC', Status='BUY')]\n")
        self.assertEqual((len(signals), len(days), warmup), (4, 4, {}))


if __name__ == "__main__":
    unittest.main()
