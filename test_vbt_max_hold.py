"""The maximum hold used with a profit target is configurable (default 7, range 1-60)."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import backtests
import vectorbt_workspace as vb
from backtest_dashboard import target_rule

DAYS = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20)]


def engine():
    try:
        from vbt_service import engine as module
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("vectorbtpro"):
            raise unittest.SkipTest("Licensed VectorBT PRO runtime required") from exc
        raise
    return module


def flat():
    return [dict(day=day, open=100, high=101, low=99, close=100 - i * 0.1, signal=i == 0) for i, day in enumerate(DAYS)]


class PortalInput(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(backtests.target_hold(None), 7)
        self.assertEqual(backtests.target_hold("  "), 7)
        self.assertEqual(backtests.target_hold(" 3 "), 3)
        self.assertEqual(backtests.target_hold("60"), 60)
        for bad in ("0", "61", "1,5,10", "2.5", "abc", "-3"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "one maximum hold of 1–60 trading days"):
                backtests.target_hold(bad)

    def test_submit_sends_the_chosen_maximum(self):
        import tempfile
        from pathlib import Path

        st = SimpleNamespace(results_dir=Path(tempfile.mkdtemp()))
        sent = {}

        def cohort(manager, rid, holds, fee, selected, target=None):
            sent.update(holds=holds, target=target)
            return {}

        fields = dict(rid="2026-10-30_160000_daily", symbols="ALL", commission="0", profit_target_pct="2")
        with patch("backtests.cohort", side_effect=cohort), patch("backtests.submit_job", return_value="job"):
            vb.submit(st, dict(fields, holds="3"), lambda _: ["ABC"])
            self.assertEqual(sent, dict(holds=[3], target=2))
            vb.submit(st, fields, lambda _: ["ABC"])                      # no hold given: default
            self.assertEqual(sent["holds"], [7])
            with self.assertRaisesRegex(ValueError, "one maximum hold"):
                vb.submit(st, dict(fields, holds="1,5,10"), lambda _: ["ABC"])
            vb.submit(st, dict(fields, profit_target_pct="", holds="1,5,10"), lambda _: ["ABC"])
            self.assertEqual(sent, dict(holds=[1, 5, 10], target=None))    # lists still work without a target

    def test_form_field_is_editable_and_keeps_the_value(self):
        form = vb.controls([], "", {}, [], True, dict(profit_target_pct="2", holds="3"))
        self.assertIn("Maximum hold (trading days)</span><input name='holds' list='vb-holds' value='3' required", form)
        self.assertNotIn("readonly required", form)
        saved = vb.controls([], "", dict(profit_target_pct=2.0, _hold=12, holds=[12]), [], True, {})
        self.assertIn("name='holds' list='vb-holds' value='12' required", saved)

    def test_rule_text_names_the_last_day(self):
        info = dict(profit_target_pct=2, exit_on_target=True, entry_basis="signal_close")
        self.assertIn("sell at the close of trading day 3.", target_rule(dict(info, holds=[3])))
        self.assertIn("sell at the close of trading day 7.", target_rule(dict(info, holds=[7])))
        self.assertIn("sell at the close of trading day 7.", target_rule(info))  # older jobs without holds

    def test_request_model_allows_longer_windows(self):
        from pydantic import ValidationError
        from vbt_service.api import Cohort

        def cohort(n):
            return Cohort(run="r", strategy="s", version="v", symbol="1 buys", holds=[n], fee_bps=0, source="Run buy signals",
                          signal_day=DAYS[0], dates=[DAYS[0], DAYS[1]], elapsed=[True, True], prices={"ABC": [100, 100]},
                          exit_on_target=True, profit_target_pct=2, completed_sessions=n,
                          target_days=[DAYS[0]] * n, target_bars={"ABC": [dict(open=1, high=1, low=1, close=1)] * n})

        self.assertEqual(cohort(60).completed_sessions, 60)
        with self.assertRaises(ValidationError):
            cohort(61)


class EngineMaximum(unittest.TestCase):
    def test_exit_rule_uses_the_given_last_day(self):
        e = engine()
        bar = dict(open=100, high=101, low=99, close=98)
        self.assertIsNone(e.target_exit(bar, 102.0, 2, max_age=3))
        self.assertEqual(e.target_exit(bar, 102.0, 3, max_age=3), (98, False, "time"))
        self.assertIsNone(e.target_exit(bar, 102.0, 3))                       # default is still seven
        self.assertEqual(e.target_exit(bar, 102.0, 7), (98, False, "time"))
        self.assertEqual(e.target_exit(dict(bar, high=103), 102.0, 3, max_age=3), (102.0, True, "target"))

    def request(self, hold, **extra):
        return dict(series={"ABC": flat(), "XYZ": flat()}, holds=[hold], fee_bps=0, entry_basis="signal_close",
                    exit_on_target=True, profit_target_pct=2, run="r", symbol="2 symbols", strategy="daily", **extra)

    def test_replay_sells_on_the_chosen_day(self):
        calculate = engine().calculate
        for hold in (1, 3, 7, 10):
            with self.subTest(hold=hold):
                fills = calculate(self.request(hold))["full_period"]["fills"]
                self.assertEqual({(f["exit"], f["hold"], f["exit_reason"]) for f in fills}, {(DAYS[hold], hold, "time")})
                self.assertAlmostEqual(fills[0]["sell"], 100 - hold * 0.1)

    def test_saved_buy_list_sells_on_the_chosen_day(self):
        calculate = engine().calculate
        bars = flat()
        for hold in (2, 10):
            cohort = dict(holds=[hold], fee_bps=0, entry_basis="signal_close", exit_on_target=True, profit_target_pct=2,
                          elapsed=[True, True], dates=[DAYS[0], DAYS[hold]], target_days=DAYS[1:hold + 1],
                          completed_sessions=hold, prices={"ABC": [100, 100]},
                          target_bars={"ABC": [{k: b[k] for k in ("open", "high", "low", "close")} for b in bars[1:hold + 1]]})
            fill = calculate(cohort)["target_fills"]["ABC"]
            self.assertEqual((fill["exit"], fill["hold"], fill["reason"]), (DAYS[hold], hold, "time"))

    def test_engine_validates_the_maximum(self):
        calculate = engine().calculate
        for holds in ([0], [61], [3, 5], []):
            with self.subTest(holds=holds), self.assertRaisesRegex(ValueError, "one maximum hold of 1–60 sessions"):
                request = self.request(7)
                request["holds"] = holds
                calculate(request)


if __name__ == "__main__":
    unittest.main()
