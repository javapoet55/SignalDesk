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
                d: {s: {"o": 10, "c": 10} for s in symbols}
                for d in days + ["2026-11-24"]
            }
            (folder / "history_cache.json").write_text(
                json.dumps(dict(history=history, tickers=symbols))
            )
            (root / "strategy.py").write_text(
                "def finviz_signals(tickers, day, cache):\n"
                "    assert max(cache['history']) == day\n"
                "    return [dict(Ticker=s, Status='BUY' if s=='ANF' or day.endswith('10') else 'WAIT') for s in tickers]\n"
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
                for selection, expected in [
                    ("ANF,TGLS", symbols[:2]),
                    ("ALL", symbols),
                ]:
                    self.assertEqual(
                        submit(
                            manager,
                            dict(
                                rid=rid, symbols=selection, start=days[0], end=days[-1]
                            ),
                            lambda _: symbols,
                        ),
                        "job",
                    )
                    data = service.call_args.args[1]
                    self.assertEqual(list(data["series"]), expected)
                    self.assertEqual(data["observed_days"], len(days))
                    for s, bars in data["series"].items():
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
                ):
                    submit(
                        manager,
                        dict(rid=rid, symbols="ALL", start=days[0], end=days[-1]),
                        lambda _: symbols,
                    )


class PortfolioCalculation(unittest.TestCase):
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
