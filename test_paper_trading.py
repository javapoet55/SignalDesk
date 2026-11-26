import concurrent.futures
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from paper_trading.ledger import Ledger, Problem


class Feed:
    def __init__(self, stamp):
        self.stamp = stamp
        self.price = "10"
        self.down = False

    def quote(self, ticker):
        if self.down:
            raise Problem("offline", 503)
        return {
            "stamp": self.stamp,
            "price": self.price,
            "source": "Fictional test feed",
        }


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = int(dt.datetime(2026, 11, 24, 16, tzinfo=dt.UTC).timestamp())
        self.feed = Feed(self.now)
        self.db = Ledger(Path(self.temp.name) / "paper.db", self.feed, lambda: self.now)
        self.pid = self.db.create("alice", {"name": "Research", "capital": "1000"})[
            "id"
        ]

    def preview(self, side="Buy", method="shares", amount="2", key="test-key-0001"):
        return self.db.preview(
            "alice",
            self.pid,
            {
                "side": side,
                "method": method,
                "amount": amount,
                "symbol": "TEST",
                "request_key": key,
            },
        )

    def confirm(self, r):
        return self.db.process("alice", self.pid, r["id"], "confirm")

    def test_all_sizing_and_roundtrip(self):
        for method, amount in [
            ("shares", "2"),
            ("dollars", "20"),
            ("equity_pct", "2"),
            ("cash_pct", "2"),
        ]:
            self.assertEqual(
                float(
                    json.loads(
                        self.preview(
                            method=method, amount=amount, key="request-" + method
                        )["preview"]
                    )["qty"]
                ),
                2,
            )
        self.confirm(self.preview())
        self.feed.price = "12"
        self.confirm(self.preview("Sell", "position_pct", "50", "request-sell"))
        v = self.db.valuation("alice", self.pid)
        self.assertEqual(
            (v["equity"], v["realized"], v["unrealized"]), ("1004.00", "2.00", "2.00")
        )
        self.assertEqual(v["portfolio"]["cash"], "992.00")

    def test_validation(self):
        for args in [
            ("Sell", "shares", "1"),
            ("Buy", "shares", "101"),
            ("Buy", "cash_pct", "101"),
            ("Buy", "shares", "NaN"),
            ("Buy", "shares", "-1"),
            ("Buy", "position_pct", "50"),
        ]:
            with self.subTest(args=args), self.assertRaises(Problem):
                self.preview(*args)
        self.db.settings("alice", self.pid, {"fractional": False})
        self.assertEqual(
            json.loads(self.preview(method="dollars", amount="19")["preview"])["qty"],
            "1",
        )

    def test_stale_missing_and_expired(self):
        r = self.preview()
        self.now += 121
        self.assertEqual(self.confirm(r)["status"], "Expired")
        with self.assertRaises(Problem):
            self.preview(key="second-key")
        self.feed.down = True
        with self.assertRaises(Problem):
            self.preview(key="third-key")

    def test_duplicate_and_racing_signals(self):
        a = self.preview(amount="80")
        self.assertEqual(a["id"], self.preview(amount="80")["id"])
        b = self.preview(amount="80", key="second-key")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            list(pool.map(self.confirm, [a, b, a, b]))
        self.assertEqual(len(self.db.records("alice", self.pid, "transactions")), 1)
        self.assertEqual(
            self.db.valuation("alice", self.pid)["portfolio"]["cash"], "200.00"
        )

    def test_cancel_archive_isolation_reset(self):
        sig = self.preview()
        for action in [
            lambda: self.db.records("bob", self.pid, "audit"),
            lambda: self.db.process("bob", self.pid, sig["id"], "confirm"),
            lambda: self.db.valuation("bob", self.pid),
        ]:
            with self.assertRaises(Problem):
                action()
        self.assertEqual(
            self.db.process("alice", self.pid, sig["id"], "cancel")["status"],
            "Canceled",
        )
        replacement = self.db.settings(
            "alice", self.pid, {"action": "reset", "confirm_name": "Research"}
        )
        self.assertNotEqual(self.pid, replacement["id"])
        self.assertEqual(
            self.db.valuation("alice", self.pid)["portfolio"]["status"], "archived"
        )
        self.assertEqual(len(self.db.records("alice", self.pid, "orders")), 1)

    def test_atomic_failure_and_immutable_cash(self):
        r = self.preview()
        with self.db.connection(True) as db:
            db.execute(
                "CREATE TRIGGER fail_test BEFORE INSERT ON paper_transactions BEGIN SELECT RAISE(ABORT,'forced'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.confirm(r)
        self.assertEqual(
            self.db.records("alice", self.pid, "signals")[0]["status"], "Pending"
        )
        self.assertEqual(self.db.records("alice", self.pid, "orders"), [])
        with self.assertRaises(sqlite3.IntegrityError), self.db.connection(True) as db:
            db.execute("DELETE FROM paper_cash_ledger")

    def test_research_reports_and_history(self):
        from paper_trading.analytics import analytics, snapshot
        from paper_trading.reporting import pdf_report

        self.assertEqual(
            analytics(self.db, "alice", self.pid)["metrics"]["Annualized volatility"],
            "N/A",
        )
        self.confirm(self.preview())
        self.now += 60
        self.feed.stamp = self.now
        snapshot(self.db, "alice", self.pid)
        self.feed.price = "8.50"
        self.now += 60
        self.feed.stamp = self.now
        snapshot(self.db, "alice", self.pid)
        st = analytics(self.db, "alice", self.pid)
        self.assertEqual(st["series"][-1]["equity"], "997.00")
        self.assertEqual(st["metrics"]["Maximum drawdown"], "-0.30%")
        self.assertTrue(pdf_report(self.db, "alice", self.pid).startswith(b"%PDF"))
        self.db.journal("alice", self.pid, {"hypothesis": "Check subsequent returns."})
        self.assertEqual(len(self.db.records("alice", self.pid, "journal")), 1)

    def test_market_holiday_and_partial_close(self):
        from paper_trading.market import fresh

        for day, hour in [(26, 16), (27, 18)]:
            now = int(dt.datetime(2026, 11, day, hour, 0, tzinfo=dt.UTC).timestamp())
            with self.assertRaises(Problem):
                fresh({"stamp": now}, now)

    def test_fees_rounding_actions_and_reconciliation(self):
        from paper_trading.actions import apply_action

        self.db.settings("alice", self.pid, {"fee": "0.25"})
        self.feed.price = "3"
        r = self.confirm(self.preview(method="dollars", amount="10"))
        self.assertEqual(json.loads(r["preview"])["qty"], "3.333333")
        v = self.db.valuation("alice", self.pid)
        self.assertEqual(v["pnl"], "-0.25")
        event = {
            "symbol": "TEST",
            "kind": "split",
            "value": "2",
            "confirm_symbol": "TEST",
            "request_key": "fictional-01",
            "reference": "fictional split",
        }
        apply_action(self.db, "alice", self.pid, event)
        apply_action(self.db, "alice", self.pid, event)
        self.feed.price = "1.5"
        self.assertEqual(
            self.db.valuation("alice", self.pid)["positions"][0]["qty"], "6.666666"
        )
        apply_action(
            self.db,
            "alice",
            self.pid,
            {
                **event,
                "kind": "dividend",
                "value": "0.30",
                "request_key": "fictional-02",
            },
        )
        self.confirm(self.preview("Sell", "position_pct", "100", "close-position"))
        v = self.db.valuation("alice", self.pid)
        self.assertEqual(
            (v["positions"], v["equity"], v["realized"]), ([], "1001.50", "1.50")
        )

    def test_draft_edit_price_change_and_failed_feed(self):
        args = {
            "side": "Buy",
            "symbol": "TEST",
            "amount": "1",
            "method": "shares",
            "request_key": "draft-key",
        }
        d = self.db.draft("alice", self.pid, args)
        self.assertEqual(d["status"], "Draft")
        with self.assertRaises(Problem):
            self.confirm(d)
        r = self.db.preview(
            "alice",
            self.pid,
            {**args, "request_key": "edited-key", "replaces": d["id"]},
        )
        self.assertEqual(
            self.db.process("alice", self.pid, d["id"], "cancel")["status"], "Canceled"
        )
        self.feed.down = True
        with self.assertRaises(Problem):
            self.confirm(r)
        self.feed.down = False
        self.feed.price = "11"
        self.assertEqual(self.confirm(r)["status"], "Expired")
        self.assertFalse(self.db.records("alice", self.pid, "transactions"))

    def test_budget_policies_and_migration(self):
        from unittest.mock import patch

        from paper_trading.reporting import (
            explanation,
            historical_benchmark,
            pdf_report,
        )

        with patch.dict(
            "os.environ",
            {
                "PAPER_ENTITLEMENTS": '{"alice":{"advanced":false,"export":false,"portfolios":1}}'
            },
        ):
            for fn in (historical_benchmark, explanation, pdf_report):
                with self.assertRaises(Problem):
                    fn(self.db, "alice", self.pid)
            with self.assertRaises(Problem):
                self.db.create("alice", {"name": "Too many", "capital": "10"})
        with sqlite3.connect(self.db.path) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript(
                (
                    Path(__file__).parent / "paper_trading/migrations/002_down.sql"
                ).read_text()
            )
            con.executescript(
                (
                    Path(__file__).parent / "paper_trading/migrations/001_down.sql"
                ).read_text()
            )
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 0)
        recreated = Ledger(self.db.path, self.feed)
        self.assertEqual(recreated.portfolios("alice"), [])

    def test_scoped_filters_and_conflicting_keys(self):
        self.confirm(self.preview())
        with self.assertRaises(Problem):
            self.preview(amount="3")
        self.assertEqual(
            self.db.records(
                "alice", self.pid, "transactions", filters={"symbol": "OTHER"}
            ),
            [],
        )
        self.assertEqual(
            len(
                self.db.records("alice", self.pid, "orders", filters={"query": "TEST"})
            ),
            1,
        )
        self.assertEqual(
            self.db.records(
                "alice", self.pid, "transactions", filters={"start": self.now + 1}
            ),
            [],
        )

    def test_model_and_licensed_adapter_contracts(self):
        import io
        import os
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch

        from finviz_agent import FetchError
        from paper_trading.analytics import snapshot
        from paper_trading.market import Market
        from paper_trading.reporting import explanation, historical_benchmark

        class FailingClient:
            def quote_history(self, ticker, period):
                raise FetchError("offline")

        with self.assertRaisesRegex(Problem, "No verified"):
            Market(SimpleNamespace(client=FailingClient())).history("TEST")
        flags = {
            "PAPER_ENTITLEMENTS": '{"alice":{"ai":true,"compute_credits":1}}',
            "PAPER_LOCAL_MODEL": "fictional-unit-model",
            "PAPER_VECTORBT_LICENSED": "1",
        }
        output = {
            "facts": "No trades.",
            "assumptions": "USD.",
            "uncertainties": "No history.",
        }
        with (
            patch.dict(os.environ, flags),
            patch(
                "urllib.request.urlopen",
                return_value=io.BytesIO(
                    json.dumps({"response": json.dumps(output)}).encode()
                ),
            ),
        ):
            self.assertEqual(explanation(self.db, "alice", self.pid), output)
        self.now += 60
        self.feed.stamp = self.now
        snapshot(self.db, "alice", self.pid)
        self.now += 60
        self.feed.stamp = self.now
        snapshot(self.db, "alice", self.pid)
        adapter = SimpleNamespace(
            Portfolio=SimpleNamespace(
                from_holding=lambda close: SimpleNamespace(total_return=0.0)
            )
        )
        with (
            patch.dict(os.environ, flags),
            patch.dict(sys.modules, {"vectorbtpro": adapter}),
        ):
            self.assertEqual(
                historical_benchmark(self.db, "alice", self.pid)["observations"], 2
            )
            with self.assertRaises(Problem):
                historical_benchmark(self.db, "alice", self.pid)

    def test_cash_budget_includes_fee_and_fractional_liquidation(self):
        self.db.settings("alice", self.pid, {"fee": "1"})
        self.confirm(self.preview(method="cash_pct", amount="100"))
        self.assertEqual(
            self.db.valuation("alice", self.pid)["portfolio"]["cash"], "0.00"
        )
        self.db.settings("alice", self.pid, {"fractional": False})
        self.confirm(self.preview("Sell", "position_pct", "100", "liquidate-all"))
        self.assertEqual(self.db.valuation("alice", self.pid)["positions"], [])

    def test_saved_run_and_expiration_sweep(self):
        from types import SimpleNamespace

        from paper_trading.research import saved_result

        run = "2026-11-24_120000_fiction"
        root = Path(self.temp.name) / "results"
        d = root / run
        d.mkdir(parents=True)
        (d / "signals.csv").write_text("Ticker,Status,Return\nTEST,BUY,0.04\n")
        (d / "meta.json").write_text(
            json.dumps({"strategy": "fiction", "strategy_sha256": "fixture"})
        )
        app = SimpleNamespace(strategies=SimpleNamespace(results_dir=root))
        self.assertEqual(saved_result(app, run, "TEST")["result"]["Return"], "0.04")
        with self.assertRaises(Problem):
            saved_result(app, "../other", "TEST")
        r = self.preview()
        self.now += 121
        self.db.expire_pending()
        self.assertEqual(self.confirm(r)["status"], "Expired")

    def test_linked_stop_limit_waits_for_recovery(self):
        from paper_trading.exits import check

        data = {
            "side": "Buy",
            "method": "shares",
            "amount": "2",
            "symbol": "TEST",
            "gain_pct": "10",
            "stop_price": "9",
            "limit_price": "8.50",
            "request_key": "stop-limit-001",
        }
        r = self.db.preview("alice", self.pid, data)
        self.assertEqual(json.loads(r["preview"])["exit"]["target"], "11.0")
        self.confirm(r)
        for price, count in [("9.50", 1), ("8.00", 1), ("8.75", 2), ("12", 2)]:
            self.feed.price = price
            self.now += 60
            self.feed.stamp = self.now
            check(self.db, "alice", self.pid)
            self.assertEqual(
                len(self.db.records("alice", self.pid, "transactions")), count
            )
        self.assertEqual(
            self.db.valuation("alice", self.pid)["portfolio"]["cash"], "997.50"
        )

    def test_linked_percentage_target_and_validation(self):
        from paper_trading.exits import check

        data = {
            "side": "Buy",
            "method": "shares",
            "amount": "2",
            "symbol": "TEST",
            "gain_pct": "10",
            "request_key": "target-buy-01",
        }
        for bad in [
            {"gain_pct": "-1"},
            {"stop_price": "9"},
            {"stop_price": "9", "limit_price": "10"},
            {"stop_price": "12", "limit_price": "11"},
        ]:
            with self.assertRaises(Problem):
                self.db.preview("alice", self.pid, {**data, **bad})
        self.confirm(self.db.preview("alice", self.pid, data))
        self.feed.price = "11"
        self.now += 60
        self.feed.stamp = self.now
        check(self.db, "alice", self.pid)
        check(self.db, "alice", self.pid)
        self.assertEqual(
            self.db.valuation("alice", self.pid)["portfolio"]["cash"], "1002.00"
        )

    def test_daily_close_calendar_and_missing_data(self):
        from types import SimpleNamespace

        from paper_trading.market import Market, latest_close

        def epoch(day, hour):
            return int(dt.datetime(2026, 11, day, hour, tzinfo=dt.UTC).timestamp())

        # Thanksgiving, Black Friday before and after the early close, Saturday.
        for day, hour, expected in [
            (25, 23, epoch(25, 21)),
            (26, 18, epoch(25, 21)),
            (27, 17, epoch(25, 21)),
            (27, 18, epoch(27, 18)),
            (28, 14, epoch(27, 18)),
        ]:
            self.assertEqual(latest_close(epoch(day, hour)), expected)
        csv = "Date,Open,High,Low,Close,Volume\n11/25/2026,350,360,349,355.58,100\n"
        feed = Market(
            SimpleNamespace(
                client=SimpleNamespace(quote_history=lambda sym, period: csv)
            )
        )
        self.assertEqual(feed.closing_quote("TEST", epoch(26, 17))["price"], "355.58")
        with self.assertRaises(Problem):
            feed.closing_quote("TEST", epoch(27, 20))

    def test_after_hours_preview_fill_and_expiry(self):
        from paper_trading.market import latest_close

        self.now = int(dt.datetime(2026, 11, 25, 23, tzinfo=dt.UTC).timestamp())
        self.feed.closing_quote = lambda sym, now: {
            "price": "12.50",
            "stamp": latest_close(now),
            "source": "Finviz daily close",
        }
        ticket = {
            "side": "Buy",
            "symbol": "TEST",
            "amount": "2",
            "method": "shares",
            "pricing": "close",
            "request_key": "after-close-1",
        }
        r = self.db.preview("alice", self.pid, ticket)
        self.db.expire_pending()
        self.assertEqual(self.confirm(r)["status"], "Simulated")
        self.assertEqual(self.confirm(r)["status"], "Simulated")
        tx = self.db.records("alice", self.pid, "transactions")[0]
        self.assertEqual(
            (tx["price"], tx["stamp"], tx["created"]),
            ("12.50", latest_close(self.now), self.now),
        )
        r = self.db.preview(
            "alice", self.pid, {**ticket, "request_key": "after-close-2"}
        )
        self.now += 121
        self.assertEqual(self.confirm(r)["status"], "Expired")
