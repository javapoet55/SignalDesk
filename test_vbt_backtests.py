import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backtests import dataset
from strategy_runner import replay_daily


class HistoricalSignals(unittest.TestCase):
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
