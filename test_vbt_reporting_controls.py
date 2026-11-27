import io
import unittest
from unittest.mock import patch

import dow_benchmark
from vectorbt_workspace import history_table, history_timestamp, report_data, time_taken, trade_text
from vbt_service.performance import performance


class BacktestHistoryTime(unittest.TestCase):
    def test_pacific_dates_and_daylight_saving(self):
        for value, expected in (
            ("2026-11-27T00:52:11.587990+00:00", "26-NOV-2026 @ 16:52:11 PST"),
            ("2026-07-01T05:01:02Z", "30-JUN-2026 @ 22:01:02 PDT"),
            ("2026-11-01T08:30:00Z", "01-NOV-2026 @ 01:30:00 PDT"),
            ("2026-11-01T09:30:00Z", "01-NOV-2026 @ 01:30:00 PST"),
            ("2026-11-26", "26-NOV-2026"),
            (None, "—"),
        ):
            with self.subTest(value=value):
                self.assertEqual(history_timestamp(value), expected)


class BacktestHistoryPerformance(unittest.TestCase):
    def test_replay_scores_only_known_target_outcomes(self):
        result = {"full_period": {"equity": [100000, 97000], "fills": [
            {"pnl": 100, "target_hit": True}, {"pnl": 50, "target_hit": False},
            {"pnl": -10, "target_hit": None}]}}
        stats = performance({"profit_target_pct": 2}, result)
        self.assertEqual(stats.win_rate_pct, 50)
        self.assertAlmostEqual(stats.total_return_pct, -3)
        ordinary = performance({}, result)
        self.assertAlmostEqual(ordinary.win_rate_pct, 200 / 3)

    def test_buy_basket_defaults_to_first_hold_and_keeps_partial_valuations(self):
        info = {"dates": ["2026-07-01", "2026-07-02", "2026-07-09"],
                "fee_bps": 0, "close_marks": {"2026-07-01": 1.01}}
        result = {"outcomes": {"A": [0, 10], "B": ["Pending exit", -2]}}
        first = performance(info, result)
        self.assertEqual(first.win_rate_pct, 0)
        self.assertAlmostEqual(first.total_return_pct, 1)
        second = performance(info, result, 1)
        self.assertEqual((second.win_rate_pct, second.total_return_pct), (50, 4))

    def test_missing_and_zero_are_distinct(self):
        job = dict(id="test", strategy="daily", symbol="AAA", status="Queued or running")
        self.assertIn("<td>—</td><td>—</td><td>Queued or running", history_table([job]))
        job.update(status="Completed", win_rate_pct=None, total_return_pct=0)
        self.assertIn("<td>—</td><td>0.00%</td><td>Completed", history_table([job]))


class BacktestDuration(unittest.TestCase):
    def test_finished_pending_and_legacy_runs(self):
        for elapsed, label in ((0.1, "Less than 1 second"), (1, "1 second"),
                               (29.8, "30 seconds"), (59.8, "1 minute"), (121, "2 minutes 1 second")):
            with self.subTest(elapsed=elapsed):
                self.assertEqual(time_taken({"elapsed_seconds": elapsed}), label)
        self.assertEqual(time_taken(None), "—")
        self.assertEqual(time_taken({"result": None, "error": None}), "In progress")
        self.assertEqual(time_taken({"result": {"engine": "older"}}), "Not recorded")
        self.assertEqual(time_taken({"error": "failed"}), "Not recorded")


class DowBenchmark(unittest.TestCase):
    def test_neutral_start_exact_trading_dates_and_stale_cache(self):
        days = ["2026-07-01", "2026-07-02", "2026-07-06"]
        csv = b"observation_date,DJIA\n2026-07-01,50000\n2026-07-02,50500\n2026-07-03,.\n2026-07-06,51000\n"
        with (
            patch("dow_benchmark._closes", {}),
            patch("dow_benchmark._refresh_at", 0.0),
            patch("dow_benchmark.time.monotonic", return_value=100),
            patch(
                "dow_benchmark.urllib.request.urlopen",
                side_effect=[io.BytesIO(csv), OSError()],
            ) as fetch,
        ):
            self.assertEqual(
                dow_benchmark.equity(days, 100000), [100000, 101000, 102000]
            )
            self.assertEqual(dow_benchmark.equity([*days, "2026-07-07"], 100000), [])
            self.assertEqual(fetch.call_count, 1)
            with patch("dow_benchmark.time.monotonic", return_value=4000):
                self.assertEqual(dow_benchmark.equity(days, 100000)[-1], 102000)
                self.assertEqual(fetch.call_count, 2)


class TradeLists(unittest.TestCase):
    def test_pending_and_flat_trades_and_raw_profit_sign(self):
        outcomes = {
            "AAA": [0.000001],
            "BBB": [-0.000001],
            "CCC": [0],
            "DDD": ["Pending exit"],
        }
        info = dict(
            run="sample",
            strategy="sample",
            symbol="4 buys",
            holds=[5],
            dates=["2026-07-01", "2026-07-09"],
            signal_day="2026-06-30",
            fee_bps=0,
            prices={s: [10, 10] for s in outcomes},
        )
        metrics = report_data(
            dict(
                inputs=info, result=dict(engine="test", outcomes=outcomes), error=None
            ),
            "5",
        )
        for group, expected in (
            ("all", {"AAA", "BBB", "CCC"}),
            ("wins", {"AAA"}),
            ("losses", {"BBB"}),
        ):
            contents = trade_text(info, metrics, group)
            actual = {
                line.split("\t")[0]
                for line in contents.splitlines()
                if "\tLong\t" in line
            }
            self.assertEqual(actual, expected)
            self.assertIn("Holding period 5 trading days", contents)
            self.assertIn("2026-07-09", contents)
