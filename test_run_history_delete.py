import unittest,json
from test_server import LiveServer, _REAL_PROTECTED_CHANGE_LABEL
import server
class TestRunHistoryDelete(unittest.TestCase):
 def setUp(self):
  self.s=LiveServer();self.addCleanup(self.s.close);self.st=self.s.app.strategies
  self.rid='2026-11-01_080000_example';self.folder=self.st.results_dir/self.rid;self.folder.mkdir()
  (self.folder/'meta.json').write_text(json.dumps(dict(id=self.rid,status='ok',strategy='example')))
  (self.folder/'output.txt').write_text('example log');(self.folder/'signals.csv').write_text('Ticker\nAAPL\n')
 def test_delete_only_selected_run(self):
  other=self.st.results_dir/'2026-11-01_080001_example';other.mkdir()
  self.st.delete_result(self.rid);self.assertFalse(self.folder.exists());self.assertTrue(other.exists())
 def test_running_and_unsafe_rejected(self):
  for rid in ['../outside','',self.rid+'/../x']:
   with self.assertRaises(ValueError):self.st.delete_result(rid)
  (self.folder/'meta.json').write_text(json.dumps(dict(status='running')))
  with self.assertRaises(ValueError):self.st.delete_result(self.rid)
  self.assertTrue(self.folder.exists())
 def test_ui_auth_origin_and_otp_label(self):
  r,b=self.s.request('GET','/strategies');self.assertIn(b"action='/strategies/results/delete'",b);self.assertIn(b'class=\'ghost delete-run\'',b)
  self.assertIn('Delete run history',_REAL_PROTECTED_CHANGE_LABEL('/strategies/results/delete',('id='+self.rid).encode(),'application/x-www-form-urlencoded'))
  r,_=self.s.request('POST','/strategies/results/delete',auth=None,form={'id':self.rid});self.assertEqual(r.status,303);self.assertTrue(self.folder.exists())
  r,_=self.s.request('POST','/strategies/results/delete',form={'id':self.rid},headers={'Origin':'https://evil.example'});self.assertEqual(r.status,403);self.assertTrue(self.folder.exists())
