"""Private general notebook, separate from ticker-linked trading notes."""
import datetime as dt
import hashlib
import html
import json
import os
import threading
from pathlib import Path

class NotebookError(ValueError):
    pass

class Notebook:
    def __init__(self, root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700);self.root.chmod(0o700)
        self.path=self.root/'notes.json';self.lock=threading.RLock()
    def read(self):
        with self.lock:
            if not self.path.exists():return {'text':'','updated':'','revision':''}
            try:
                raw=self.path.read_bytes();data=json.loads(raw)
                if not isinstance(data,dict) or not isinstance(data.get('text'),str):raise ValueError()
                return dict(data,revision=hashlib.sha256(raw).hexdigest())
            except (ValueError,OSError) as exc:raise NotebookError('Notes could not be read. Existing data has been preserved.') from exc
    def save(self,text,revision):
        if len(text)>10000:raise NotebookError('Notes can contain at most 10,000 characters.')
        with self.lock:
            if self.read()['revision']!=revision:raise NotebookError('Notes changed in another tab. Copy your changes, then reload before saving.')
            data={'text':text,'updated':dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}
            tmp=self.path.with_suffix('.tmp');fd=os.open(tmp,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:
                json.dump(data,f);f.flush();os.fsync(f.fileno())
            os.chmod(tmp,0o600);os.replace(tmp,self.path)

def content(data,error='',saved=False):
    e=html.escape
    return ("<style>.notebook-head{display:flex;justify-content:space-between;gap:16px;align-items:center;margin:28px 0 24px}"
            ".notebook-head h1{font-size:32px;margin:0 0 6px;letter-spacing:-.8px}.notebook-head p{margin:0}"
            ".notebook-card textarea{display:block;box-sizing:border-box;width:100%;min-height:430px;resize:vertical;"
            "font:inherit;line-height:1.8;padding:18px;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:10px}"
            ".notebook-foot{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-top:18px}"
            ".notebook-foot span{font-size:12px;color:var(--muted)}.notebook-card label{display:block;margin-bottom:12px;font-weight:600}"
            "@media(max-width:640px){.notebook-card textarea{font-size:16px;padding:12px}.notebook-head h1{font-size:28px}}</style>"
            "<div class='notebook-head'><div><h1>Notes</h1><p class='muted'>Your private space for ideas, reminders, and market observations.</p></div></div>"
            + ("<div class='warn' role='alert'>"+e(error)+"</div>" if error else '')
            + ("<div class='okbox' role='status'>Notes saved.</div>" if saved else '')
            + "<form class='card notebook-card' method='post' action='/notebook/save'><label for='general-notes'>My notes</label>"
            "<input type='hidden' name='revision' value='"+e(data.get('revision',''),quote=True)+"'>"
            "<textarea id='general-notes' name='text' maxlength='10000' placeholder='Write your notes here…'>"+e(data['text'])+"</textarea>"
            "<div class='notebook-foot'><span id='notes-count' aria-live='polite'></span><span id='notes-state'>"
            + ("Last saved "+e(data.get('updated','')) if data.get('updated') else 'Not saved yet')
            + "</span><button type='submit'>Save notes</button></div></form>"
            "<script>(function(){var t=document.getElementById('general-notes'),dirty=false;"
            "function count(){document.getElementById('notes-count').textContent=Array.from(t.value).length.toLocaleString()+' / 10,000 characters';}"
            "t.addEventListener('input',function(){dirty=true;count();document.getElementById('notes-state').textContent='Unsaved changes';});count();"
            "t.form.addEventListener('submit',function(){dirty=false;});window.addEventListener('beforeunload',function(e){if(dirty){e.preventDefault();e.returnValue='';}});})();</script>")
