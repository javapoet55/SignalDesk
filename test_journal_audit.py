"""Journal fixes from the module audit: drill-down links, same-day exits, and deleting a trade that
shares an execution with the next one."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import journal
import server
from test_server import LiveServer


def store():
    return journal.JournalStore(Path(tempfile.mkdtemp()))


def manual(**values):
    return dict(dict(symbol="ABC", side="long", qty="10", entry_date="2026-10-30", entry_price="100"), **values)


class SameDayExit(unittest.TestCase):
    def test_exit_without_a_time_on_the_entry_day_is_accepted(self):
        s = store()
        rec = s.add_manual(manual(entry_time="09:31", exit_date="2026-10-30", exit_price="110"))
        self.assertEqual((rec["entry_time"], rec["exit_time"]), ("2026-10-30 09:31:00", "2026-10-30"))
        trade = s.trades()[0]
        self.assertEqual((trade["status"], trade["pnl"]), ("closed", 100.0))
        # The other way round: a timed exit after a date-only entry on the same day.
        s.add_manual(manual(symbol="DEF", exit_date="2026-10-30", exit_time="15:00", exit_price="90"))
        self.assertEqual(len(s.trades()), 2)

    def test_a_real_exit_before_entry_is_still_refused(self):
        s = store()
        for values in (dict(entry_time="09:31", exit_date="2026-10-29", exit_price="110"),
                       dict(exit_date="2026-10-29", exit_time="15:00", exit_price="110"),
                       dict(entry_time="09:31", exit_date="2026-10-30", exit_time="09:30", exit_price="110"),
                       dict(exit_date="2026-10-29", exit_price="110")):
            with self.subTest(values=values), self.assertRaisesRegex(journal.JournalError, "exit is before the entry"):
                s.add_manual(manual(**values))
        self.assertEqual(s.trades(), [])
        s.add_manual(manual(entry_time="09:31", exit_date="2026-10-30", exit_time="09:31", exit_price="110"))
        self.assertEqual(len(s.trades()), 1)


FLIP = (b"Date,Symbol,Action,Quantity,Price,Fees\n"
        b"10/26/2026,ABC,Buy,10,100,0\n"
        b"10/27/2026,ABC,Sell,15,110,3\n"      # closes the 10 long and opens a 5 short
        b"10/28/2026,ABC,Buy,5,105,0\n"
        b"10/29/2026,XYZ,Buy,4,50,0\n")


class DeleteSharedExecution(unittest.TestCase):
    def setUp(self):
        self.s = store()
        self.s.import_csv("flip.csv", FLIP)
        trades = {(t["symbol"], t["side"]): t for t in self.s.trades()}
        self.long, self.short, self.other = trades[("ABC", "long")], trades[("ABC", "short")], trades[("XYZ", "long")]

    def test_starting_point(self):
        self.assertEqual((self.long["qty"], self.long["pnl"]), (10.0, 98.0))        # +100, less 2 of the 3 fee
        self.assertEqual((self.short["qty"], self.short["pnl"]), (5.0, 24.0))       # +25, less 1 of the 3 fee
        shared = set(self.long["fill_ids"]) & set(self.short["fill_ids"])
        self.assertEqual(len(shared), 1)
        fid = shared.pop()
        self.assertEqual((self.long["fill_shares"][fid], self.short["fill_shares"][fid]), (10.0, 5.0))

    def test_deleting_the_first_trade_leaves_the_second_exactly_as_it_was(self):
        self.s.delete(self.long["id"])
        left = {(t["symbol"], t["side"]): t for t in self.s.trades()}
        self.assertEqual(set(left), {("ABC", "short"), ("XYZ", "long")})
        after = left[("ABC", "short")]
        for key in ("id", "qty", "entry_price", "exit_price", "pnl", "fees", "entry_time", "exit_time", "status"):
            self.assertEqual(after[key], self.short[key], key)
        self.s.delete(after["id"])                                   # and it can then be deleted cleanly
        self.assertEqual([t["symbol"] for t in self.s.trades()], ["XYZ"])

    def test_deleting_the_second_trade_leaves_the_first_exactly_as_it_was(self):
        self.s.delete(self.short["id"])
        left = {(t["symbol"], t["side"]): t for t in self.s.trades()}
        self.assertEqual(set(left), {("ABC", "long"), ("XYZ", "long")})
        after = left[("ABC", "long")]
        for key in ("id", "qty", "entry_price", "exit_price", "pnl", "fees", "entry_time", "exit_time", "status"):
            self.assertEqual(after[key], self.long[key], key)

    def test_an_ordinary_trade_is_removed_whole_and_notes_go_with_it(self):
        self.s.save_notes(self.other["id"], {"notes": "keep?"})
        self.s.delete(self.other["id"])
        self.assertEqual(sorted(t["side"] for t in self.s.trades()), ["long", "short"])
        self.assertNotIn(self.other["id"], self.s.notes())
        self.assertEqual({t["pnl"] for t in self.s.trades()}, {98.0, 24.0})


class DrillDownLinks(unittest.TestCase):
    """The calendar and the breakdown tables link to closed trades, usually older than the list's
    default 40-day window. Those links must show the trades they counted."""

    def setUp(self):
        self.srv = LiveServer()
        self.addCleanup(self.srv.close)
        app = self.srv.app
        app.clock = lambda: dt.datetime(2026, 11, 27, 20, tzinfo=dt.timezone.utc)
        app.open_marks = None
        add = app.journal.add_manual
        add(manual(symbol="OLDWIN", entry_date="2026-06-01", exit_date="2026-06-03", exit_price="120",
                   tags="gap up", setup="Breakout", strategy="fmfi"))
        add(manual(symbol="OLDLOSS", entry_date="2026-06-02", exit_date="2026-06-03", exit_price="90", mistakes="chased"))
        add(manual(symbol="NEWOPEN", entry_date="2026-11-20"))
        add(manual(symbol="OLDOPEN", entry_date="2026-06-01", tags="gap up"))
        self.app = app

    def shown(self, query):
        page = server.render_journal_trades(self.app, query)
        return {name for name in ("OLDWIN", "OLDLOSS", "NEWOPEN", "OLDOPEN") if f"<strong>{name}</strong>" in page}, page

    def test_plain_page_keeps_its_default_view(self):
        names, page = self.shown({})
        self.assertEqual(names, {"NEWOPEN"})                       # open trades, last 40 days
        self.assertIn("name='from' value='2026-10-18'", page)
        self.assertIn("<option value='open' selected>Open Trades</option>", page)

    def test_calendar_day_shows_that_days_closed_trades(self):
        names, page = self.shown({"day": "2026-06-03"})
        self.assertEqual(names, {"OLDWIN", "OLDLOSS"})
        self.assertIn("<option value='' selected>All trades</option>", page)
        self.assertIn("2 trades", page)

    def test_breakdown_links_show_the_trades_they_counted(self):
        self.assertEqual(self.shown({"tag": "gap up"})[0], {"OLDWIN", "OLDOPEN"})
        self.assertEqual(self.shown({"tag": "chased"})[0], {"OLDLOSS"})
        self.assertEqual(self.shown({"setup": "Breakout"})[0], {"OLDWIN"})
        self.assertEqual(self.shown({"strategy": "fmfi"})[0], {"OLDWIN"})
        self.assertEqual(self.shown({"result": "loss"})[0], {"OLDLOSS"})
        self.assertEqual(self.shown({"symbol": "OLDWIN"})[0], {"OLDWIN"})          # the By symbol table

    def test_a_choice_made_on_the_page_is_never_overridden(self):
        # The filter form always sends a status, so these are the user's own choices.
        self.assertEqual(self.shown({"tag": "gap up", "status": "open", "from": "", "to": ""})[0], {"OLDOPEN"})
        self.assertEqual(self.shown({"strategy": "fmfi", "status": "open", "from": "", "to": ""})[0], set())
        self.assertEqual(self.shown({"day": "2026-06-03", "status": "open"})[0], set())
        self.assertEqual(self.shown({"status": "", "from": "", "to": ""})[0], {"OLDWIN", "OLDLOSS", "NEWOPEN", "OLDOPEN"})

    def test_sort_links_inside_a_drill_down_stay_in_it(self):
        _, page = self.shown({"tag": "gap up"})
        link = page[page.index("aria-label='Sort by Symbol'") - 200:page.index("aria-label='Sort by Symbol'")]
        for part in ("tag=gap+up", "status=&amp;", "from=&amp;", "sort=symbol"):
            self.assertIn(part, link)

    def test_overview_links_resolve_over_http(self):
        _, body = self.srv.request("GET", "/journal?from=&to=")
        overview = body.decode()
        self.assertIn("href='/journal/trades?day=2026-06-03'", overview)
        _, body = self.srv.request("GET", "/journal/trades?day=2026-06-03")
        page = body.decode()
        self.assertIn("<strong>OLDWIN</strong>", page)
        self.assertNotIn("No trades match", page)


if __name__ == "__main__":
    unittest.main()
