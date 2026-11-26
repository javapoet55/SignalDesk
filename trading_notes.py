"""Private persistent ticker-linked trading notes."""
import datetime as dt
import json
import math
import os
import re
import secrets
import threading
from pathlib import Path

class NoteError(ValueError):
    pass

class NoteStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / 'notes.json'
        self.lock = threading.RLock()

    def list(self):
        with self.lock:
            if not self.path.exists():
                return []
            try:
                data = json.loads(self.path.read_text())
                if not isinstance(data, list) or any(not isinstance(n, dict) for n in data):
                    raise ValueError()
                return sorted(data, key=lambda n: n.get('updated', ''), reverse=True)
            except (OSError, ValueError) as exc:
                raise NoteError('Trading notes could not be read. Existing data has been preserved.') from exc

    def save(self, form):
        with self.lock:
            notes = self.list()
            ident = form.get('id', '')
            old = next((n for n in notes if n['id'] == ident), None)
            if ident and old is None:
                raise NoteError('This note no longer exists. Reload the page.')
            if old and form.get('revision', '') != old.get('updated', ''):
                raise NoteError('This note changed in another tab. Reload it before saving.')
            n = {}
            for key, limit in {'symbol':10,'company':120,'title':180,'status':20,'date':10,'tags':200,'source':180,'thesis':4000,'trigger':2000,'invalidation':2000,'reflection':4000}.items():
                val = str(form.get(key, '')).strip()
                if len(val) > limit:
                    raise NoteError(f'{key.title()} exceeds {limit} characters.')
                n[key] = val
            n['symbol'] = n['symbol'].upper()
            if n['symbol'] and not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,9}', n['symbol']):
                raise NoteError('Enter a ticker such as AAPL or BRK.B.')
            if not any(n.get(k) for k in ('symbol','title','thesis','trigger','invalidation','reflection','source')):
                raise NoteError('Write a note or enter a ticker or title before saving.')
            if not n['title']:
                text = next((n[k] for k in ('thesis','trigger','reflection','source') if n.get(k)), '')
                n['title'] = text.splitlines()[0][:180] if text else (n['symbol'] + ' trading note' if n['symbol'] else 'Trading note')
            if n['status'] not in ('watching', 'planned', 'review'):
                raise NoteError('Choose a valid note status.')
            try:
                dt.date.fromisoformat(n['date'])
            except ValueError:
                raise NoteError('Choose a valid note date.')
            for key in ('entry','stop','target'):
                value = str(form.get(key, '')).strip()
                try:
                    number = float(value) if value else None
                    if number is not None and (not math.isfinite(number) or number <= 0):
                        raise ValueError()
                except ValueError:
                    raise NoteError('Trade-plan prices must be positive numbers or empty.')
                n[key] = value
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds')
            n.update(id=ident or secrets.token_hex(12), created=old['created'] if old else now, updated=now)
            if old:
                notes = [n if row['id'] == ident else row for row in notes]
            else:
                notes.append(n)
            temp = self.path.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(notes, f, ensure_ascii=True, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
            return n['id']


TEMPLATE = '<div class="notes-title"><div><div class="eyebrow">YOUR EDGE, WRITTEN DOWN</div><h1>Trading notes</h1><p class="muted">Capture the setup. Define the risk. Review the decision.</p></div><a class="btnlink" href="/notes?new=1">＋ New note</a></div>\n<div class="preview-label">Private notes · Saved to your persistent storage</div>\n<div class="notes-layout"><aside class="card note-library"><div class="library-head"><h2>My notebook <span class="muted" id="note-count">0</span></h2><label class="search-label"><span class="sr-only">Search notes</span><input id="note-search" placeholder="Search ticker, title, or tag…"></label><div class="note-filters" style="flex-wrap:wrap"><button class="filter active" data-filter="all">All notes</button><button class="filter" data-filter="watching">Watching</button><button class="filter" data-filter="planned">Planned</button><button class="filter" data-filter="review">Review</button></div></div><div id="note-list"></div><div class="library-footer">Private to your account <span>◎</span></div></aside>\n<section class="card note-editor"><div class="editor-toolbar"><div class="note-symbol"><label>Ticker<input id="symbol" maxlength="10" placeholder="AAPL" required></label><label>Company (optional)<input id="company" maxlength="120" placeholder="Company name"></label></div><span class="saved" id="save-state">New note</span></div>\n<div class="editor-top"><div class="note-meta"><label>Status<select id="status"><option value="watching">Watching</option><option value="planned">Planned</option><option value="review">Review</option></select></label><label>Note date<input id="note-date" type="date" ></label></div><div class="source-links"><a id="finviz-link" href="#" target="_blank" rel="noopener">Finviz ↗</a><a id="tv-link" href="#" target="_blank" rel="noopener">TradingView ↗</a></div></div>\n<label class="sr-only" for="note-title">Note title</label><input id="note-title" class="note-heading" placeholder="Give this trading idea a title" maxlength="180">\n<label class="section-label" for="tags">Tags</label><input id="tags" maxlength="200" placeholder="FMFI, Breakout, Swing trade"><div style="height:18px"></div>\n<label class="section-label" for="source">Scan / strategy context (optional)</label><input id="source" maxlength="180" placeholder="e.g. FMFI · Market Top Gainers"><div style="height:4px"></div>\n<div class="plan-heading"><h2>Trade plan</h2><span class="muted">Plan before you enter</span></div>\n<div class="trade-plan"><label>Entry price ($)<input type="number" id="entry"  step="0.01"></label><label>Stop loss ($)<input type="number" id="stop"  step="0.01"></label><label>Target ($)<input type="number" id="target"  step="0.01"></label><div class="reward"><span class="muted">Reward / risk · Long</span><strong id="rr">—</strong></div></div>\n<label class="section-label" for="thesis">Setup &amp; thesis</label><textarea id="thesis" rows="4"></textarea>\n<div class="two-notes"><div><label class="section-label" for="trigger">Entry trigger</label><textarea id="trigger" rows="3"></textarea></div><div><label class="section-label" for="invalidation">What invalidates this idea?</label><textarea id="invalidation" rows="3"></textarea></div></div>\n<details class="review-section"><summary>Post-trade reflection <span class="muted">What worked? What would you change?</span></summary><label class="sr-only" for="reflection">Post-trade reflection</label><textarea id="reflection" rows="4" placeholder="Review your execution, discipline, and lessons…"></textarea></details>\n<div class="editor-footer"><span id="feedback" class="muted" aria-live="polite">Enter a ticker and title to create your first note.</span><button id="save-note">Save note</button></div>\n</section></div>\n<style>\n.notes-title{display:flex;justify-content:space-between;align-items:center;gap:20px;margin:28px 0 12px}.notes-title h1{font-size:34px;letter-spacing:-1px;margin:5px 0}.notes-title p{margin:0;font-size:14px}.eyebrow{color:var(--accent);font-size:10px;font-weight:600;letter-spacing:1.7px}.preview-label{color:var(--muted);font-size:12px;margin-bottom:22px}.notes-layout{display:grid;grid-template-columns:300px minmax(0,1fr);gap:20px;align-items:start}.journal-app .note-library{padding:0;overflow:hidden;margin:0}.library-head{padding:20px 18px 14px}.library-head h2{margin:0 0 16px;font-size:15px;display:flex;justify-content:space-between}.notes-layout input,.notes-layout textarea,.notes-layout select{width:100%;font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;min-width:0;max-width:100%;box-sizing:border-box}.notes-layout textarea{resize:vertical;line-height:1.7;font-size:13px}.notes-layout input:focus,.notes-layout textarea:focus{outline:2px solid var(--accent);outline-offset:2px}.search-label input{font-size:12px}.note-filters{display:flex;gap:5px;margin-top:14px}.note-filters button{background:transparent;color:var(--muted);font-size:11px;padding:6px 9px;font-weight:500}.note-filters .active{background:var(--jsoft);color:var(--accent)}.note-item{display:block;width:100%;text-align:left;background:transparent;color:var(--ink);border-radius:0;border-top:1px solid var(--line);padding:18px;font-weight:400;border-left:3px solid transparent}.note-item.active{background:var(--jsoft);border-left-color:var(--accent)}.note-item .top{display:flex;justify-content:space-between;align-items:center;gap:8px}.note-item strong{font-size:13px}.note-item .state{font-size:10px;color:var(--accent)}.note-item .title{display:block;line-height:1.55;margin:9px 0;font-size:13px}.note-item .bottom{color:var(--muted);font-size:11px;display:flex;justify-content:space-between}.library-footer{padding:18px;color:var(--muted);font-size:11px;border-top:1px solid var(--line);display:flex;justify-content:space-between}.journal-app .note-editor{margin:0;padding:24px 26px}.editor-toolbar{display:flex;justify-content:space-between;gap:14px;align-items:center;padding-bottom:20px;border-bottom:1px solid var(--line)}.note-symbol{display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:12px}.note-symbol #symbol-badge{font-size:16px;font-weight:700;color:var(--accent);background:var(--jsoft);padding:8px 12px;border-radius:8px}.saved{font-size:11px;color:var(--muted);white-space:nowrap}.editor-top{display:flex;justify-content:space-between;gap:14px;align-items:end;flex-wrap:wrap;margin:20px 0}.note-meta{display:flex;gap:14px;flex-wrap:wrap}.note-meta label,.trade-plan label{display:flex;flex-direction:column;gap:7px;font-size:11px;color:var(--muted)}.note-meta input,.note-meta select{font-size:12px;padding:7px 9px}.source-links{display:flex;gap:16px;font-size:12px;padding-bottom:7px}.notes-layout input.note-heading{font-size:23px;font-weight:600;letter-spacing:-.5px;line-height:1.4;border:0;padding:0;background:transparent;border-radius:0}.tags{display:flex;gap:7px;margin:14px 0 22px;flex-wrap:wrap}.tags span{font-size:10px;color:var(--muted);padding:4px 8px;border:1px solid var(--line);border-radius:5px}.snapshot{display:flex;gap:20px;justify-content:space-between;padding:15px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.snapshot div{display:flex;flex-direction:column;gap:6px}.snapshot span{font-size:10px}.snapshot strong{font-size:11px;font-weight:500}.plan-heading{display:flex;justify-content:space-between;gap:10px;align-items:center;margin:24px 0 12px}.plan-heading h2{margin:0;font-size:15px}.plan-heading span{font-size:11px}.trade-plan{display:grid;grid-template-columns:repeat(3,minmax(0,1fr)) 1.1fr;gap:12px;margin-bottom:25px}.trade-plan input{font-size:13px}.reward{display:flex;flex-direction:column;justify-content:center;gap:5px;padding-left:10px;border-left:1px solid var(--line)}.reward span{font-size:10px}.reward strong{font-size:20px;color:var(--accent)}.section-label{display:block;font-size:13px;font-weight:600;margin:0 0 10px}.two-notes{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:22px}.review-section{border-top:1px solid var(--line);margin-top:24px;padding-top:18px}.review-section summary{font-size:13px;color:var(--ink);font-weight:600;cursor:pointer}.review-section summary span{display:block;font-weight:400;font-size:11px;margin:7px 0 0 15px}.review-section textarea{margin-top:14px}.editor-footer{display:flex;gap:14px;justify-content:space-between;align-items:center;margin-top:24px;padding-top:20px;border-top:1px solid var(--line)}.editor-footer span{font-size:11px}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}\n@media(max-width:900px){.notes-layout{grid-template-columns:240px minmax(0,1fr)}.trade-plan{grid-template-columns:repeat(2,minmax(0,1fr))}.snapshot{flex-wrap:wrap}.notes-layout input.note-heading{font-size:20px}}\n@media(max-width:700px){.notes-layout{grid-template-columns:1fr}.notes-title{align-items:start}.notes-title h1{font-size:28px}.notes-title button{white-space:nowrap;font-size:12px}.journal-app .note-editor{padding:18px}.two-notes{grid-template-columns:1fr}.editor-footer{flex-wrap:wrap}.note-library .library-footer{display:none}.notes-layout input,.notes-layout textarea{font-size:16px}.snapshot{gap:14px}.editor-top{gap:20px}}\n\n.note-symbol label{display:flex;flex-direction:column;gap:6px;font-size:11px;color:var(--muted)}\n.note-symbol #symbol{width:110px;color:var(--accent);font-weight:600}\n.note-symbol #company{width:210px}.notes-layout .note-heading{margin-bottom:18px}\n.note-meta{min-width:0}.note-meta label{max-width:100%}.notes-layout textarea{overflow-wrap:anywhere}\n@media(max-width:700px){.notes-title .btnlink{white-space:nowrap}.note-symbol #company{width:100%}.note-symbol label{max-width:100%}.note-symbol{width:100%}.editor-toolbar{flex-wrap:wrap}}\n</style>\n'

TEMPLATE = TEMPLATE.replace('Ticker<input', 'Ticker (optional)<input').replace('placeholder="AAPL" required','placeholder="e.g. AAPL"').replace('Give this trading idea a title','Title (optional — generated from your note)').replace('Enter a ticker and title to create your first note.','Write your note, then save. Ticker and title are optional.')
TEMPLATE += "<style>.notes-title .btnlink{background:var(--btn);color:var(--btnink);font-weight:600;text-decoration:none;display:inline-block}</style>"

JS = r"""
(function(){const root=document.getElementById('trading-notes'),q=id=>root.querySelector('#'+id),data=JSON.parse(q('notes-data').textContent),notes=data.notes;let selected=data.selected,filter='all',dirty=false;
const ids={title:'note-title',date:'note-date'},fields=['symbol','company','title','status','date','tags','source','entry','stop','target','thesis','trigger','invalidation','reflection'];
const field=k=>q(ids[k]||k);let revision='';
function esc(s){const n=document.createElement('span');n.textContent=s;return n.innerHTML}
function list(){const search=q('note-search').value.toLowerCase();let count=0;q('note-list').innerHTML=notes.map((n,i)=>{if((filter!=='all'&&filter!==n.status)||!`${n.symbol} ${n.title} ${n.tags} ${n.source}`.toLowerCase().includes(search))return '';count++;return `<button type="button" class="note-item ${selected===i?'active':''}" data-note="${i}"><span class="top"><strong>${esc(n.symbol||'General note')}</strong><span class="state">${esc(n.status)}</span></span><span class="title">${esc(n.title)}</span><span class="bottom"><span>${esc(n.date)}</span><span>${esc(n.tags||'Trading note')}</span></span></button>`}).join('')||'<p class="muted" style="padding:18px">No matching notes. Create a note to start your notebook.</p>';q('note-count').textContent=count;root.querySelectorAll('[data-note]').forEach(b=>b.onclick=()=>{if(dirty&&!confirm('Discard unsaved changes and open this note?'))return;load(+b.dataset.note)})}
function links(){const symbol=field('symbol').value.trim().toUpperCase();const valid=/^[A-Z0-9][A-Z0-9.\-]{0,9}$/.test(symbol);for(const [id,base] of [['finviz-link','https://finviz.com/quote.ashx?t='],['tv-link','https://www.tradingview.com/chart/?symbol=']]){q(id).href=valid?base+encodeURIComponent(symbol):'#';q(id).setAttribute('aria-disabled',String(!valid))}}
function rr(){let e=+field('entry').value,s=+field('stop').value,t=+field('target').value;q('rr').textContent=e>s&&s>0&&t>e?((t-e)/(e-s)).toFixed(1)+' : 1':'—'}
function load(i){selected=i;const n=notes[i]||data.draft;fields.forEach(k=>field(k).value=n[k]??'');revision=n.updated||'';dirty=false;q('save-state').textContent=n.id?'Saved note':'New note';q('feedback').textContent=n.id?'Last saved '+new Date(n.updated).toLocaleString():'Write your note, then save. Ticker and title are optional.';if(data.error){q('feedback').textContent=data.error;q('feedback').setAttribute('role','alert');}links();rr();list()}
fields.forEach(k=>field(k).addEventListener('input',()=>{dirty=true;q('save-state').textContent='Unsaved changes';if(k==='symbol')links();if(['entry','stop','target'].includes(k))rr()}));
root.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;root.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));list()});q('note-search').oninput=list;
q('save-note').onclick=()=>{const symbol=field('symbol').value.trim().toUpperCase();
if(symbol&&!/^[A-Z0-9][A-Z0-9.\-]{0,9}$/.test(symbol)){q('feedback').textContent='Enter a valid ticker, or leave Ticker empty for a general note.';q('feedback').setAttribute('role','alert');field('symbol').focus();return;}
if(!['symbol','title','thesis','trigger','invalidation','reflection','source'].some(k=>field(k).value.trim())){q('feedback').textContent='Write a note or enter a ticker or title before saving.';q('feedback').setAttribute('role','alert');field('thesis').focus();return;}
const form=document.createElement('form');form.method='post';form.action='/notes/save';const values={id:notes[selected]?.id||'',revision};fields.forEach(k=>values[k]=field(k).value);for(const [k,v] of Object.entries(values)){const input=document.createElement('input');input.type='hidden';input.name=k;input.value=v;form.appendChild(input)}q('feedback').textContent='Saving…';q('save-note').disabled=true;dirty=false;document.body.appendChild(form);HTMLFormElement.prototype.submit.call(form)};
root.querySelectorAll('.source-links a').forEach(a=>a.onclick=e=>{if(a.getAttribute('aria-disabled')==='true')e.preventDefault()});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue=''}});load(selected);
})();
"""

def render_content(notes, query, today, submitted=None, error=''):
    import html
    draft = {k:'' for k in ('symbol','company','title','tags','source','entry','stop','target','thesis','trigger','invalidation','reflection')}
    draft.update(status='watching', date=today)
    symbol = query.get('symbol','').strip().upper()
    if re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,9}', symbol):
        draft.update(symbol=symbol, title=f'{symbol} trading plan', source=query.get('source','')[:180])
    selected = next((i for i,n in enumerate(notes) if n['id']==query.get('id')), -1)
    if selected < 0 and notes and not query.get('new') and not symbol:
        selected = 0
    if submitted is not None:
        draft.update(submitted)
        selected = -1
        # Preserve the original ID/revision so retrying validation edits the same note.
        draft['updated'] = submitted.get('revision','')
        notes = notes + [draft]
        selected = len(notes)-1
    payload = json.dumps(dict(notes=notes, selected=selected, draft=draft, error=error), allow_nan=False).replace('<','\\u003c')
    notice = '<div class="warn" role="alert">'+html.escape(error)+'</div>' if error else ''
    return '<div id="trading-notes">'+notice+TEMPLATE+'<script id="notes-data" type="application/json">'+payload+'</script></div><script>'+JS+'</script>'
