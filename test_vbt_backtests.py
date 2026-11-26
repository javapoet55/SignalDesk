import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backtests import dataset
from strategy_runner import replay_daily


class HistoricalSignals(unittest.TestCase):
    def test_replay_reports_unrelated_errors_and_late_history_loss(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "history.json"
            cache.write_text(json.dumps({"history": {f"2026-11-0{d}": {"ABC": {}} for d in range(2, 6)}}))
            args = SimpleNamespace(strategy="fixture.py", cache=str(cache), symbol="ABC",
                                   asof="2026-11-05", sessions=4, output=str(root / "result.json"), candle_label=None)
            for failure_day, error in (("2026-11-02", "High below low"),
                                       ("2026-11-04", "Insufficient history (2 candles); need at least 48")):
                def strategy(symbols, day, data):
                    return [{"Ticker": "ABC", "Status": "ERROR" if day == failure_day else "WAIT", "Error": error}]

                with patch("strategy_runner._import", return_value=SimpleNamespace(finviz_signals=strategy)):
                    with self.assertRaisesRegex(ValueError, "Strategy could not evaluate ABC on " + failure_day):
                        replay_daily(args)

    def test_blocks_late_archived_signals_and_other_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bars = {
                f"2026-11-{d:02d}": {"ABC": {"o": 10, "c": 11}} for d in range(2, 7)
            }

            def run(day, finished, version):
                rid = f"2026-11-{day:02d}_160000_strategy"
                folder = root / rid
                folder.mkdir()
                (folder / "meta.json").write_text(
                    json.dumps(
                        dict(
                            strategy="strategy",
                            strategy_sha256=version,
                            status="ok",
                            mode="close",
                            candles="1d",
                            asof=f"2026-11-{day:02d}",
                            finished_utc=finished,
                        )
                    )
                )
                return rid

            run(2, "2026-11-02T23:00:00+00:00", "v1")
            run(3, "2026-11-04T15:00:00+00:00", "v1")
            run(4, "2026-11-04T23:00:00+00:00", "v2")
            rid = run(6, "2026-11-06T23:00:00+00:00", "v1")
            (root / rid / "history_cache.json").write_text(
                json.dumps({"history": bars})
            )
            output = dataset(root, rid, "ABC", [1, 2], 0, lambda folder: ["ABC"])
            self.assertEqual(
                [b["day"] for b in output["bars"] if b["signal"]], ["2026-11-02"]
            )
            self.assertEqual(output["observed_days"], 1)

    def test_python_replay_only_sees_bars_through_each_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "s.py"
            path.write_text(
                "def finviz_signals(tickers, asof, cache):\n"
                "    assert max(cache['history']) == asof\n"
                "    return [{'Ticker':'ABC','Status':'BUY' if asof.endswith('03') else 'WAIT'}]\n"
            )
            cache = root / "cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "history": {
                            f"2026-11-0{i}": {"ABC": {"c": i, "o": i}}
                            for i in range(1, 7)
                        }
                    }
                )
            )
            output = root / "result.json"
            replay_daily(
                SimpleNamespace(
                    strategy=str(path),
                    cache=str(cache),
                    asof="2026-11-05",
                    symbol="ABC",
                    sessions=4,
                    candle_label=None,
                    output=str(output),
                )
            )
            result = json.loads(output.read_text())
            self.assertEqual(result["buys"], ["2026-11-03"])
            self.assertEqual(result["days"], [f"2026-11-0{i}" for i in range(2, 6)])


class BatchSignals(unittest.TestCase):
    def test_entire_buy_list_waits_for_next_exchange_open(self):
        import datetime as dt
        from backtests import cohort

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rid = "2026-11-25_133104_fmfi_scanner"
            folder = root / rid
            folder.mkdir()
            (folder / "meta.json").write_text(
                json.dumps(
                    {
                        "mode": "close",
                        "strategy": "fmfi_scanner",
                        "status": "ok",
                        "asof": "2026-11-25",
                        "finished_utc": "2026-11-25T21:32:00+00:00",
                    }
                )
            )
            manager = SimpleNamespace(
                results_dir=root, finviz=SimpleNamespace(cache_dir=root)
            )
            request = cohort(
                manager,
                rid,
                [1, 5, 10],
                1,
                lambda _: [f"S{i}" for i in range(125)],
                now=dt.datetime.fromisoformat("2026-11-26T07:30:00+00:00"),
            )
            self.assertEqual(request["dates"][:2], ["2026-11-27", "2026-11-30"])
            self.assertEqual(len(request["prices"]), 125)
            self.assertFalse(any(request["elapsed"]))
            (root / "ABC.csv").write_text(
                "Date,Open,High,Low,Close\n"
                "2026-11-27,300,302,299,301\n"
                "2026-11-30,301,306,298,299\n"
                "2026-12-01,299,320,297,302\n"
            )
            snapshot = cohort(
                manager, rid, [2, 1], 0, lambda _: ["ABC"],
                now=dt.datetime.fromisoformat("2026-12-01T15:00:00+00:00"), target=2,
            )
            self.assertEqual(snapshot["holds"], [7])
            self.assertEqual(snapshot["prices"]["ABC"], [300, None])
            self.assertEqual(snapshot["completed_sessions"], 2)
            self.assertEqual(snapshot["target_days"][-1], "2026-12-07")
            self.assertEqual(snapshot["target_bars"]["ABC"], [
                dict(open=300,high=302,close=301), dict(open=301,high=306,close=299),
                dict(open=299,high=None,close=None)])


class CalendarReplayTests(unittest.TestCase):
    def test_date_errors_report_cutoff_and_trading_session_count_in_either_order(self):
        from backtests import calendar_replay

        for a, b, message in (
            ("2026-11-26", "2026-06-01", "data through 2026-11-25"),
            ("2026-11-25", "2026-11-24", "contains 2 NYSE sessions"),
            ("2026-11-25", "2025-01-03", "4–252 sessions"),
        ):
            for interval in ((a, b), (b, a)):
                with self.subTest(interval=interval), self.assertRaisesRegex(ValueError, message):
                    calendar_replay(None, {"asof": "2026-11-25"}, None, {}, "ABC", *interval)

    def test_selects_exact_calendar_month_and_preserves_prior_history(self):
        from unittest.mock import patch
        from backtests import calendar_replay
        from paper_trading.market import exchange

        days = [
            s.date().isoformat()
            for s in exchange().sessions_in_range("2026-09-25", "2026-11-03")
        ]
        hist = {day: {"ABC": {"o": 10, "c": 10}} for day in days}
        october = [day for day in days if day.startswith("2026-10")]
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "history.json"
            cache.write_text(json.dumps({"history": hist, "tickers": ["ABC"]}))

            def check(manager, meta, snapshot, symbol, count):
                data = json.loads(snapshot.read_text())["history"]
                self.assertEqual(max(data), "2026-10-30")
                self.assertIn("2026-09-25", data)
                self.assertEqual(count, 22)
                return {october[1]}, set(october), "v1", {}

            with patch("backtests.replay", check):
                bars, _, _ = calendar_replay(
                    SimpleNamespace(finviz=None),
                    {"asof": "2026-11-03"},
                    cache,
                    hist,
                    "ABC",
                    "2026-10-01",
                    "2026-10-31",
                )
            self.assertEqual([b["day"] for b in bars], october)
            self.assertTrue(bars[1]["signal"])
