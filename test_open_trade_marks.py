import unittest,threading,time
import journal
from open_trade_marks import OpenTradeMarks,valuation
class TestMarks(unittest.TestCase):
 def trade(self,side='long'):
  return dict(status='open',symbol='AAPL',qty=10,open_qty=10,entry_price=100,fees=2,side=side)
 def test_long_short_and_fees(self):
  self.assertEqual(valuation(self.trade(),{'price':110}),(98,9.8))
  self.assertEqual(valuation(self.trade('short'),{'price':90}),(98,9.8))
  self.assertEqual(valuation(self.trade(),None),(None,None))
 def test_partial_exits_and_scaling(self):
  fills=[]
  for i,(side,qty,price) in enumerate([('buy',10,100),('sell',5,110),('buy',5,120)]):
   fills.append(dict(id=str(i),seq=i,when='2026-10-30',has_time=False,symbol='AAPL',side=side,qty=qty,price=price,fees=0))
  t=journal.match_trades(fills)[0]
  self.assertEqual(t['mark_basis'],110);self.assertEqual(t['open_qty'],10)
  net,ret=valuation(t,{'price':120});self.assertAlmostEqual(net,150);self.assertAlmostEqual(ret,9.375,places=4)
 def test_background_refresh_cache_and_failure(self):
  class Client:
   count=0
   def quote_history(self,symbol,period):
    self.count+=1;return 'Date,Open,High,Low,Close,Volume\n2026-10-30,100,112,98,110,1000\n'
  client=Client();s=OpenTradeMarks(client)
  s.snapshot([self.trade()])
  end=time.monotonic()+2
  while s.busy and time.monotonic()<end:time.sleep(.01)
  quotes,busy=s.snapshot([self.trade()]);self.assertEqual(quotes['AAPL']['price'],110);self.assertFalse(busy);self.assertEqual(client.count,1)
  class Bad:
   def quote_history(self,*args):raise ValueError('private provider error')
  s.client=Bad();s.attempts.clear();s.snapshot([self.trade()])
  end=time.monotonic()+2
  while s.busy and time.monotonic()<end:time.sleep(.01)
  self.assertEqual(s.quotes['AAPL']['price'],110)

 def test_table_shows_marks_without_changing_closed_stats(self):
  from test_server import LiveServer
  import server
  from types import SimpleNamespace
  srv=LiveServer()
  try:
   srv.app.journal.add_manual(dict(symbol='AAPL',side='long',entry_date='2026-10-30',entry_price='100',qty='10',fees='2'))
   srv.app.open_marks=SimpleNamespace(snapshot=lambda trades:({'AAPL':dict(price=110,day='2026-10-30',fetched=time.time(),stale=False)},False))
   page=server.render_journal_trades(srv.app,{'from':'','to':''})
   self.assertIn('+$98.00',page);self.assertIn('+9.80%',page);self.assertIn('Fetched ',page);self.assertNotIn('Open · Finviz',page);self.assertNotIn('Open-trade values use',page)
   self.assertIsNone(srv.app.journal.trades()[0]['pnl'])
  finally:srv.close()
