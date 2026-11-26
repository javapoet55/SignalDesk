"""Interactive journal analytics. All plotted values derive from closed journal trades."""
import datetime as dt
import json


def render(trades):
    rows = []
    for t in sorted((t for t in trades if t['status'] == 'closed'),
                    key=lambda t: (t['exit_time'], t['entry_time'], t['id'])):
        minutes = None
        if t.get('has_time'):
            try:
                minutes = max(0, (dt.datetime.fromisoformat(t['exit_time']) -
                                  dt.datetime.fromisoformat(t['entry_time'])).total_seconds() / 60)
            except (ValueError, TypeError):
                pass
        rows.append([t['exit_time'][:10], t['symbol'], t['gross'], t['pnl'], t['fees'],
                     t.get('return_pct'), minutes])
    data = json.dumps(rows, separators=(',', ':'), allow_nan=False).replace('<', '\u003c')
    tabs = ''.join(f"<button type='button' data-metric='{k}' aria-pressed='{'true' if k == 'pnl' else 'false'}'>{v}</button>"
                   for k, v in [('pnl','PnL'),('averages','Averages'),('performance','Performance'),
                               ('drawdown','Drawdown'),('trades','Trades'),('hold','Hold Time'),
                               ('fees','Commissions'),('advanced','Advanced')])
    return ("<section id='journal-analytics' aria-labelledby='analytics-title'><h2 id='analytics-title'>Performance explorer</h2>"
            f"<div class='ja-tabs' role='group' aria-label='Chart metric'>{tabs}</div>"
            "<div class='card ja-card'><div class='ja-head'><div><div class='ja-label'></div>"
            "<div class='ja-value money' aria-live='polite'></div></div>"
            "<div class='ja-style' role='group' aria-label='Chart style'><button type='button' data-style='line' aria-pressed='false'>Line</button>"
            "<button type='button' data-style='bar' aria-pressed='true'>Bar</button></div></div>"
            "<div class='ja-controls'><label>Group <select data-group aria-label='Chart grouping'>"
            "<option value='trade'>By trade</option><option value='day'>By day</option><option value='week'>By week</option>"
            "<option value='month'>By month</option></select></label><label>Measure <select data-measure aria-label='Chart measure'></select></label></div>"
            "<p class='ja-definition'></p><div class='ja-plot'><svg role='img' tabindex='0' aria-label='Journal performance chart. Use left and right arrow keys to inspect points.'></svg>"
            "<div class='ja-tooltip' hidden></div><p class='ja-empty' hidden>No closed trades match your filters yet.</p></div>"
            "<div class='ja-footer'><span class='ja-count'></span><span class='ja-detail' aria-live='polite'>Hover or tap a bar to inspect it.</span></div>"
            f"<script type='application/json' class='ja-data'>{data}</script></div></section>"
            '<style>' + CSS + '</style><script>' + JS + '</script>')


CSS = """
#journal-analytics{--ja-series:#7061ec;--ja-plot:color-mix(in srgb,var(--bg) 70%,var(--card));margin-top:30px}
:root[data-theme=dark] #journal-analytics{--ja-series:#8a7dff}
#journal-analytics .ja-tabs{display:flex;gap:4px;flex-wrap:wrap;margin:12px 0}
#journal-analytics button{background:transparent;color:var(--muted);font-size:12px;padding:7px 11px;border:1px solid transparent;border-radius:7px;font-weight:500}
#journal-analytics button[aria-pressed=true]{color:var(--ink);background:var(--card);border-color:var(--line)}
#journal-analytics .ja-tabs button[aria-pressed=true]{background:var(--jsoft);color:var(--accent)}
#journal-analytics .ja-card{margin-top:0;padding:18px 18px 12px;border-radius:17px}
#journal-analytics .ja-head{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:20px}
#journal-analytics .ja-label{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
#journal-analytics .ja-value{font-size:27px;font-weight:600;margin-top:3px;letter-spacing:-.6px}
#journal-analytics .ja-style{display:flex;background:var(--bg);padding:3px;border-radius:10px}
#journal-analytics .ja-controls{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:10px}
#journal-analytics label{display:flex;gap:7px;align-items:center;color:var(--muted);font-size:11px}
#journal-analytics select{font-size:12px;padding:5px 9px;background:var(--card);border-radius:6px;max-width:100%}
#journal-analytics .ja-definition{font-size:12px;color:var(--muted);margin:8px 0 12px}
#journal-analytics .ja-plot{position:relative;background:var(--ja-plot);border-radius:10px;overflow:hidden;min-width:0}
#journal-analytics svg{width:100%;height:360px;display:block;outline-offset:-3px}
#journal-analytics svg text{font:11px system-ui,sans-serif;fill:var(--muted)}
#journal-analytics .ja-tooltip{position:absolute;z-index:2;pointer-events:none;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:9px 12px;font-size:12px;max-width:230px;box-shadow:0 4px 16px #0002}
#journal-analytics .ja-empty{position:absolute;inset:35% 12px auto;text-align:center;color:var(--muted);font-size:14px}
#journal-analytics .ja-footer{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:11px;padding-top:12px}
@media(max-width:600px){#journal-analytics .ja-card{padding:13px}#journal-analytics svg{height:300px}
#journal-analytics .ja-tabs{gap:2px}#journal-analytics button{padding:7px 8px}#journal-analytics .ja-value{font-size:23px}
#journal-analytics .ja-controls label{flex-wrap:wrap}#journal-analytics select{font-size:16px;max-width:220px}}
"""


JS = r"""
(()=>{
const root=document.getElementById('journal-analytics');if(!root)return;
const q=s=>root.querySelector(s),rows=JSON.parse(q('.ja-data').textContent),svg=q('svg'),ns='http://www.w3.org/2000/svg';
const specs={
pnl:[['gross','PnL gross $','money','Cumulative gross P&L before fees.'],['net','PnL net $','money','Cumulative net P&L after fees.']],
averages:[['average','Average trade','money','Running average net P&L per closed trade.'],['avgwin','Average winner','money','Running average net P&L of winning trades.'],['avgloss','Average loser','money','Running average net P&L of losing trades.']],
performance:[['winrate','Win rate','percent','Running share of closed trades with positive net P&L; breakeven trades are included in the denominator.'],['return','Average return','percent','Running arithmetic average of individual trade returns; this is not an account return.']],
drawdown:[['drawdown','Drawdown $','money','Cumulative net P&L minus its previous peak, including the starting balance of zero.']],
trades:[['count','Closed trades','count','Cumulative count of closed trades.'],['wins','Winning trades','count','Cumulative count of winning trades.'],['losses','Losing trades','count','Cumulative count of losing trades.']],
hold:[['hold','Average hold time','minutes','Running average holding time in minutes. Trades without entry and exit times are excluded.']],
fees:[['fees','Commissions & fees','money','Cumulative recorded fees on closed trades.']],
advanced:[['factor','Profit factor','ratio','Cumulative winning net P&L divided by the absolute losing net P&L. Undefined until there is a losing trade.']]};
let metric='pnl',measure='gross',style='bar',group='trade',points=[],selected=-1,geometry;
function format(v,compact=false){if(v===null||!Number.isFinite(v))return '—';const unit=spec()[2];
let text=new Intl.NumberFormat('en-US',{minimumFractionDigits:unit==='money'&&!compact?2:0,maximumFractionDigits:unit==='count'?0:2,notation:compact?'compact':'standard'}).format(Math.abs(v));
return (v<0?'−':'')+(unit==='money'?'$':'')+text+(unit==='percent'?'%':unit==='minutes'?' min':unit==='ratio'?'×':'');}
function spec(){return specs[metric].find(s=>s[0]===measure)||specs[metric][0];}
function key(day,index){if(group==='trade')return String(index);if(group==='day')return day;if(group==='month')return day.slice(0,7);
const d=new Date(day+'T00:00:00Z');d.setUTCDate(d.getUTCDate()-((d.getUTCDay()+6)%7));return d.toISOString().slice(0,10);}
function compute(){let gross=0,net=0,fees=0,wins=0,losses=0,wp=0,lp=0,returns=0,rn=0,hold=0,hn=0,peak=0;points=[];
rows.forEach((r,i)=>{gross+=r[2];net+=r[3];fees+=r[4];if(r[3]>.005){wins++;wp+=r[3];}else if(r[3]<-.005){losses++;lp+=r[3];}
if(r[5]!==null){returns+=r[5];rn++;}if(r[6]!==null){hold+=r[6];hn++;}peak=Math.max(peak,net);
const values={gross,net,fees,count:i+1,wins,losses,average:net/(i+1),avgwin:wins?wp/wins:null,avgloss:losses?lp/losses:null,
winrate:wins/(i+1)*100,return:rn?returns/rn:null,hold:hn?hold/hn:null,drawdown:net-peak,factor:losses?wp/(-lp):null};
const k=key(r[0],i),p={key:k,date:r[0],symbol:r[1],value:values[measure],count:i+1};
if(points.length&&points[points.length-1].key===k)points[points.length-1]=p;else points.push(p);});}
function el(tag,attrs,text){const n=document.createElementNS(ns,tag);Object.entries(attrs||{}).forEach(([k,v])=>n.setAttribute(k,v));if(text!==undefined)n.textContent=text;svg.appendChild(n);return n;}
function dateLabel(p){if(group==='trade'||group==='day')return new Date(p.date+'T00:00:00Z').toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'});return p.key;}
function draw(){const width=Math.max(240,Math.floor(q('.ja-plot').getBoundingClientRect().width)),height=width<600?300:360;
svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.replaceChildren();const left=12,right=width<600?65:78,top=22,bottom=38,w=width-left-right,h=height-top-bottom;
const finite=points.map(p=>p.value).filter(v=>v!==null&&Number.isFinite(v));let low=finite.reduce((a,b)=>Math.min(a,b),0),high=finite.reduce((a,b)=>Math.max(a,b),0);
if(high===low){high+=1;low-=1;}const pad=(high-low)*.1;high+=pad;low-=pad;
const y=v=>top+(high-v)/(high-low)*h,x=i=>left+(i+.5)/Math.max(1,points.length)*w;geometry={left,w,x,y,top,h};
const defs=el('defs'),gradient=document.createElementNS(ns,'linearGradient');gradient.id='journal-bar-fill';gradient.setAttribute('x2','0');gradient.setAttribute('y2','1');
for(const [offset,opacity] of [['0','1'],['1','.4']]){const stop=document.createElementNS(ns,'stop');stop.setAttribute('offset',offset);stop.setAttribute('stop-color','var(--ja-series)');stop.setAttribute('stop-opacity',opacity);gradient.appendChild(stop);}defs.appendChild(gradient);
for(let i=0;i<=4;i++){const v=low+(high-low)*i/4,Y=y(v);el('line',{x1:left,x2:left+w,y1:Y,y2:Y,stroke:'var(--line)','stroke-dasharray':'3 4'});el('text',{x:width-8,y:Y+4,'text-anchor':'end'},format(v,true));}
el('line',{x1:left,x2:left+w,y1:y(0),y2:y(0),stroke:'var(--muted)','stroke-opacity':'.65'});
q('.ja-empty').hidden=finite.length>0;q('.ja-empty').textContent=rows.length?'No values available for this measure.':'No closed trades match your filters yet.';
if(style==='bar'){const max=Math.max(1,Math.floor(w/3)),step=Math.max(1,Math.ceil(points.length/max));for(let i=0;i<points.length;i+=step){const end=Math.min(i+step,points.length)-1,p=points[end];if(p.value===null)continue;
const slot=w/points.length,bw=Math.max(.5,slot*(end-i+1)-1);el('rect',{x:left+i*slot+.5,y:Math.min(y(0),y(p.value)),width:bw,height:Math.max(.5,Math.abs(y(p.value)-y(0))),rx:Math.min(2,bw/3),fill:'url(#journal-bar-fill)'});}}
else{let path='',active=false;points.forEach((p,i)=>{if(p.value===null){active=false;return;}path+=(active?'L':'M')+x(i)+','+y(p.value);active=true;});el('path',{d:path,fill:'none',stroke:'var(--ja-series)','stroke-width':2.3,'stroke-linejoin':'round'});if(points.length===1&&points[0].value!==null)el('circle',{cx:x(0),cy:y(points[0].value),r:4,fill:'var(--ja-series)'});}
const ticks=Math.min(points.length,width<600?2:4);for(let i=0;i<ticks;i++){const index=ticks===1?0:Math.round(i*(points.length-1)/(ticks-1));el('text',{x:i===0?left:i===ticks-1?left+w:x(index),y:height-12,'text-anchor':i===0?'start':i===ticks-1?'end':'middle'},dateLabel(points[index]));}
if(selected>=0)inspect(selected,false);}
function inspect(i,tooltip=true){if(!points.length)return;selected=Math.max(0,Math.min(points.length-1,i));const p=points[selected];
const text=`${group==='trade'?'Trade '+p.count+' · '+p.symbol+' · ':''}${p.date} · ${spec()[1]}: ${format(p.value)}`;q('.ja-detail').textContent=text;
svg.querySelectorAll('[data-guide]').forEach(n=>n.remove());const X=geometry.x(selected);el('line',{'data-guide':'1',x1:X,x2:X,y1:geometry.top,y2:geometry.top+geometry.h,stroke:'var(--muted)','stroke-dasharray':'3 3'});
if(p.value!==null)el('circle',{'data-guide':'1',cx:X,cy:geometry.y(p.value),r:4,fill:'var(--ja-series)',stroke:'var(--card)','stroke-width':2});
const tip=q('.ja-tooltip');tip.textContent=text;tip.hidden=!tooltip;tip.style.left=Math.max(5,Math.min(X-100,svg.clientWidth-240))+'px';tip.style.top='12px';}
function update(){selected=-1;q('.ja-tooltip').hidden=true;compute();q('.ja-label').textContent=spec()[1];q('.ja-value').textContent=format(points.length?points[points.length-1].value:null);
q('.ja-definition').textContent=spec()[3];q('.ja-count').textContent=`${rows.length.toLocaleString()} closed trades · ${points.length.toLocaleString()} ${group==='trade'?'trade':group} points${group==='trade'?' · dense bars are grouped visually':''}`;
q('.ja-detail').textContent='Hover or tap to inspect · arrow keys when chart is focused';draw();}
function measures(){q('[data-measure]').replaceChildren();specs[metric].forEach(s=>{const option=document.createElement('option');option.value=s[0];option.textContent=s[1];q('[data-measure]').appendChild(option);});measure=specs[metric][0][0];}
root.querySelectorAll('[data-metric]').forEach(b=>b.onclick=()=>{metric=b.dataset.metric;root.querySelectorAll('[data-metric]').forEach(n=>n.setAttribute('aria-pressed',String(n===b)));measures();update();});
root.querySelectorAll('[data-style]').forEach(b=>b.onclick=()=>{style=b.dataset.style;root.querySelectorAll('[data-style]').forEach(n=>n.setAttribute('aria-pressed',String(n===b)));update();});
q('[data-group]').onchange=e=>{group=e.target.value;update();};q('[data-measure]').onchange=e=>{measure=e.target.value;update();};
function pointer(e){const box=svg.getBoundingClientRect(),i=Math.floor((e.clientX-box.left-geometry.left)/geometry.w*points.length);inspect(i);}
svg.addEventListener('pointermove',pointer);svg.addEventListener('click',pointer);svg.addEventListener('pointerleave',()=>{q('.ja-tooltip').hidden=true;});
svg.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();inspect(selected<0?0:selected+(e.key==='ArrowRight'?1:-1));}});
new ResizeObserver(draw).observe(q('.ja-plot'));measures();update();
})();
"""
