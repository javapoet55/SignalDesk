import unittest,tempfile
from pathlib import Path
from notebook import Notebook,NotebookError
from test_server import LiveServer
class TestNotebook(unittest.TestCase):
 def test_limit_persist_conflict_escape(self):
  with tempfile.TemporaryDirectory() as d:
   s=Notebook(Path(d)/'notes');s.save('x'*10000,'');row=s.read();self.assertEqual(len(row['text']),10000)
   with self.assertRaises(NotebookError):s.save('x'*10001,row['revision'])
   with self.assertRaises(NotebookError):s.save('stale','')
   self.assertEqual(s.path.stat().st_mode&0o777,0o600)
   self.assertEqual(Notebook(s.root).read()['text'],row['text'])
 def test_web(self):
  s=LiveServer();self.addCleanup(s.close)
  r,_=s.request('GET','/notebook',auth=None);self.assertEqual(r.status,303)
  r,b=s.request('GET','/notebook');self.assertEqual(r.status,200);self.assertIn(b"maxlength='10000'",b)
  self.assertLess(b.index(b'>TradingView</a>'),b.index(b'>Notes</a>'))
  h={'Origin':f'http://127.0.0.1:{s.port}'}
  r,_=s.request('POST','/notebook/save',form={'text':'<script>alert(1)</script>','revision':''},headers=h);self.assertEqual(r.status,303)
  r,b=s.request('GET','/notebook');self.assertIn(b'&lt;script&gt;',b);self.assertNotIn(b'<script>alert(1)</script>',b)
  row=s.app.notebook.read()
  r,_=s.request('POST','/notebook/save',form={'text':'x'*10001,'revision':row['revision']},headers=h);self.assertEqual(r.status,400)
  self.assertEqual(s.app.notebook.read()['text'],row['text'])
  r,_=s.request('POST','/notebook/save',form={'text':'bad'},headers={'Origin':'https://evil.example'});self.assertEqual(r.status,403)
