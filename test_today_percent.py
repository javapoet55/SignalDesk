import unittest,time
from types import SimpleNamespace
from test_server import LiveServer
from open_trade_marks import OpenTradeMarks
import server
class TestTodayPercent(unittest.TestCase):
 def test_previous_trading_day_and_missing_history(self):
  class Client:
   def quote_history(self,*args):return 'Date,Open,High,Low,Close,Volume\n2026-10-30,100,100,100,100,1\n2026-11-02,105,110,104,110,2\n'
  s=OpenTradeMarks(Client());s._refresh(['AAPL']);self.assertAlmostEqual(s.quotes['AAPL']['today_pct'],10)
  class Missing:
   def quote_history(self,*args):return 'Date,Open,High,Low,Close,Volume\n2026-11-02,105,110,104,110,2\n'
  s=OpenTradeMarks(Missing());s._refresh(['AAPL']);self.assertIsNone(s.quotes['AAPL']['today_pct'])
 def test_column_placement_and_sort(self):
  s=LiveServer();self.addCleanup(s.close)
  for sym in ['AAA','BBB']:
   s.app.journal.add_manual(dict(symbol=sym,side='long',entry_date='2026-10-30',entry_price='100',qty='1'))
  s.app.open_marks=SimpleNamespace(snapshot=lambda trades:({'AAA':dict(price=98,day='2026-11-02',fetched=time.time(),stale=False,today_pct=-2), 'BBB':dict(price=105,day='2026-11-02',fetched=time.time(),stale=False,today_pct=5)},False))
  page=server.render_journal_trades(s.app,{'from':'','to':'','sort':'today_pct','direction':'desc'})
  self.assertIn('Today %',page);self.assertIn('+5.00%',page);self.assertIn('-2.00%',page)
  self.assertLess(page.index('Sort by Symbol'),page.index('Sort by Today %'));self.assertLess(page.index('Sort by Today %'),page.index('Sort by Shares'))
  self.assertLess(page.index('<strong>BBB'),page.index('<strong>AAA'))
