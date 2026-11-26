"""Tests for journal.py: broker CSV parsing, fill matching, stats, store operations."""
import datetime as dt
import json
import math
import random
import tempfile
import unittest
from pathlib import Path

import journal as j


def matched(csv_bytes):
    broker, fills, skipped = j.read_fills(csv_bytes)
    for f, i in zip(fills, j.fill_ids(fills)):
        f["id"] = i
    for n, f in enumerate(fills):
        f["seq"] = n
    return broker, j.match_trades(fills), skipped


class TestParsers(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(j.parse_number("$1,234.50"), 1234.5)
        self.assertEqual(j.parse_number("(12.00)"), -12.0)
        self.assertEqual(j.parse_number("-3"), -3.0)
        for blank in ("", "--", "N/A", None, "abc", "nan", "inf"):
            self.assertIsNone(j.parse_number(blank))

    def test_dates(self):
        cases = {
            "2026-10-30 09:31:05": ("2026-10-30 09:31:05", True),
            "10/30/2026 09:31:05 EDT": ("2026-10-30 09:31:05", True),
            "10/30/2026 as of 10/29/2026": ("2026-10-30", False),
            "2026-10-30, 09:31:05": ("2026-10-30 09:31:05", True),
            "20261030;093105": ("2026-10-30 09:31:05", True),
            "2026-10-30T13:31:05.123Z": ("2026-10-30 13:31:05", True),
            "10/30/26": ("2026-10-30", False),
            "Oct 30, 2026 9:31:05 AM": ("2026-10-30 09:31:05", True),
            "10/30/2026 1:05 PM": ("2026-10-30 13:05:00", True),
            "garbage": (None, False),
        }
        for raw, want in cases.items():
            self.assertEqual(j.parse_when(raw), want, raw)

    def test_sides(self):
        for b in ("Buy", "BOT", "Buy to Cover", "YOU BOUGHT APPLE INC (AAPL) (Cash)", "B"):
            self.assertEqual(j.side_of(b if b != "BOT" else "Buy"), "buy", b)
        for s in ("Sell", "Sell Short", "Short", "YOU SOLD APPLE", "S", "SS"):
            self.assertEqual(j.side_of(s), "sell", s)
        for x in ("Dividend", "CDIV", "Journal", ""):
            self.assertIsNone(j.side_of(x), x)


class TestBrokerFormats(unittest.TestCase):
    def test_schwab(self):
        data = (b'"Transactions  for account XXXX-1234 as of 10/31/2026 12:00:00 ET"\n'
                b'"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
                b'"10/30/2026","Buy to Cover","TSLA","TESLA","5","$200.00","$0.66","-$1,000.66"\n'
                b'"10/29/2026","Sell Short","TSLA","TESLA","5","$210.00","$0.66","$1,049.34"\n'
                b'"10/28/2026","Qualified Dividend","MSFT","MICROSOFT","","","","$7.50"\n'
                b'"10/28/2026","Sell","AAPL","APPLE INC","10","$110.00","$0.10","$1,099.90"\n'
                b'"10/27/2026 as of 10/26/2026","Buy","AAPL","APPLE INC","10","$100.00","","-$1,000.00"\n'
                b'"Transactions Total","","","","","","","$1,148.58"\n')
        broker, trades, skipped = matched(data)
        self.assertEqual(broker, "Schwab")
        by = {t["symbol"]: t for t in trades}
        self.assertEqual(by["AAPL"]["side"], "long")
        self.assertAlmostEqual(by["AAPL"]["pnl"], 100 - 0.10)
        self.assertEqual(by["AAPL"]["entry_time"], "2026-10-27")
        self.assertEqual(by["TSLA"]["side"], "short")
        self.assertAlmostEqual(by["TSLA"]["pnl"], 50 - 1.32)
        self.assertTrue(any("Dividend" in s for s in skipped))
        self.assertEqual(len(trades), 2)

    def test_webull(self):
        data = (b"Name,Symbol,Side,Status,Filled,Total Qty,Price,Avg Price,Time-in-Force,Placed Time,Filled Time\n"
                b"NVIDIA,NVDA,Sell,Filled,20,20,@MKT,131.5,DAY,10/30/2026 10:15:00 EDT,10/30/2026 10:15:01 EDT\n"
                b"NVIDIA,NVDA,Buy,Cancelled,0,20,129.00,,DAY,10/30/2026 09:40:00 EDT,\n"
                b"NVIDIA,NVDA,Buy,Filled,20,20,130.00,130.00,DAY,10/30/2026 09:31:00 EDT,10/30/2026 09:31:02 EDT\n")
        broker, trades, _ = matched(data)
        self.assertEqual(broker, "Webull")
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["entry_time"], t["exit_time"]), ("2026-10-30 09:31:02", "2026-10-30 10:15:01"))
        self.assertAlmostEqual(t["pnl"], 30.0)

    def test_robinhood(self):
        data = (b'"Activity Date","Process Date","Settle Date","Instrument","Description","Trans Code","Quantity",'
                b'"Price","Amount"\n'
                b'"10/30/2026","10/30/2026","11/1/2026","AMD","AMD","Sell","3","$160.00","$480.00"\n'
                b'"10/29/2026","10/29/2026","10/30/2026","AMD","AMD","Buy","3","$150.00","($450.00)"\n'
                b'"10/28/2026","10/28/2026","10/28/2026","","Interest","INT","","","$0.12"\n'
                b'"10/27/2026","10/27/2026","10/28/2026","AAPL 11/20/2026 Call $200.00","Option","BTO","1","$1.20",'
                b'"($120.00)"\n')
        broker, trades, skipped = matched(data)
        self.assertEqual(broker, "Robinhood")
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["pnl"], 30.0)
        self.assertTrue(any("only stocks" in s for s in skipped))

    def test_ibkr_activity_statement(self):
        data = (b"Statement,Header,Field Name,Field Value\n"
                b"Statement,Data,Title,Activity Statement\n"
                b"Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,"
                b"C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n"
                b'Trades,Data,Order,Stocks,USD,QQQ,"2026-10-30, 09:45:00",-10,505,504,5050,-1,-5001,48,10,C\n'
                b'Trades,Data,Order,Stocks,USD,QQQ,"2026-10-30, 09:35:00",10,500,504,-5000,-1,5001,0,40,O\n'
                b"Trades,SubTotal,,Stocks,USD,QQQ,,0,,,50,-2,0,48,50,\n"
                b"Trades,Total,,Stocks,USD,,,,,,50,-2,0,48,50,\n")
        broker, trades, _ = matched(data)
        self.assertEqual(broker, "Interactive Brokers")
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["pnl"], 48.0)  # same as IBKR's own Realized P/L
        self.assertEqual(trades[0]["entry_time"], "2026-10-30 09:35:00")

    def test_fidelity(self):
        data = (b"\n\nRun Date,Action,Symbol,Security Description,Security Type,Quantity,Price ($),Commission ($),"
                b"Fees ($),Accrued Interest ($),Amount ($),Settlement Date\n"
                b"10/30/2026,YOU SOLD SPDR S&P 500 (SPY) (Cash), SPY,SPDR,Cash,-2,580,,0.02,,1159.98,11/01/2026\n"
                b"10/29/2026,YOU BOUGHT SPDR S&P 500 (SPY) (Cash), SPY,SPDR,Cash,2,575,,,,-1150,10/30/2026\n")
        broker, trades, _ = matched(data)
        self.assertEqual(broker, "Fidelity")
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["pnl"], 10 - 0.02)

    def test_generic_signed_quantity_and_separate_time(self):
        data = (b"Date,Time,Ticker,Qty,Fill Price,Fees\n"
                b"2026-10-30,09:31,F,100,11.00,0\n"
                b"2026-10-30,15:59,F,-100,11.25,0\n")
        broker, trades, _ = matched(data)
        self.assertEqual(broker, "Generic")
        self.assertEqual(trades[0]["entry_time"], "2026-10-30 09:31:00")
        self.assertAlmostEqual(trades[0]["pnl"], 25.0)

    def test_missing_columns_named(self):
        with self.assertRaises(j.JournalError) as cm:
            j.read_fills(b"Symbol,Price\nAAPL,1\n")
        self.assertIn("Quantity", str(cm.exception))
        with self.assertRaises(j.JournalError) as cm:
            j.read_fills(b"hello,world\n1,2\n")
        self.assertIn("Couldn't find the trade columns", str(cm.exception))
        with self.assertRaises(j.JournalError):
            j.read_fills(b"\x00\x01PK binary")


class TestMatching(unittest.TestCase):
    def test_scale_in_out_and_flip(self):
        data = (b"Date/Time,Symbol,Side,Quantity,Price,Commission\n"
                b"2026-10-01 09:31:00,AAPL,Buy,100,10,1\n"
                b"2026-10-01 09:40:00,AAPL,Buy,100,12,1\n"
                b"2026-10-01 10:00:00,AAPL,Sell,150,13,1.5\n"
                b"2026-10-02 09:35:00,AAPL,Sell,100,9,1\n"
                b"2026-10-02 11:00:00,AAPL,Buy,50,8,0.5\n"
                b"2026-10-03 09:30:00,MSFT,Buy,10,400,0\n")
        _, trades, _ = matched(data)
        a = [t for t in trades if t["symbol"] == "AAPL"]
        self.assertEqual([t["side"] for t in a], ["long", "short"])
        # long: avg cost 11; 150 @13 (+300) and 50 @9 (-100) = +200; fees 1+1+1.5+half of 1
        self.assertAlmostEqual(a[0]["gross"], 200.0)
        self.assertAlmostEqual(a[0]["fees"], 4.0)
        self.assertAlmostEqual(a[0]["pnl"], 196.0)
        self.assertAlmostEqual(a[0]["exit_price"], 12.0)
        self.assertEqual(a[0]["exit_time"], "2026-10-02 09:35:00")
        self.assertAlmostEqual(a[1]["pnl"], 49.0)  # short 50 @9, cover @8, fees .5+.5
        self.assertEqual(len(set(a[0]["fill_ids"]) & set(a[1]["fill_ids"])), 1)  # the flip fill is in both
        m = [t for t in trades if t["symbol"] == "MSFT"][0]
        self.assertEqual((m["status"], m["open_qty"], m["pnl"]), ("open", 10.0, None))

    def test_random_against_brute_force(self):
        """Total realized P&L of all closed trades equals cash-flow P&L of the flat periods."""
        rng = random.Random(7)
        for trial in range(200):
            rows, pos, cash, fees, t0 = [], 0, 0.0, 0.0, dt.datetime(2026, 1, 5, 9, 30)
            for k in range(rng.randint(2, 30)):
                q = rng.randint(1, 50)
                side = rng.choice(["Buy", "Sell"])
                price = round(rng.uniform(5, 50), 2)
                fee = round(rng.uniform(0, 2), 2)
                rows.append(f"{(t0 + dt.timedelta(minutes=k)):%Y-%m-%d %H:%M:%S},XYZ,{side},{q},{price},{fee}")
            # close out so everything is realized
            pos = sum((int(r.split(',')[3]) if ',Buy,' in r else -int(r.split(',')[3])) for r in rows)
            if pos:
                rows.append(f"{(t0 + dt.timedelta(minutes=99)):%Y-%m-%d %H:%M:%S},XYZ,"
                            f"{'Sell' if pos > 0 else 'Buy'},{abs(pos)},20,0")
            for r in rows:
                _, _, side, q, price, fee = r.split(",")
                cash += (-1 if side == "Buy" else 1) * int(q) * float(price)
                fees += float(fee)
            _, trades, _ = matched(("Date/Time,Symbol,Side,Quantity,Price,Commission\n"
                                    + "\n".join(rows)).encode())
            self.assertTrue(all(t["status"] == "closed" for t in trades), trial)
            self.assertAlmostEqual(math.fsum(t["pnl"] for t in trades), cash - fees, places=4, msg=str(trial))
            self.assertAlmostEqual(math.fsum(t["fees"] for t in trades), fees, places=4)


class TestStats(unittest.TestCase):
    def mk(self, pnl, day, entry="2026-10-01 09:30:00", tags=None):
        return {"id": f"m{abs(hash((pnl, day))) % 16**16:016x}", "status": "closed", "pnl": pnl, "fees": 1.0,
                "entry_time": entry, "exit_time": f"{day} 10:30:00", "has_time": True, "tags": tags or [],
                "symbol": "X", "side": "long"}

    def test_numbers(self):
        ts = [self.mk(100, "2026-10-01"), self.mk(-50, "2026-10-02"), self.mk(-30, "2026-10-02"),
              self.mk(200, "2026-10-05"), self.mk(0, "2026-10-06")]
        s = j.stats(ts)
        self.assertEqual((s["trades"], s["wins"], s["losses"], s["even"]), (5, 2, 2, 1))
        self.assertAlmostEqual(s["win_rate"], 50.0)
        self.assertAlmostEqual(s["net"], 220.0)
        self.assertAlmostEqual(s["profit_factor"], 300 / 80)
        self.assertAlmostEqual(s["avg_win"], 150.0)
        self.assertAlmostEqual(s["avg_loss"], -40.0)
        self.assertAlmostEqual(s["expectancy"], 44.0)
        self.assertAlmostEqual(s["max_drawdown"], 80.0)  # 100 -> 20
        self.assertEqual((s["streak_win"], s["streak_loss"]), (1, 2))
        self.assertEqual(s["curve"][-1][1], 220.0)
        d = j.daily(ts)
        self.assertEqual(d["2026-10-02"], (-80.0, 2))

    def test_breakdown_tags_count_in_each(self):
        ts = [self.mk(10, "2026-10-01", tags=["gap", "news"]), self.mk(-5, "2026-10-02", tags=["gap"])]
        rows = {r["name"]: r for r in j.breakdown(ts, lambda t: t["tags"])}
        self.assertEqual(rows["gap"]["trades"], 2)
        self.assertAlmostEqual(rows["gap"]["net"], 5.0)
        self.assertEqual(rows["news"]["trades"], 1)

    def test_empty(self):
        s = j.stats([])
        self.assertEqual(s["trades"], 0)
        self.assertIsNone(s["win_rate"])
        self.assertIsNone(s["profit_factor"])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = j.JournalStore(Path(self.tmp.name) / "journal")
        self.csv = (b"Date/Time,Symbol,Side,Quantity,Price,Commission\n"
                    b"2026-10-01 09:31:00,AAPL,Buy,10,100,0\n"
                    b"2026-10-01 09:31:00,AAPL,Buy,10,100,0\n"
                    b"2026-10-01 10:00:00,AAPL,Sell,20,101,0\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_is_idempotent_but_keeps_identical_rows(self):
        r1 = self.store.import_csv("a.csv", self.csv)
        self.assertEqual((r1["rows"], r1["added"], r1["duplicates"]), (3, 3, 0))
        r2 = self.store.import_csv("a.csv", self.csv)
        self.assertEqual((r2["added"], r2["duplicates"]), (0, 3))
        trades = self.store.trades()
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["pnl"], 20.0)  # both identical 10-share fills counted
        self.assertEqual(len(self.store.imports()), 2)

    def test_open_position_closed_by_later_file(self):
        self.store.import_csv("a.csv", b"Date/Time,Symbol,Side,Quantity,Price\n2026-10-01 09:31:00,KO,Buy,5,60\n")
        self.assertEqual(self.store.trades()[0]["status"], "open")
        self.store.import_csv("b.csv", b"Date/Time,Symbol,Side,Quantity,Price\n2026-10-03 09:31:00,KO,Sell,5,62\n")
        t = self.store.trades()
        self.assertEqual(len(t), 1)
        self.assertAlmostEqual(t[0]["pnl"], 10.0)

    def test_manual_add_edit_notes_delete(self):
        form = {"symbol": "nvda", "side": "short", "qty": "10", "entry_price": "130", "entry_date": "2026-10-30",
                "entry_time": "9:45", "exit_price": "125.5", "exit_date": "2026-10-30", "exit_time": "11:00",
                "fees": "1", "setup": "Gap fade", "strategy": "fmfi_scanner", "tags": "gap, Gap, news",
                "notes": "Faded the open"}
        rec = self.store.add_manual(form)
        t = self.store.trade(rec["id"])
        self.assertEqual((t["symbol"], t["side"], t["entry_time"]), ("NVDA", "short", "2026-10-30 09:45:00"))
        self.assertAlmostEqual(t["pnl"], 45.0 - 1)
        self.assertEqual(t["tags"], ["gap", "news"])
        self.assertEqual(t["strategy"], "fmfi_scanner")
        self.store.update_manual(rec["id"], {**form, "exit_price": "131", "notes": ""})
        t = self.store.trade(rec["id"])
        self.assertAlmostEqual(t["pnl"], -11.0)
        self.store.save_notes(rec["id"], {"setup": "", "strategy": "", "tags": "", "mistakes": "late", "notes": ""})
        self.assertEqual(self.store.trade(rec["id"])["mistakes"], ["late"])
        self.store.delete(rec["id"])
        self.assertIsNone(self.store.trade(rec["id"]))
        self.assertNotIn(rec["id"], self.store.notes())

    def test_manual_validation(self):
        base = {"symbol": "AAPL", "side": "long", "qty": "1", "entry_price": "1", "entry_date": "2026-10-30"}
        bad = [({"symbol": "aapl 11/20 c"}, "symbol"), ({"qty": "0"}, "shares"), ({"entry_price": "-1"}, "entry price"),
               ({"entry_date": "30/10/2026"}, "Dates"), ({"exit_price": "2"}, "both"),
               ({"exit_date": "2026-10-29", "exit_price": "2"}, "before"), ({"entry_time": "25:00"}, "Times"),
               ({"side": "up"}, "Side")]
        for change, words in bad:
            with self.assertRaises(j.JournalError, msg=str(change)) as cm:
                self.store.add_manual({**base, **change})
            self.assertIn(words.lower(), str(cm.exception).lower(), change)
        rec = self.store.add_manual(base)  # open trade
        self.assertEqual(self.store.trade(rec["id"])["status"], "open")

    def test_delete_imported_trade_removes_its_fills_only(self):
        self.store.import_csv("a.csv", self.csv + b"2026-10-02 09:31:00,MSFT,Buy,1,400,0\n")
        aapl = next(t for t in self.store.trades() if t["symbol"] == "AAPL")
        self.store.save_notes(aapl["id"], {"notes": "x"})
        self.store.delete(aapl["id"])
        left = self.store.trades()
        self.assertEqual([t["symbol"] for t in left], ["MSFT"])
        self.assertEqual(self.store.notes(), {})

    def test_clear_and_export(self):
        self.store.import_csv("a.csv", self.csv)
        self.store.add_manual({"symbol": "T", "side": "long", "qty": "1", "entry_price": "20",
                               "entry_date": "2026-10-01", "notes": 'has "quotes", commas'})
        out = self.store.export_csv()
        self.assertIn('"has ""quotes"", commas"', out)
        self.assertEqual(len(out.strip().splitlines()), 3)
        self.assertIn("Deleted all 2 trades", self.store.clear())
        self.assertEqual(self.store.trades(), [])

    def test_bad_ids(self):
        self.assertIsNone(self.store.trade("../etc"))
        with self.assertRaises(j.JournalError):
            self.store.delete("m0000000000000000")

    def test_filters(self):
        self.store.import_csv("a.csv", self.csv)
        rec = self.store.add_manual({"symbol": "T", "side": "short", "qty": "1", "entry_price": "20",
                                     "entry_date": "2026-10-05", "exit_date": "2026-10-06", "exit_price": "21",
                                     "tags": "news"})
        ts = self.store.trades()
        self.assertEqual(len(j.filter_trades(ts, {"side": "short"})), 1)
        self.assertEqual(len(j.filter_trades(ts, {"from": "2026-10-02"})), 1)
        self.assertEqual(len(j.filter_trades(ts, {"result": "win"})), 1)
        self.assertEqual(j.filter_trades(ts, {"tag": "NEWS"})[0]["id"], rec["id"])
        self.assertEqual(len(j.filter_trades(ts, {"day": "2026-10-06"})), 1)


if __name__ == "__main__":
    unittest.main()
