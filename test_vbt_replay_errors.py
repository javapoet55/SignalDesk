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

    def test_strategy_output_after_the_error_is_not_mistaken_for_the_reason(self):
        # A scanner prints one line per ticker. Buffered output reaches the log after the traceback.
        def printed_after(args, cwd, log, *_):
            Path(log).write_text(
                "  BUY HPQ        as of 2026-07-01\n"
                "Traceback (most recent call last):\n"
                '  File "strategy_runner.py", line 620, in main\n'
                "    return replay_daily(a)\n"
                "ValueError: Strategy could not evaluate KSS on 2026-07-02: No data returned\n"
                "  ERROR T0353      Insufficient history (7 candles); need at least 48Traceback (most recent call last):\n"
                "  File x\n"
                "ValueError: Strategy could not evaluate KSS on 2026-07-02: No data returned\n"
                "  ERROR OCTV       Insufficient history (7 candles); need at least 48\n"
                "  ERROR ZZZZ       Insufficient history (9 candles); need at least 48\n")
            return 3, ""

        ok = "def finviz_signals(tickers, day, cache):\n    return []\n"
        with self.assertRaisesRegex(
                ValueError, r"^Historical Python replay stopped\. Strategy could not evaluate KSS on 2026-07-02: No data returned\.$"):
            self.replay(ok, sandbox=printed_after)

        def chained(args, cwd, log, *_):
            Path(log).write_text(
                "Traceback (most recent call last):\n  File x\nKeyError: 'c'\n\n"
                "During handling of the above exception, another exception occurred:\n\n"
                "Traceback (most recent call last):\n  File y\n    raise RuntimeError('bad bar')\n"
                "RuntimeError: bad bar\nScan finished: 3 tickers\n")
            return 3, ""

        with self.assertRaisesRegex(ValueError, r"replay stopped\. RuntimeError: bad bar\."):
            self.replay(ok, sandbox=chained)

        def only_prints(args, cwd, log, *_):
            Path(log).write_text("  ERROR OCTV       Insufficient history (7 candles); need at least 48\n")
            return 3, ""

        with self.assertRaisesRegex(ValueError, "failed or timed out"):  # no error found: say so plainly
            self.replay(ok, sandbox=only_prints)

    def test_runner_writes_the_error_after_the_strategy_output(self):
        import os
        import subprocess
        import sys

        # Production runs the strategy with normal block buffering; a test machine may not.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s.py").write_text(
                "def finviz_signals(tickers, day, cache):\n"
                "    for i in range(400):\n"
                "        print(f'  ERROR T{i:04d}      Insufficient history (7 candles); need at least 48')\n"
                "    return [dict(Ticker='ABC', Status='ERROR', Error='No data returned')]\n")
            (root / "c.json").write_text(json.dumps({"history": {f"2026-11-{d:02d}": {"ABC": {"o": 1, "c": 1}}
                                                                 for d in range(2, 9)}}))
            log = root / "replay.log"
            with open(log, "w") as handle:  # a file, so the child's stdout is block-buffered as in production
                code = subprocess.run([sys.executable, "strategy_runner.py", "replay", str(root / "s.py"), "--symbol", "ABC",
                                       "--cache", str(root / "c.json"), "--asof", "2026-11-30", "--output", str(root / "o.json"),
                                       "--sessions", "4"], stdout=handle, stderr=subprocess.STDOUT, env=env).returncode
            self.assertEqual(code, 3)
            self.assertEqual(log.read_text().strip().splitlines()[-1],
                             "ValueError: Strategy could not evaluate ABC on 2026-11-05: No data returned")
            self.assertEqual(backtests.replay_failure(log, 512),
                             "Historical Python replay stopped. Strategy could not evaluate ABC on 2026-11-05: No data returned.")

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
