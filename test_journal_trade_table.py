import unittest,datetime as dt,urllib.parse
from test_server import LiveServer
import server,journal
class TestTradesTable(unittest.TestCase):
 def setUp(self):
  self.s=LiveServer();self.addCleanup(self.s.close)
  self.s.app.clock=lambda:dt.datetime(2026,11,1,20,tzinfo=dt.timezone.utc)
 def test_defaults_and_sort_headers(self):
  self.s.app.journal.add_manual(dict(symbol='ZZZ',side='long',entry_date='2026-10-30',entry_price='10',qty='2'))
  self.s.app.journal.add_manual(dict(symbol='AAA',side='long',entry_date='2026-10-29',entry_price='100',qty='10'))
  page=server.render_journal_trades(self.s.app,{})
  self.assertIn("name='from' value='2026-09-22'",page);self.assertIn("name='to' value='2026-11-01'",page)
  self.assertEqual(page.count('aria-sort='),12)
  page=server.render_journal_trades(self.s.app,{'sort':'symbol','direction':'asc'})
  self.assertLess(page.index('<strong>AAA'),page.index('<strong>ZZZ'))
  page=server.render_journal_trades(self.s.app,{'sort':'shares','direction':'asc'})
  self.assertLess(page.index('<strong>ZZZ'),page.index('<strong>AAA'))
  page=server.render_journal_trades(self.s.app,{'from':'','to':''})
  self.assertIn("name='from' value=''",page)
 def test_masked_existing_import_account(self):
  content=b'Date,Symbol,Action,Quantity,Price\n10/30/2026,AAPL,Buy,10,100\n10/31/2026,AAPL,Sell,10,110\n'
  self.s.app.journal.import_csv('History_for_Account_000001234.csv',content)
  t=self.s.app.journal.trades()[0];self.assertEqual(t['account_suffix'],'1234')
  page=server.render_journal_trades(self.s.app,{})
  self.assertIn('•••• 1234',page);self.assertNotIn('000001234',page)
  self.assertEqual(journal.fill_account({'file':'History_for_Account_123.csv'}),'123')
