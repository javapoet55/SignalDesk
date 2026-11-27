"""Stop-loss exits beside the profit target: pricing rules, engine results, form and report."""

import unittest
from types import SimpleNamespace

import backtests
import vectorbt_workspace as vb

DAYS = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6, 7, 8, 9, 12, 13, 14)]


def engine():
    try:
        from vbt_service import engine as module
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("vectorbtpro"):
            raise unittest.SkipTest("Licensed VectorBT PRO runtime required") from exc
        raise
    return module


def flat(**first_signal):
    return [dict(day=day, open=100, high=101, low=99, close=100, signal=i == 0) for i, day in enumerate(DAYS)]


class ExitPricing(unittest.TestCase):
    """Entry 100, target +2% = 102, stop -10% = 90."""

    def check(self, bar, expected, age=1, complete=True, stop=90.0):
        self.assertEqual(engine().target_exit(bar, 102.0, age, complete, stop), expected)

    def test_each_rule(self):
        e = engine()
        self.assertEqual((e.limit_price(100, 2), e.stop_price(100, 10)), (102.0, 90.0))
        bar = dict(open=100, high=101, low=95, close=100)
        self.check(bar, None)                                                      # neither level reached
        self.check(dict(bar, low=90), (90.0, False, "stop"))                       # touched the stop exactly
        self.check(dict(bar, low=85), (90.0, False, "stop"))                       # traded through: fills at the stop
        self.check(dict(bar, open=88, low=80), (88, False, "stop"))                # gapped below: fills at the open
        self.check(dict(bar, high=103), (102.0, True, "target"))                   # target only
        self.check(dict(bar, open=104, high=105), (104, True, "target"))           # gapped above the target
        self.check(dict(bar, high=103, low=89), (90.0, False, "stop"))             # both touched: stop counts first
        self.check(dict(bar, close=98), (98, False, "time"), age=7)                # seventh close
        self.check(dict(bar, low=85), (90.0, False, "stop"), age=7)                # stop still wins on day seven
        self.check(dict(open=88, high=None, low=None, close=None), (88, False, "stop"), complete=False)
        self.check(dict(open=95, high=None, low=None, close=None), None, complete=False)  # session still open

    def test_missing_low_cannot_be_priced_unless_the_close_proves_a_stop(self):
        with self.assertRaisesRegex(ValueError, "daily low unavailable"):
            self.check(dict(open=100, high=103, low=None, close=101), None)
        self.check(dict(open=100, high=101, low=None, close=89), (90.0, False, "stop"))

    def test_without_a_stop_the_low_is_ignored(self):
        bar = dict(open=100, high=101, low=50, close=100)
        self.assertIsNone(engine().target_exit(bar, 102.0, 1))
        self.assertEqual(engine().target_exit(dict(bar, high=102), 102.0, 1), (102.0, True, "target"))


class EngineResults(unittest.TestCase):
    def series(self):
        series = {name: flat() for name in ("STOP", "GAPDOWN", "BOTH", "HIT", "TIME")}
        series["STOP"][2].update(low=89, close=95)               # day two trades through 90
        series["GAPDOWN"][1].update(open=85, high=86, low=80, close=84)
        series["BOTH"][1].update(high=103, low=88)               # same session touches both levels
        series["HIT"][3].update(high=102.5)
        series["TIME"][7].update(close=97)
        return series

    def request(self, **extra):
        return dict(series=self.series(), holds=[7], fee_bps=0, entry_basis="signal_close", exit_on_target=True,
                    profit_target_pct=2, run="r", symbol="5 symbols", strategy="daily", **extra)

    def test_replay_fills_and_report(self):
        calculate = engine().calculate
        result = calculate(self.request(stop_loss_pct=10))
        fills = {f["symbol"]: f for f in result["full_period"]["fills"]}
        self.assertEqual({s: (f["sell"], f["exit"], f["exit_reason"], f["target_hit"]) for s, f in fills.items()}, {
            "STOP": (90.0, DAYS[2], "stop", False), "GAPDOWN": (85.0, DAYS[1], "stop", False),
            "BOTH": (90.0, DAYS[1], "stop", False), "HIT": (102.0, DAYS[3], "target", True),
            "TIME": (97.0, DAYS[7], "time", False)})
        self.assertAlmostEqual(fills["STOP"]["pct"], -10.0)
        self.assertAlmostEqual(fills["GAPDOWN"]["pct"], -15.0)
        # 20,000 per symbol: -2,000 -3,000 -2,000 +400 -600.
        self.assertAlmostEqual(result["full_period"]["equity"][-1], 100000 - 7200, places=2)

        info = self.request(stop_loss_pct=10)
        info.pop("series")
        job = dict(inputs=dict(info, symbols=list(fills)), result=result, error=None)
        metrics = vb.report_data(job, "")
        self.assertEqual((metrics["stopped"], metrics["wins"], metrics["losses"], metrics["stop_loss_pct"]), (3, 1, 4, 10))
        outcome = {row[0]: row[-1] for row in metrics["rows"]}
        self.assertEqual(outcome, {"STOP": "Stopped", "GAPDOWN": "Stopped", "BOTH": "Stopped", "HIT": "Hit", "TIME": "Missed"})
        from unittest.mock import patch
        with patch("dow_benchmark.equity", return_value=[]):
            page = vb.report(job, "abc00000-0000-0000-0000-000000000001", metrics, "All")
        self.assertIn("<dt>Stop loss</dt><dd>10%</dd>", page)
        self.assertIn("Sell at a 10% loss from entry", page)
        self.assertIn("3 of 5 closed trades were stopped out.", page)
        self.assertIn(b"Stopped", vb.export("csv", job["inputs"], metrics))

    def test_no_stop_gives_the_same_trades_as_before(self):
        calculate = engine().calculate
        plain = calculate(self.request())["full_period"]
        by_symbol = {f["symbol"]: f for f in plain["fills"]}
        self.assertEqual(by_symbol["BOTH"]["exit_reason"], "target")   # the low is ignored without a stop
        self.assertEqual(by_symbol["STOP"]["exit_reason"], "time")
        self.assertFalse(any(f["exit_reason"] == "stop" for f in plain["fills"]))

    def test_saved_buy_list_uses_the_same_rules(self):
        calculate = engine().calculate
        series = self.series()
        cohort = dict(holds=[7], fee_bps=0, entry_basis="signal_close", exit_on_target=True, profit_target_pct=2,
                      stop_loss_pct=10, elapsed=[True, True], dates=[DAYS[0], DAYS[7]], target_days=DAYS[1:8],
                      completed_sessions=7, prices={s: [100, 100] for s in series},
                      target_bars={s: [{k: b[k] for k in ("open", "high", "low", "close")} for b in bars[1:8]]
                                   for s, bars in series.items()})
        result = calculate(cohort)
        self.assertEqual({s: (f["sell"], f["exit"], f["reason"]) for s, f in result["target_fills"].items()}, {
            "STOP": (90.0, DAYS[2], "stop"), "GAPDOWN": (85, DAYS[1], "stop"), "BOTH": (90.0, DAYS[1], "stop"),
            "HIT": (102.0, DAYS[3], "target"), "TIME": (97, DAYS[7], "time")})
        self.assertAlmostEqual(result["outcomes"]["STOP"][0], -10.0)
        self.assertAlmostEqual(result["equity"][-1], 100000 - 7200, places=2)

    def test_engine_rejects_a_stop_without_target_exits(self):
        calculate = engine().calculate
        request = self.request(stop_loss_pct=10)
        request.update(exit_on_target=False, profit_target_pct=None, holds=[1])
        with self.assertRaisesRegex(ValueError, "stop loss needs a profit target"):
            calculate(request)
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            calculate(self.request(stop_loss_pct=100))


class PortalInput(unittest.TestCase):
    def test_parsing(self):
        self.assertIsNone(backtests.stop_loss(""))
        self.assertIsNone(backtests.stop_loss(None))
        self.assertEqual(backtests.stop_loss(" 10 "), 10)
        for bad in ("0", "-5", "100", "abc", "nan"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "stop loss between 0 and 100"):
                backtests.stop_loss(bad)

    def test_daily_low_must_be_consistent_with_the_bar(self):
        self.assertEqual(backtests.daily_low(dict(o=10, c=11, l=9.5)), 9.5)
        self.assertIsNone(backtests.daily_low(dict(o=10, c=11, l=10.5)))  # above the open: not a real low
        self.assertIsNone(backtests.daily_low(dict(o=10, c=11)))
        self.assertIsNone(backtests.daily_low({}))
        self.assertIsNone(backtests.daily_low(dict(o=10, c=11, l=0)))

    def test_stop_needs_a_target(self):
        st = SimpleNamespace(results_dir=None)
        with self.assertRaisesRegex(ValueError, "Enter a profit target to use a stop loss"):
            vb.submit(st, dict(rid="x", symbols="ABC", holds="5", commission="0", stop_loss_pct="10"), lambda _: [])
        with self.assertRaisesRegex(ValueError, "Enter a profit target to use a stop loss"):
            backtests.submit_job(dict(stop_loss_pct=10.0, profit_target_pct=None), 0.0)

    def test_form_field_and_recovery_keep_the_stop(self):
        form = vb.controls([], "", {}, [], True, dict(profit_target_pct="2", stop_loss_pct="10"))
        self.assertIn("name='stop_loss_pct' inputmode='decimal' pattern='" + vb.PERCENT_LIST + "' value='10'", form)
        saved = vb.controls([], "", dict(profit_target_pct=2.0, stop_loss_pct=10.0), [], True, {})
        self.assertIn("name='stop_loss_pct' inputmode='decimal' pattern='" + vb.PERCENT_LIST + "' value='10.0'", saved)
        error = SimpleNamespace(common_range=("2026-10-02", "2026-10-09"), covered_symbols=[], symbol_count=2,
                                requested_range=("2026-10-01", "2026-10-09"))
        self.assertIn("name='stop_loss_pct' value='10'", vb.coverage_options(error, dict(rid="r", stop_loss_pct="10")))

    def test_request_model_accepts_and_bounds_the_new_fields(self):
        from pydantic import ValidationError
        from vbt_service.api import Bar, Research, SessionPrices

        bars = [dict(day=d, open=10, high=11, low=9, close=10, signal=False) for d in DAYS[:4]]
        base = dict(run="r", strategy="s", version="v", symbol="ABC", holds=[7], fee_bps=0, observed_days=4, bars=bars,
                    exit_on_target=True, profit_target_pct=2)
        self.assertEqual(Research(**base, stop_loss_pct=10).model_dump()["stop_loss_pct"], 10)
        self.assertEqual(Research(**base).model_dump()["bars"][0]["low"], 9)
        self.assertIsNone(Bar(day=DAYS[0], open=10, close=10, signal=False).low)
        self.assertIsNone(SessionPrices(open=1, high=None, close=None).low)
        for bad in (0, 100, -1):
            with self.assertRaises(ValidationError):
                Research(**base, stop_loss_pct=bad)


if __name__ == "__main__":
    unittest.main()
