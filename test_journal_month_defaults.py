import unittest,datetime as dt
from test_server import LiveServer
import server
class TestMonthDefaults(unittest.TestCase):
 def test_month_dates_and_explicit_override(self):
  s=LiveServer();self.addCleanup(s.close)
  s.app.clock=lambda:dt.datetime(2026,11,14,20,tzinfo=dt.timezone.utc)
  s.app.journal.add_manual(dict(symbol='AAPL',side='long',entry_date='2026-11-10',entry_price='100',qty='1'))
  page=server.render_journal(s.app,{})
  self.assertIn("name='from' value='2026-11-01'",page);self.assertIn("name='to' value='2026-11-14'",page)
  page=server.render_journal(s.app,{'from':'2026-10-01','to':'2026-10-31'})
  self.assertIn("name='from' value='2026-10-01'",page)
  page=server.render_journal(s.app,{'from':'','to':''})
  self.assertIn("name='from' value=''",page)
