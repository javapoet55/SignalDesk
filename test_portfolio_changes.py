import unittest
from test_server import LiveServer
import server
class TestPortfolio(unittest.TestCase):
 def test_open_filter_sold_and_name(self):
  s=LiveServer();self.addCleanup(s.close)
  s.app.journal.add_manual(dict(symbol='OPEN',side='long',entry_date='2026-10-20',entry_price='100',qty='1'))
  s.app.journal.add_manual(dict(symbol='CLOSED',side='long',entry_date='2026-10-20',entry_price='100',qty='1',exit_date='2026-10-25',exit_price='110'))
  p=server.render_journal_trades(s.app,{'from':'','to':'','status':'open'})
  self.assertIn('<h1>Trades &amp; Portfolio</h1>',p);self.assertIn('>Open Trades</option>',p)
  self.assertIn('<strong>OPEN',p);self.assertNotIn('<strong>CLOSED',p)
  self.assertNotIn("type='hidden' name='status'",p)
  p=server.render_journal_trades(s.app,{'from':'','to':'','status':'','sort':'sold','direction':'asc'})
  self.assertIn('2026-10-25</td>',p);self.assertLess(p.index('<strong>CLOSED'),p.index('<strong>OPEN'))
  self.assertIn('Sort by Sold',p)
