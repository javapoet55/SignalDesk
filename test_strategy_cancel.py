import unittest,threading,queue,time,json
from test_server import LiveServer
import strategies as sm
class TestCancel(unittest.TestCase):
 def setUp(self):
  self.s=LiveServer();self.addCleanup(self.s.close);self.st=self.s.app.strategies
 def test_queued_exact_and_stale(self):
  st=self.st
  with st._queue_lock:
   st.queued.append(dict(job_id='one',name='x'))
  self.assertIn('cancelled',st.cancel('one'))
  self.assertEqual(st.queued,[])
  with self.assertRaises(ValueError):st.cancel('missing')
  rid='2026-11-01_080000_example';folder=st.results_dir/rid;folder.mkdir();(folder/'meta.json').write_text(json.dumps(dict(id=rid,status='running')))
  self.assertIn('cancelled',st.cancel(rid));self.assertEqual(sm.read_meta(folder)['status'],'cancelled')
 def test_running_cancellation_and_next_job(self):
  st=self.st;entered=threading.Event();next_done=threading.Event()
  def fake(job):
   if job['job_id']=='two':next_done.set();return
   rid='2026-11-01_080001_example';folder=st.results_dir/rid;folder.mkdir();(folder/'meta.json').write_text(json.dumps(dict(id=rid,status='running')))
   with st._queue_lock:st.current['result_id']=rid
   entered.set()
   while not st._cancel_event.wait(.01):pass
   st._check_cancel()
  st._run=fake
  st._jobs.put(dict(job_id='one',name='x'));self.assertTrue(entered.wait(2))
  self.assertIn('Stop requested',st.cancel('one'))
  st._jobs.put(dict(job_id='two',name='x'));self.assertTrue(next_done.wait(2))
  self.assertEqual(sm.read_meta(st.results_dir/'2026-11-01_080001_example')['status'],'cancelled')
 def test_web_auth_origin_and_stop_button(self):
  rid='2026-11-01_080002_example';folder=self.st.results_dir/rid;folder.mkdir();(folder/'meta.json').write_text(json.dumps(dict(id=rid,status='running',strategy='example')))
  r,b=self.s.request('GET','/strategies/history/panel');self.assertIn(b"action='/strategies/cancel'",b)
  r,_=self.s.request('POST','/strategies/cancel',form={'id':rid},headers={'Origin':'https://evil.example'});self.assertEqual(r.status,403)
  r,_=self.s.request('POST','/strategies/cancel',auth=None,form={'id':rid});self.assertEqual(r.status,303)
  self.assertEqual(sm.read_meta(folder)['status'],'running')
  r,_=self.s.request('POST','/strategies/cancel',form={'id':rid},headers={'Origin':f'http://127.0.0.1:{self.s.port}'});self.assertEqual(r.status,303)
  self.assertEqual(sm.read_meta(folder)['status'],'cancelled')

 def test_sandbox_process_group_stops(self):
  from unittest.mock import patch
  import subprocess,sys
  real_popen=subprocess.Popen;created=[];event=threading.Event()
  def launch(*args,**kwargs):
   p=real_popen([sys.executable,'-c','import time;time.sleep(20)'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
   created.append(p);threading.Timer(.2,event.set).start();return p
  with patch.object(sm.subprocess,'Popen',side_effect=launch):
   start=time.monotonic()
   code,_=sm.run_sandboxed([],self.st.root,None,10,10,256,cancel_event=event)
  self.assertEqual(code,-2);self.assertLess(time.monotonic()-start,3);self.assertIsNotNone(created[0].poll())
