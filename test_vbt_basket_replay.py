"""Regression coverage for historical baskets across the portal and worker."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class RangeSubmission(unittest.TestCase):
    def test_selected_and_all_replay_each_day_without_future_prices(self):
        from paper_trading.market import exchange
        from strategy_runner import main
        from vectorbt_workspace import submit

        days = [
            s.date().isoformat()
            for s in exchange().sessions_in_range("2026-07-01", "2026-10-30")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rid = "2026-11-25_115440_nqqe_scanner"
            folder = root / rid
            folder.mkdir()
            (folder / "meta.json").write_text(
                json.dumps(
                    dict(strategy="test", status="ok", mode="close", asof="2026-11-24")
                )
            )
            symbols = ["ANF", "TGLS", "CCCC", "CELC"]
            history = {
                d: {s: {"o": 10, "c": 10, "h": 10.5} for s in symbols}
                for d in days + ["2026-11-24"]
            }
            (folder / "history_cache.json").write_text(
                json.dumps(dict(history=history, tickers=symbols))
            )
            (root / "strategy.py").write_text(
                "def finviz_signals(tickers, day, cache):\n"
                "    assert max(cache['history']) == day\n"
                "    n = len(cache['history'])\n"
                "    return [dict(Ticker=s, Status='ERROR', Error=f'Insufficient history ({n} candles); need at least 3')\n"
                "            if s == 'CCCC' and n < 3 else dict(Ticker=s, Status='BUY' if s=='ANF' or day.endswith('10') else 'WAIT') for s in tickers]\n"
            )
            manager = SimpleNamespace(
                results_dir=root,
                finviz=None,
                scripts_dir=root,
                mem_mb=512,
                timeout=90,
                meta=lambda: {"test": {"kind": "generic", "file": "strategy.py"}},
            )

            def run_fixture(args, *_):
                return main(args), ""

            with (
                patch("strategies.run_sandboxed", side_effect=run_fixture),
                patch("strategies._chown_nobody"),
                patch("backtests.service", return_value={"id": "job"}) as service,
            ):
                for selection, expected, start, end in [
                    ("ANF,TGLS", symbols[:2], days[-1], days[0]),
                    ("ALL", symbols, days[0], days[-1]),
                ]:
                    self.assertEqual(
                        submit(
                            manager,
                            dict(
                                rid=rid, symbols=selection, start=start, end=end, profit_target_pct="2"
                            ),
                            lambda _: symbols,
                        ),
                        "job",
                    )
                    data = service.call_args.args[1]
                    self.assertEqual(data["profit_target_pct"], 2)
                    self.assertEqual(data["warmup_sessions"], {"CCCC": 2} if "CCCC" in expected else {})
                    self.assertEqual(list(data["series"]), expected)
                    self.assertEqual(data["observed_days"], len(days))
                    for s, bars in data["series"].items():
                        self.assertTrue(all(b["high"] == 10.5 for b in bars))
                        self.assertEqual([b["day"] for b in bars], days)
                        self.assertEqual(
                            [b["day"] for b in bars if b["signal"]],
                            [d for d in days if s == "ANF" or d.endswith("10")],
                        )
                del history[days[2]]["CCCC"]
                (folder / "history_cache.json").write_text(
                    json.dumps(dict(history=history))
                )
                with self.assertRaisesRegex(
                    ValueError, "CCCC lacks a daily open or close on " + days[2]
                ) as problem:
                    submit(
                        manager,
                        dict(rid=rid, symbols="ALL", start=days[0], end=days[-1]),
                        lambda _: symbols,
                    )
                self.assertEqual(problem.exception.covered_symbols, ["ANF", "TGLS", "CELC"])
                self.assertEqual(problem.exception.common_range, (days[3], days[-1]))


class PortfolioCalculation(unittest.TestCase):
    def test_target_orders_fill_on_touch_gap_or_seventh_close(self):
        try:
            from vbt_service.engine import calculate
        except ModuleNotFoundError as exc:
            if exc.name == "vectorbtpro":
                self.skipTest("Licensed VectorBT PRO runtime required")
            raise
        from vectorbt_workspace import report_data

        days = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6, 7, 8, 9, 12, 13, 14)]

        def bars():
            return [
                dict(day=day, open=100, high=101, close=100, signal=i == 0)
                for i, day in enumerate(days)
            ]

        series = {s: bars() for s in ("TOUCH", "GAP", "TIME", "OPEN")}
        series["TOUCH"][1].update(high=102, close=91, signal=True)
        series["TOUCH"][2].update(high=105)
        series["TOUCH"][0]["high"] = 140
        series["GAP"][2].update(open=105, high=106)
        series["TIME"][7].update(close=98, signal=False)
        series["TIME"][8]["high"] = 120
        series["OPEN"][0]["signal"] = False
        series["OPEN"][-2]["signal"] = True
        series["OPEN"][-1].update(close=99)
        data = dict(
            series=series,
            symbol="4 symbols",
            strategy="scanner",
            run="sample",
            holds=[7],
            fee_bps=10,
            profit_target_pct=2,
            exit_on_target=True,
        )
        result = calculate(data)
        full = result["full_period"]
        self.assertEqual((full["trades"], full["open_trades"]), (4, 1))
        self.assertEqual(
            [
                (f["symbol"], f["sell"], f["hold"], f["target_hit"])
                for f in full["fills"]
            ],
            [
                ("TOUCH", 102, 1, True),
                ("GAP", 105, 2, True),
                ("TIME", 98, 7, False),
                ("TOUCH", 102, 1, True),
            ],
        )
        rate = 0.999 / 1.001
        expected = 25000 * (
            (1.02 * rate) ** 2 + 1.05 * rate + 0.98 * rate + 0.99 / 1.001
        )
        self.assertAlmostEqual(full["equity"][-1], expected, places=2)
        m = report_data(dict(inputs=data, result=result, error=None), "")
        self.assertEqual((m["wins"], m["losses"], m["avg_hold"]), (3, 1, 2.75))
        self.assertEqual(result["parameters"][0]["trades"], 3)
        cohort = dict(
            data,
            dates=[days[1], days[7]],
            target_days=days[1:8],
            elapsed=[True, True],
            completed_sessions=7,
            signal_day=days[0],
            prices={s: [100, 100] for s in series},
            target_bars={s: b[1:8] for s, b in series.items()},
        )
        snapshot = calculate(cohort)
        self.assertEqual(
            snapshot["target_fills"]["GAP"], dict(exit=days[2], sell=105, hold=2)
        )
        self.assertEqual(snapshot["target_hits"]["TIME"], [False])
        self.assertAlmostEqual(
            snapshot["equity"][-1], 25000 * rate * (1.02 + 1.05 + 0.98 + 1), places=2
        )
        cm = report_data(dict(inputs=cohort, result=snapshot, error=None), "")
        self.assertEqual(cm["rows"][0][4], days[2])
        cohort.update(
            completed_sessions=0,
            target_bars={s: [dict(open=100, high=None, close=None)] for s in series},
        )
        live = calculate(cohort)
        self.assertEqual(live["equity"], [])
        self.assertTrue(all(v == ["Pending exit"] for v in live["outcomes"].values()))
        series["TIME"][3]["high"] = None
        with self.assertRaisesRegex(ValueError, "TIME.*daily high unavailable"):
            calculate(data)

    def test_profit_target_touch_within_open_to_open_hold(self):
        try:
            from vbt_service.engine import calculate
        except ModuleNotFoundError as exc:
            if exc.name == "vectorbtpro":
                self.skipTest("Licensed VectorBT PRO runtime required")
            raise
        from vectorbt_workspace import report_data, trade_text

        days = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6, 7, 8, 9, 12, 13, 14)]

        def bars(entry_high, exit_open):
            bars = [{"day": d, "open": 300, "close": 300, "high": 300, "signal": i == 0} for i, d in enumerate(days)]
            bars[0]["high"] = 350  # The signal-day high occurs before entry.
            bars[1]["high"] = entry_high
            bars[3].update(open=exit_open, high=350)  # Only the open is owned on exit day.
            return bars

        series = {"HIT": bars(306, 295), "MISS": bars(305.99, 303), "GAP": bars(None, 306), "UNKNOWN": bars(None, 299)}
        info = {"run": "example", "symbol": "4 symbols", "strategy": "daily", "series": series, "holds": [2], "fee_bps": 0}
        target = dict(info, profit_target_pct=2)
        result = calculate(target)
        baseline = calculate(info)
        self.assertEqual(result["full_period"]["equity"], baseline["full_period"]["equity"])
        metrics = report_data({"inputs": target, "result": result, "error": None}, "")
        self.assertEqual((metrics["wins"], metrics["losses"], metrics["unscored"], metrics["scored"]), (2, 1, 1, 3))
        self.assertAlmostEqual(metrics["rate"], 200 / 3)
        for group, symbols in [("wins", {"HIT", "GAP"}), ("losses", {"MISS"}), ("all", set(series))]:
            listing = trade_text(target, metrics, group)
            self.assertEqual({s for s in series if s+"\tLong" in listing}, symbols)
        self.assertEqual(report_data({"inputs": info, "result": baseline, "error": None}, "")["wins"], 2)

    def test_saved_buy_targets_follow_each_holding_period(self):
        try:
            from vbt_service.engine import calculate
        except ModuleNotFoundError as exc:
            if exc.name == "vectorbtpro":
                self.skipTest("Licensed VectorBT PRO runtime required")
            raise
        data = {
            "holds": [2, 1], "elapsed": [True, True, True], "fee_bps": 0,
            "dates": ["2026-10-01", "2026-10-05", "2026-10-02"], "profit_target_pct": 2,
            "prices": {"ABC": [300, 295, 301], "NOHIGH": [300, 299, 299], "NODATA": [None, None, None]},
            "session_highs": {"ABC": [302, 306], "NOHIGH": [305, None]},
        }
        result = calculate(data)
        self.assertEqual(result["target_hits"], {"ABC":[True, False], "NOHIGH":[None, False], "NODATA":[None,None]})
        self.assertLess(result["outcomes"]["ABC"][0], 0)
        self.assertGreater(result["outcomes"]["ABC"][1], 0)

    def test_capital_fees_and_dates_for_overlapping_signals(self):
        try:
            from vbt_service.engine import calculate
        except ModuleNotFoundError as exc:
            if exc.name == "vectorbtpro":
                self.skipTest("Licensed VectorBT PRO runtime required")
            raise

        days = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6, 7, 8, 9, 12, 13, 14)]

        def bars(prices, buy):
            return [
                dict(day=d, open=p, close=p, signal=i in buy)
                for i, (d, p) in enumerate(zip(days, prices))
            ]

        abc = bars([10, 10, 12, 12, 12, 12, 12, 12, 12, 12], {0})
        xyz = bars([20, 20, 18, 18, 18, 18, 18, 18, 18, 18], {0})
        cash = bars([10] * 10, set())

        def run(series, fee=0):
            return calculate(dict(series=series, holds=[1], fee_bps=fee))["full_period"]

        result = run({"ABC": abc, "XYZ": xyz})
        self.assertEqual([t["symbol"] for t in result["fills"]], ["ABC", "XYZ"])
        self.assertEqual(result["trades"], 2)
        self.assertAlmostEqual(result["equity"][-1], 105000)
        self.assertAlmostEqual(result["benchmark"][-1], 105000)
        self.assertTrue(
            all(t["entry"] == days[1] and t["exit"] == days[2] for t in result["fills"])
        )
        self.assertAlmostEqual(run({"ABC": abc, "CASH": cash})["equity"][-1], 110000)
        charged = run({"ABC": abc, "XYZ": xyz}, 10)
        self.assertAlmostEqual(charged["equity"][-1], 105000 * 0.999 / 1.001, places=2)
        flat = run({"A": cash, "B": cash})
        self.assertEqual((flat["trades"], flat["equity"][-1]), (0, 100000))
        with self.assertRaisesRegex(ValueError, "same sessions"):
            run({"ABC": abc, "XYZ": xyz[1:]})
