"""Price-bar size for replays, the header capital line, skipped-symbol notes and button feedback."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import backtests
import vectorbt_workspace as vb
from strategy_runner import _day_candles, replay_daily


class CandleBuilder(unittest.TestCase):
    def history(self, count):
        return {f"2026-10-{d:02d}": {"ABC": {"o": d, "h": d + 2, "l": d - 1, "c": d + 1, "v": 10}, "SPY": {"o": 1, "c": 1}}
                for d in range(1, count + 1)}

    def test_matches_the_run_time_candle_builder(self):
        import finviz_prices

        for count in (6, 7, 8):
            history = self.history(count)
            for sym in ("SPY",):
                for bar in (history[d][sym] for d in history):
                    bar.update(h=1, l=1, v=0)
            for n in (2, 3, 5):
                with self.subTest(count=count, n=n):
                    expected = finviz_prices.day_candles({"tickers": [], "history": history}, n)["history"]
                    self.assertEqual(_day_candles(history, ["ABC", "SPY"], n), expected)

    def test_newest_candle_ends_on_the_last_day_and_missing_extremes_are_filled(self):
        candles = _day_candles(self.history(7), ["ABC"], 3)
        self.assertEqual(sorted(candles), ["2026-10-04", "2026-10-07"])  # day 1 does not fill a candle
        self.assertEqual(candles["2026-10-07"]["ABC"], {"o": 5.0, "h": 9.0, "l": 4.0, "c": 8.0, "v": 30.0})
        self.assertEqual(_day_candles(self.history(4), ["SPY"], 2)["2026-10-04"]["SPY"]["h"], 1.0)


class ReplayOnLongerBars(unittest.TestCase):
    STRATEGY = (
        "CANDLE_DAYS = 1\n"
        "def finviz_signals(tickers, day, cache):\n"
        "    days = sorted(cache['history'])\n"
        "    assert days[-1] == day, (days[-1], day)                     # no bar from the future\n"
        "    spans = [cache['history'][d]['ABC']['v'] for d in days]\n"
        "    return [dict(Ticker='ABC', Status='BUY' if len(days) % 2 else 'WAIT', Bars=len(days), Span=spans[-1])]\n"
    )

    def run_replay(self, candle_days):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s.py").write_text(self.STRATEGY)
            history = {f"2026-11-{d:02d}": {"ABC": {"o": 10, "h": 11, "l": 9, "c": 10, "v": 1}, "ZZZ": {"o": 1, "c": 1}}
                       for d in range(2, 14)}
            (root / "c.json").write_text(json.dumps({"history": history}))
            args = SimpleNamespace(strategy=str(root / "s.py"), cache=str(root / "c.json"), symbol="ABC", asof="2026-11-13",
                                   sessions=4, output=str(root / "out.json"), candle_label="x", candle_days=candle_days)
            replay_daily(args)
            return json.loads((root / "out.json").read_text())

    def test_each_day_sees_candles_ending_that_day(self):
        daily = self.run_replay(1)
        three = self.run_replay(3)
        self.assertEqual(daily["days"], three["days"])
        # Daily: 9,10,11,12 bars visible -> BUY on odd counts. Three-day: 3,3,3,4 candles -> BUY, BUY, BUY, WAIT.
        self.assertEqual(daily["buys"], ["2026-11-10", "2026-11-12"])
        self.assertEqual(three["buys"], ["2026-11-10", "2026-11-11", "2026-11-12"])

    def test_command_line_accepts_one_to_five(self):
        from strategy_runner import main

        with self.assertRaises(SystemExit):
            main(["replay", "s.py", "--symbol", "A", "--cache", "c", "--asof", "2026-11-13", "--output", "o",
                  "--sessions", "4", "--candle-days", "6"])


class PortalRules(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual([backtests.candle_days(v) for v in (None, "", "1", " 5 ")], [1, 1, 1, 5])
        for bad in ("0", "6", "2.5", "x"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "price bars of 1–5 trading days"):
                backtests.candle_days(bad)

    def test_longer_bars_need_a_date_range_and_a_strategy_that_supports_them(self):
        st = SimpleNamespace(results_dir=Path(tempfile.mkdtemp()))
        fields = dict(rid="2026-10-30_160000_daily", symbols="ALL", holds="5", commission="0", candle_days="3")
        with self.assertRaisesRegex(ValueError, "Choose a date range to use 2–5 day price bars"):
            vb.submit(st, fields, lambda _: ["ABC"])
        manager = SimpleNamespace(meta=lambda: {"s": {"kind": "generic", "file": "s.py"}})
        with self.assertRaisesRegex(ValueError, "only works on daily price bars"):
            backtests.replay(manager, {"strategy": "s", "asof": "2026-10-30"}, None, "ABC", 20, candle_days=3)

    def test_replay_passes_the_bar_size_to_the_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s.py").write_text("")
            manager = SimpleNamespace(scripts_dir=root, timeout=900, mem_mb=512,
                                      meta=lambda: {"s": {"kind": "generic", "file": "s.py", "candles": True}})
            for days, expected in ((3, ["--candle-label", "3-day", "--candle-days", "3"]), (1, ["--candle-label", "Daily"])):
                with (patch("strategies.run_sandboxed", return_value=(1, "")) as sandbox, patch("strategies._chown_nobody"),
                      self.assertRaises(ValueError)):
                    backtests.replay(manager, {"strategy": "s", "asof": "2026-10-30"}, root / "c.json", "ABC", 20,
                                     **({"candle_days": days} if days > 1 else {}))
                args = sandbox.call_args.args[0]
                self.assertEqual(args[args.index("--candle-label"):], expected)


class PageElements(unittest.TestCase):
    def test_price_bars_replace_the_capital_box(self):
        form = vb.controls([], "", {}, [], True, {})
        self.assertNotIn("Initial Capital", form)
        self.assertIn("<label>Price bars<select name='candle_days'", form)
        self.assertIn("<option value='1' selected>Daily</option><option value='2'>2-day</option>", form)
        self.assertIn("<option value='5'>5-day</option>", form)
        self.assertIn("<option value='3' selected>3-day</option>", vb.controls([], "", {}, [], True, {"candle_days": "3"}))
        self.assertIn("<option value='4' selected>4-day</option>", vb.controls([], "", {"candle_days": 4}, [], True, {}))

    def test_report_shows_bar_size_and_skipped_symbols(self):
        days = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6)]
        fill = dict(entry=days[1], exit=days[2], buy=10, sell=11, pnl=10000.0, pct=10.0, hold=1)
        info = dict(run="r", strategy="daily", symbol="2 symbols", symbols=["ABC", "XYZ"], holds=[1], fee_bps=0,
                    source="Historical date-range replay", candle_days=3,
                    skipped_symbols={"BBCI": "2026-07-01", "AAA": "2026-07-02"})
        job = dict(inputs=info, error=None, result=dict(engine="t", days=days, parameters=[], full_period=dict(
            equity=[100000, 100000, 110000, 110000], hold=1, trades=1, fills=[fill])))
        with patch("dow_benchmark.equity", return_value=[]):
            page = vb.report(job, "abc00000-0000-0000-0000-000000000001", vb.report_data(job, ""), "All")
        self.assertIn("<dt>Price bars</dt><dd>3-day for signals, daily for trades</dd>", page)
        self.assertIn("Skipped 2 symbols without complete daily prices in this range: AAA, BBCI.", page)
        many = vb.skipped_note({f"S{i:02d}": "2026-07-01" for i in range(15)})
        self.assertTrue(many.endswith("S11 and 3 more."), many)
        self.assertEqual(vb.skipped_note({"ONE": "2026-07-01"}),
                         "Skipped 1 symbol without complete daily prices in this range: ONE.")

    def test_request_model_keeps_the_new_fields(self):
        from pydantic import ValidationError
        from vbt_service.api import Research

        bars = [dict(day=f"2026-10-0{d}", open=10, close=10, signal=False) for d in range(1, 5)]
        base = dict(run="r", strategy="s", version="v", symbol="ABC", holds=[1], fee_bps=0, observed_days=4, bars=bars)
        kept = Research(**base, candle_days=3, skipped_symbols={"BBCI": "2026-07-01"}).model_dump()
        self.assertEqual((kept["candle_days"], kept["skipped_symbols"]), (3, {"BBCI": "2026-07-01"}))
        self.assertEqual(Research(**base).model_dump()["candle_days"], 1)
        with self.assertRaises(ValidationError):
            Research(**base, candle_days=6)


if __name__ == "__main__":
    unittest.main()
