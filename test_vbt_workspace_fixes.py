"""Regression coverage for the VectorBT workspace audit fixes."""

import csv
import io
import json
import os
import re
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import backtests
import vectorbt_workspace as vb

KEY = "abc00000-0000-0000-0000-000000000001"


def cohort_job(**extra):
    info = dict(
        run="2026-10-30_160000_daily", strategy="daily", symbol="3 buys", source="Run buy signals",
        holds=[1, 5], fee_bps=10, signal_day="2026-10-01",
        dates=["2026-10-02", "2026-10-05", "2026-10-09"],
        prices={"ABC": [1250.5, 1300.25, 1310], "XYZ": [20, 19, 21], "NOP": [None, None, None]},
        **extra,
    )
    outcomes = {"ABC": [3.77, 4.5], "XYZ": [-5.19, 4.8], "NOP": ["Entry price unavailable"] * 2}
    return {"inputs": info, "result": {"engine": "test", "outcomes": outcomes}, "error": None}


def replay_job(result=True, error=None, **extra):
    days = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6)]
    info = dict(run="2026-10-30_160000_daily", strategy="daily", symbol="ABC", holds=[1], fee_bps=0,
                source="Historical date-range replay", **extra)
    fill = dict(entry=days[1], exit=days[2], buy=10, sell=9, pnl=-10000.0, pct=-10.0, hold=1)
    full = dict(equity=[100000, 100000, 90000, 90000], hold=1, trades=1, fills=[fill])
    return {"inputs": info, "error": error,
            "result": dict(engine="test", days=days, parameters=[], full_period=full) if result else None}


def summary(job):
    with patch("dow_benchmark.equity", return_value=[]):
        html = vb.report(job, KEY, vb.report_data(job, ""), "All")
    return re.search(r"Results Summary</h2><p>(.*?)</p>", html)[1]


class CsvExport(unittest.TestCase):
    def test_money_and_returns_are_plain_numbers(self):
        job = cohort_job()
        table = list(csv.reader(io.StringIO(vb.export("csv", job["inputs"], vb.report_data(job, "")).decode("utf-8-sig"))))
        self.assertEqual(table[0][3:8], ["Entry price (USD)", "Exit date", "Exit price (USD)", "Net P/L (USD)", "Return (%)"])
        rows = {row[0]: row for row in table[1:]}
        allocation = 100000 / 3
        self.assertEqual(float(rows["ABC"][3]), 1250.5)
        self.assertEqual(float(rows["ABC"][5]), 1300.25)
        self.assertAlmostEqual(float(rows["ABC"][6]), allocation * 0.0377, places=2)
        self.assertAlmostEqual(float(rows["XYZ"][6]), -allocation * 0.0519, places=2)
        self.assertEqual(float(rows["XYZ"][7]), -5.19)
        # A spreadsheet total of the P/L column must equal the realized net profit.
        self.assertAlmostEqual(sum(float(r[6]) for r in table[1:] if r[6]), allocation * (0.0377 - 0.0519), places=2)
        self.assertEqual(rows["NOP"][3:9], ["", "2026-10-05", "", "", "", "Entry price unavailable"])
        for row in table[1:]:
            self.assertNotRegex(",".join(row[3:8]), r"[$−%]")

    def test_target_column_and_older_results_without_raw_values(self):
        job = cohort_job(profit_target_pct=2.0)
        job["result"]["target_hits"] = {"ABC": [True, True], "XYZ": [False, True], "NOP": [None, None]}
        metrics = vb.report_data(job, "")
        table = list(csv.reader(io.StringIO(vb.export("csv", job["inputs"], metrics).decode("utf-8-sig"))))
        self.assertEqual(table[0][-1], "Target outcome (2%)")
        self.assertEqual([row[-1] for row in table[1:]], ["Hit", "—", "Missed"])
        del metrics["row_values"]
        self.assertIn(b"ABC,Long", vb.export("csv", job["inputs"], metrics))


class ResultsSummary(unittest.TestCase):
    def test_each_note_is_a_sentence(self):
        self.assertEqual(summary(replay_job()), "Backtest completed.")
        self.assertEqual(
            summary(replay_job(warmup_sessions={"ABC": 3})),
            "Backtest completed. The strategy held cash during indicator warm-up for ABC (3 sessions).",
        )
        target = summary(replay_job(profit_target_pct=2.0))
        self.assertTrue(target.startswith("Backtest completed. A win means the price reached 2% above entry"), target)
        self.assertTrue(summary(cohort_job(profit_target_pct=2.0)).startswith("Backtest completed · 0 pending. A win"))

    def test_replay_notes_wait_for_a_finished_backtest(self):
        basket = dict(symbols=["ABC", "XYZ"], warmup_sessions={"ABC": 3})
        self.assertEqual(summary(replay_job(result=False, **basket)),
                         "Queued or running. This page checks for the result every few seconds.")
        self.assertEqual(
            summary(replay_job(result=False, error="All symbols must cover the same sessions.", **basket)),
            "All symbols must cover the same sessions.",
        )
        self.assertIn("Starting capital is split equally", summary(replay_job(**basket)))

    def test_single_session_hold_link(self):
        job = cohort_job()
        with patch("dow_benchmark.equity", return_value=[]):
            html = vb.report(job, KEY, vb.report_data(job, ""), "All")
        self.assertIn(">1 session</a>", html)
        self.assertIn(">5 sessions</a>", html)


class Submission(unittest.TestCase):
    def test_commission_errors_are_readable(self):
        st = SimpleNamespace(results_dir=None)
        for value in ("abc", "1,5"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Enter the commission as a percentage"):
                vb.submit(st, dict(rid="x", symbols="ABC", holds="5", commission=value), lambda _: [])
        # A blank commission means no fee, and validation continues to the next field.
        with self.assertRaisesRegex(ValueError, "Choose at least one symbol"):
            vb.submit(st, dict(rid="x", symbols="", holds="5", commission=""), lambda _: [])


class ReplayTimeLimit(unittest.TestCase):
    def test_five_minutes_by_default_within_the_strategy_limit(self):
        long, short = SimpleNamespace(timeout=900), SimpleNamespace(timeout=60)
        self.assertEqual(backtests.replay_timeout(long, {}), 300)
        self.assertEqual(backtests.replay_timeout(short, {}), 60)
        self.assertEqual(backtests.replay_timeout(long, {"VBT_REPLAY_TIMEOUT_SEC": "600"}), 600)
        self.assertEqual(backtests.replay_timeout(long, {"VBT_REPLAY_TIMEOUT_SEC": "5"}), 30)
        self.assertEqual(backtests.replay_timeout(long, {"VBT_REPLAY_TIMEOUT_SEC": "99999"}), 900)
        self.assertEqual(backtests.replay_timeout(long, {"VBT_REPLAY_TIMEOUT_SEC": "soon"}), 300)

    def test_replay_passes_the_limit_to_the_sandbox(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s.py").write_text("")
            manager = SimpleNamespace(scripts_dir=root, timeout=900, mem_mb=512,
                                      meta=lambda: {"s": {"kind": "generic", "file": "s.py"}})
            with (patch("strategies.run_sandboxed", return_value=(1, "")) as sandbox, patch("strategies._chown_nobody"),
                  self.assertRaisesRegex(ValueError, "failed or timed out")):
                backtests.replay(manager, {"strategy": "s", "asof": "2026-10-30"}, root / "c.json", "ABC", 20)
            self.assertEqual(sandbox.call_args.args[3:5], (300, 300))


class MissingJob(unittest.TestCase):
    def response(self, code):
        return urllib.error.HTTPError("http://vbt.invalid/jobs/x", code, "status", {}, io.BytesIO(b"{}"))

    def test_service_separates_missing_jobs_from_outages(self):
        env = dict(VBT_SERVICE_URL="http://vbt.invalid", VBT_SERVICE_TOKEN="fixture")
        with patch.dict(os.environ, env):
            with patch("urllib.request.urlopen", side_effect=self.response(404)):
                with self.assertRaisesRegex(backtests.JobNotFound, "not found"):
                    backtests.service("/jobs/" + KEY)
            for failure in (self.response(500), self.response(422), OSError("refused")):
                with patch("urllib.request.urlopen", side_effect=failure):
                    with self.assertRaisesRegex(ValueError, "service is unavailable") as caught:
                        backtests.service("/jobs/" + KEY)
                    self.assertNotIsInstance(caught.exception, backtests.JobNotFound)


class PortalRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_server import LiveServer

        cls.server = LiveServer()
        cls.origin = {"Origin": f"http://127.0.0.1:{cls.server.port}"}
        cls.env = patch.dict(os.environ, VBT_SERVICE_URL="http://vbt.invalid", VBT_SERVICE_TOKEN="fixture")
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.server.close()

    def test_missing_job_message_on_workspace_and_report(self):
        def service(path, data=None):
            if path == "/jobs":
                return []
            raise backtests.JobNotFound("This backtest was not found. It may have been removed.")

        with patch("backtests.service", side_effect=service):
            response, body = self.server.request("GET", "/vectorbt?job=" + KEY)
            self.assertEqual(response.status, 200)
            self.assertIn("This backtest was not found.", body.decode())
            self.assertNotIn("service unavailable", body.decode())
            response, body = self.server.request("GET", "/strategies/backtests/" + KEY)
            self.assertEqual(response.status, 404)

    def test_large_recovery_selection_is_accepted(self):
        symbols = ",".join(f"T{i:04d}" for i in range(4000))
        form = dict(rid="2026-10-30_160000_daily", symbols=symbols, holds="5", commission="0.1",
                    start="2026-07-01", end="2026-10-30")
        with patch("backtests.service", return_value=[]), patch("vectorbt_workspace.submit", return_value=KEY) as submit:
            response, _ = self.server.request("POST", "/vectorbt/run", form=form, headers=self.origin)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/vectorbt?job=" + KEY)
        self.assertEqual(submit.call_args.args[1]["symbols"], symbols)
        form["symbols"] = "X" * 200000
        response, _ = self.server.request("POST", "/vectorbt/run", form=form, headers=self.origin)
        self.assertEqual(response.status, 413)


if __name__ == "__main__":
    unittest.main()
