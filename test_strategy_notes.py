import unittest
from test_server import LiveServer
class TestStrategyNotes(unittest.TestCase):
 def setUp(self):
  self.s=LiveServer();self.addCleanup(self.s.close);self.st=self.s.app.strategies
  (self.st.scripts_dir/'example.py').write_text('# fixture')
  self.st._save_meta({'example':{'file':'example.py','kind':'custom'}})
 def test_save_limit_and_page(self):
  self.st.set_note('example','x'*1000)
  self.assertEqual(len(self.st.meta()['example']['note']),1000)
  with self.assertRaises(ValueError):self.st.set_note('example','x'*1001)
  with self.assertRaises(ValueError):self.st.set_note('missing','note')
  r,b=self.s.request('POST','/strategies/note',form={'name':'example','note':'<hello> & reminders'},headers={'Origin':f'http://127.0.0.1:{self.s.port}'})
  self.assertEqual(r.status,303);self.assertEqual(self.st.meta()['example']['note'],'<hello> & reminders')
  r,b=self.s.request('GET','/strategies');page=b.decode()
  self.assertIn('&lt;hello&gt; &amp; reminders',page)
  self.assertLess(page.index("class='ghost strategy-note'"),page.index("class='ghost del'"))
  self.assertIn("maxlength='1000'",page);self.assertNotIn('Delete</button></label>',page)
  r,_=self.s.request('POST','/strategies/note',form={'name':'example','note':'no'},headers={'Origin':'https://evil.example'})
  self.assertEqual(r.status,403)
  self.st.set_note('example','');self.assertEqual(self.st.meta()['example']['note'],'')
