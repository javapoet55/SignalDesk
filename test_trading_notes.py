import tempfile, unittest, os
from pathlib import Path
from trading_notes import NoteStore, NoteError, render_content
from test_server import LiveServer

class TestNotes(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=NoteStore(Path(self.tmp.name)/'notes')
        self.form=dict(symbol='aapl',title='My plan',status='watching',date='2026-10-31',entry='100',stop='95',target='115',thesis='<script>alert(1)</script>')
    def test_persist_edit_and_conflict(self):
        ident=self.store.save(self.form);row=self.store.list()[0];self.assertEqual(row['symbol'],'AAPL')
        self.assertEqual(os.stat(self.store.path).st_mode&0o777,0o600)
        self.assertEqual(NoteStore(self.store.root).list()[0]['id'],ident)
        changed=dict(self.form,id=ident,revision=row['updated'],title='Edited')
        self.store.save(changed)
        with self.assertRaises(NoteError):self.store.save(changed)
        self.assertEqual(self.store.list()[0]['title'],'Edited')
    def test_validation(self):
        for extra in [dict(symbol='../bad'),dict(entry='nan'),dict(target='-1'),dict(date='wrong'),dict(status='invalid')]:
            with self.subTest(extra=extra),self.assertRaises(NoteError):self.store.save(dict(self.form,**extra))
        self.assertEqual(self.store.list(),[])
    def test_corruption_not_overwritten(self):
        self.store.path.write_text('bad-json')
        with self.assertRaises(NoteError):self.store.save(self.form)
        self.assertEqual(self.store.path.read_text(),'bad-json')
    def test_script_data_escaped(self):
        self.store.save(self.form);body=render_content(self.store.list(),{},'2026-10-31')
        self.assertNotIn('<script>alert(1)</script>',body)
        self.assertIn('\\u003cscript>',body)

class TestNotesWeb(unittest.TestCase):
    def setUp(self):
        self.srv=LiveServer();self.addCleanup(self.srv.close)
    def test_routes_and_persistence(self):
        r,_=self.srv.request('GET','/notes',auth=None);self.assertEqual(r.status,303)
        r,b=self.srv.request('GET','/notes');self.assertEqual(r.status,200);self.assertIn(b'Trading Notes',b)
        form=dict(symbol='NVDA',title='Test note',status='watching',date='2026-10-31',entry='142',stop='138',target='154')
        r,_=self.srv.request('POST','/notes/save',form=form,headers={'Origin':f'http://127.0.0.1:{self.srv.port}'})
        self.assertEqual(r.status,303);self.assertTrue(r.getheader('Location').startswith('/notes?id='))
        self.assertEqual(self.srv.app.notes.list()[0]['symbol'],'NVDA')
        r,b=self.srv.request('GET',r.getheader('Location'));self.assertIn(b'Test note',b)
        r,_=self.srv.request('POST','/notes/save',form=form,headers={'Origin':'https://evil.example'});self.assertEqual(r.status,403)
        r,_=self.srv.request('POST','/notes/save',form=dict(form,symbol='../invalid'),headers={'Origin':f'http://127.0.0.1:{self.srv.port}'});self.assertEqual(r.status,400)

class TestGeneralNoteSave(unittest.TestCase):
    def test_text_only_persists_and_reopens(self):
        from trading_notes import NoteStore, NoteError
        with tempfile.TemporaryDirectory() as folder:
            store=NoteStore(Path(folder)/'notes')
            ident=store.save(dict(thesis='My market observations',date='2026-11-01',status='watching'))
            row=NoteStore(store.root).list()[0]
            self.assertEqual(row['id'],ident);self.assertEqual(row['symbol'],'')
            self.assertEqual(row['title'],'My market observations')
            with self.assertRaises(NoteError):store.save(dict(date='2026-11-01',status='watching'))
