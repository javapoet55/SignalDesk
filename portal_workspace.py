"""Shared authenticated portal navigation and strategy workspace presentation."""
from html import escape

import strategy_design as design

SECTIONS = (
    ("configure", "Configure & Run"), ("scheduled", "Run Activity"),
    ("history", "Run History"), ("backtests", "Backtest History"),
    ("versions", "Version History"), ("settings", "Settings"),
)
ICONS = {
    "scans": "M3 5h18l-7 8v6l-4 2v-8z",
    "strategies": "m3 8 9-5 9 5-9 5z M3 12l9 5 9-5 M3 16l9 5 9-5",
    "journal": "M5 3h14v18H5z M8 8h8 M8 12h8 M8 16h5",
    "report": "M4 20V4 M4 20h17 M8 16v-5 M13 16V6 M18 16v-8",
    "paper": "M3 8h18v12H3z M8 8V4h8v4 M3 12l9 3 9-3",
    "notes": "M4 4h14v6 M4 4v16h8 M10 10H7 M15 19l-3 1 1-4 6-6 3 3z",
    "tradingview": "M3 18l6-7 4 3 8-10 M15 4h6v6",
    "notebook": "M6 3h13v18H6z M3 7h5 M3 12h5 M3 17h5",
}


WORKSPACE_TABS = (
    ("backtests", "Strategy Library", "configure"),
    ("configure", "Run Configuration", "configure"),
    ("calendar", "Scheduled Runs", "scheduled"),
    ("scheduled", "Run History", "history"),
    ("history", "VectorBT PRO", "/vectorbt"),
    ("settings", "Settings", "settings"),
)


def strategy_navigation(portal_tabs, active):
    nav = [
        ("dashboard", "/", "Dashboard"),
        *portal_tabs,
        ("backtests", "/vectorbt", "VectorBT PRO"),
    ]

    def navlink(key, url, label):
        glyph = (
            "m2 10 10-8 10 8v11h-7v-7H9v7H2Z"
            if key == "dashboard"
            else ICONS.get(key)
        )
        image = (
            f"<svg class='sd-icon' aria-hidden='true' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.6'><path d='{glyph}'/></svg>"
            if glyph
            else design.icon("backtests")
        )
        return (
            f"<a href='{url}'"
            + (" aria-current='page'" if key == "strategies" else "")
            + f">{image}{escape('Latest Report' if key == 'report' else label)}</a>"
        )

    side = "".join(navlink(*item) for item in nav)
    tabs = "".join(
        f"<a href='{key if key.startswith('/') else '/strategies?tab=' + key}'"
        + (f" data-workspace-tab='{key}'" if not key.startswith("/") else "")
        + (" data-library='true'" if label == "Strategy Library" else "")
        + (" aria-current='page'" if key == active else "")
        + f">{design.icon(sym)}{label}</a>"
        for sym, label, key in WORKSPACE_TABS
    )
    return side, tabs


def subnav(current="configure"):
    items = []
    for key, title in SECTIONS:
        url = "/strategies/backtests" if key == "backtests" else "/strategies?tab=" + key
        if key == "versions":
            url = "/strategies/archive"
        active = " aria-current='page'" if current == key else ""
        items.append(f"<a href='{url}' data-workspace-tab='{key}'{active}>{design.icon(key)}{escape(title)}</a>")
    return "<nav class='studio-tabs' aria-label='Strategy workspace'>" + "".join(items) + "</nav>"


def section_links(module, sections):
    links = "".join(f"<a href='#{anchor}'>{escape(label)}</a>" for anchor, label in sections)
    return f"<nav class='studio-tabs module-sections' aria-label='{escape(module)} sections'>{links}</nav>"



def shell(tabs, active, controls):
    if active == "strategies":
        return "<style>" + CSS + "</style>" + design.header(tabs, controls)
    links = []
    title = "Account"
    for key, url, label in tabs:
        selected = key == active
        if selected:
            title = label
        icon = f"<svg viewBox='0 0 24 24' aria-hidden='true'><path d='{ICONS[key]}'/></svg>"
        links.append(f"<a href='{url}'" + (" aria-current='page'" if selected else "")
                     + f">{icon}{escape(label)}</a>")
    links.insert(2, "<a href='/vectorbt'>" + design.icon("backtests") + "VectorBT PRO</a>")
    return ("<style>" + CSS + "</style><aside class='studio-rail' id='studio-rail'>"
            "<a href='/' class='studio-brand'><i aria-hidden='true'><span></span><span></span><span></span></i>Finviz Agent</a>"
            "<nav aria-label='Portal modules'>" + "".join(links) + "</nav></aside>"
            "<header class='studio-bar'><button type='button' class='studio-menu' aria-controls='studio-rail' "
            "aria-expanded='false'>☰ Menu</button><strong>" + escape(title) + "</strong>" + controls
            + "</header><script>document.querySelector('.studio-menu').onclick=function(){"
            "const open=document.body.classList.toggle('studio-open');this.setAttribute('aria-expanded',open);"
            "};document.addEventListener('DOMContentLoaded',function(){const links=document.querySelectorAll('.module-sections a');"
            "function mark(){links.forEach((a,i)=>{if(location.hash?a.hash===location.hash:i===0)a.setAttribute('aria-current','location');"
            "else a.removeAttribute('aria-current')})}mark();addEventListener('hashchange',mark)});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('studio-open'))"
            "{document.querySelector('.studio-menu').click();document.querySelector('.studio-menu').focus()}});</script>")


CSS = """
body:has(.studio-rail){--rail:204px;--sbg:#f4f7f7;--surface:#fff;--stroke:#cfdddf;--stext:#142c33;
--sub:#4a626a;--tint:#d9f2e8;--signal:#12664e;margin:0;padding-left:var(--rail);box-sizing:border-box;
background:var(--sbg);color:var(--stext);font-family:system-ui,-apple-system,sans-serif}
html[data-theme=dark] body:has(.studio-rail){--sbg:#0c161d;--surface:#13232c;--stroke:#30434e;
--stext:#e7f0f4;--sub:#a8bcc6;--tint:#183c35;--signal:#80e0c4}
body:has(.studio-rail) main{width:100%;max-width:1560px;box-sizing:border-box;margin:auto;padding:18px clamp(14px,2.2vw,38px) 60px;min-width:0}
.studio-rail{box-sizing:border-box;position:fixed;inset:0 auto 0 0;width:var(--rail);background:var(--surface);border-right:1px solid var(--stroke);z-index:40;padding:0 12px;overflow:auto;color:var(--stext)}
.studio-brand,.studio-rail nav a{display:flex;align-items:center;text-decoration:none;color:var(--stext);gap:12px}
.studio-brand{font-size:20px;letter-spacing:-.7px;font-weight:750;border-bottom:1px solid var(--stroke);padding:19px 6px;margin-bottom:20px}
.studio-brand i{display:flex;align-items:end;gap:3px;height:23px}.studio-brand i span{width:4px;height:40%;background:var(--signal);border-radius:2px}.studio-brand i span:nth-child(2){height:70%}.studio-brand i span:last-child{height:100%}
.studio-rail nav{display:grid;gap:5px;margin:0;padding:0;border:0}.studio-rail nav a{font-size:14px;padding:11px 12px;border-radius:8px;font-weight:550}
.studio-rail nav a:hover,.studio-rail nav a[aria-current]{color:var(--signal);background:var(--tint)}
.studio-rail svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round;stroke-width:1.65}
.studio-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:43px;border-bottom:1px solid var(--stroke);padding-bottom:12px;margin:0 0 18px}
.studio-bar .account-controls,.studio-bar .theme-switch{display:flex;align-items:center;gap:6px}.studio-bar form{margin:0}
.studio-bar button{background:var(--surface);color:var(--stext);border:1px solid var(--stroke);border-radius:6px;padding:6px 9px;font:600 12px system-ui;cursor:pointer}
.studio-bar button[aria-pressed=true]{background:var(--tint);color:var(--signal)}.studio-menu{display:none}
.studio-tabs{display:flex;align-items:stretch;gap:6px;border-bottom:1px solid var(--stroke);margin:0 0 22px;padding:0;overflow-x:auto;max-width:100%}
.studio-tabs a{white-space:nowrap;color:var(--sub);font:600 14px system-ui;padding:12px 10px;border-bottom:3px solid transparent;text-decoration:none}
.studio-tabs a[aria-current],.studio-tabs a:hover{border-color:var(--signal);color:var(--signal)}
.studio-rail a:focus-visible,.studio-tabs a:focus-visible,body:has(.studio-rail) button:focus-visible{outline:2px solid var(--signal);outline-offset:-2px}
body:has(.studio-rail) .card{border-radius:11px}.studio-stats{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}
.studio-stats div{background:var(--surface);border:1px solid var(--stroke);border-radius:10px;padding:14px 18px;flex:1;min-width:130px;color:var(--sub);font-size:13px}
.studio-stats strong{display:block;color:var(--stext);font-size:24px}.studio-actions{margin:14px 0;display:flex;align-items:center;flex-wrap:wrap;gap:12px}
.studio-actions a,.studio-link{font-weight:650;color:var(--signal)}.studio-search{display:grid;gap:5px;font-size:13px;margin:12px 0;max-width:420px}
.studio-page [hidden]{display:none!important}.studio-results{max-width:100%;overflow:auto}.studio-page .srow{flex-wrap:wrap}.studio-page .srow .nm{min-width:145px}
.studio-page #go{min-width:0}.studio-page[data-view=configure] #go{display:grid;grid-template-columns:minmax(0,2fr) minmax(240px,1fr);gap:18px;align-items:start}
.studio-page[data-view=configure] .selbar{position:sticky;top:12px}.studio-page .selbar{background:var(--surface);border:1px solid var(--stroke);border-radius:10px;padding:18px;margin:0}
.studio-page .top2{margin:0;min-width:0}.studio-page .top2 select{max-width:100%}.studio-search input{background:var(--sbg);color:var(--stext);border:1px solid var(--stroke);border-radius:7px;padding:8px 10px;min-width:0;max-width:100%}.studio-page .selbar .when{flex-wrap:wrap}
.studio-page .selbar h2,.studio-page .top2 h2{font-size:18px;margin:0 0 12px}.studio-page .top2 h2{grid-column:1/-1}
.studio-page .srow small{display:block;color:var(--sub)}.studio-preview{font-size:13px;line-height:1.65;color:var(--sub);overflow-wrap:anywhere}
.studio-detail{width:min(850px,92vw);border:1px solid var(--stroke);border-radius:10px;background:var(--surface);color:var(--stext);padding:18px}.studio-detail::backdrop{background:#07141bb3}
body:has(.studio-rail) .wrap,body:has(.studio-rail) .card{max-width:100%;box-sizing:border-box}
@media(max-width:1050px){body:has(.studio-rail){--rail:172px}.studio-page[data-view=configure] #go{grid-template-columns:1fr}.studio-page .srow{gap:7px}}
@media(max-width:760px){body:has(.studio-rail){padding-left:0}.studio-menu{display:block}.studio-rail{display:none;inset:57px auto 0 0;box-shadow:8px 12px 22px #0003}.studio-open .studio-rail{display:block}
.studio-bar{position:sticky;top:0;background:var(--sbg);padding:6px 0;z-index:41;min-height:44px}.studio-bar .welcome{display:none}.studio-bar strong{margin-right:auto}
body:has(.studio-rail) main{padding:0 12px 40px}.studio-stats div{min-width:80px;padding:10px}.studio-page .srow .nm{flex:1 1 65%}.studio-page .card{overflow:auto}.studio-tabs{gap:0}
.studio-page .top2{display:flex;flex-direction:column;align-items:stretch}.studio-page fieldset{min-width:0}.studio-bar .account-controls{gap:3px}}
.studio-detail dl{display:grid;grid-template-columns:auto 1fr;gap:10px 20px}.studio-detail dt{color:var(--sub)}.studio-detail dd{margin:0}
.schedule-editor{margin:0 0 0 auto;height:100%;max-height:100%;width:min(430px,95vw);box-sizing:border-box}.drawer-close{float:right}.schedule-editor .selbar{clear:both}.studio-page[data-view=scheduled] .selbar{border-left:4px solid var(--signal);max-width:750px;margin:0 0 12px auto}
.studio-page .top2{grid-template-columns:repeat(2,minmax(0,1fr))}
@media(max-width:760px){body .studio-bar .account-controls{width:auto;display:flex}.studio-stats strong{font-size:19px}.studio-page .top2 select{min-width:0}}
@media print{body:has(.studio-rail){padding-left:0}.studio-rail,.studio-bar,.studio-tabs{display:none}}
"""

SCRIPT = r"""
(() => {
  const page = document.querySelector('.studio-page'), form = document.querySelector('#go');
  const one = selector => page.querySelector(selector);
  const all = selector => [...page.querySelectorAll(selector)];
  const tabs = [...document.querySelectorAll('.studio-tabs [data-workspace-tab]')];
  const names = new Map(tabs.map(tab => [tab.dataset.workspaceTab, tab.textContent]));
  const backtests = document.createElement('section');
  backtests.className = 'card studio-results';
  backtests.dataset.panel = 'backtests';
  page.append(backtests);
  const scheduleDialog = document.createElement('dialog');
  scheduleDialog.className = 'studio-detail schedule-editor';
  scheduleDialog.setAttribute('aria-label','Create schedule');
  scheduleDialog.innerHTML = '<button type="button" class="drawer-close">Close</button>';
  form.append(scheduleDialog);
  const runPanel = one('.selbar'), panelHome = document.createComment('Run preview');
  if (runPanel) runPanel.before(panelHome);
  scheduleDialog.querySelector('button').onclick = () => scheduleDialog.close();
  scheduleDialog.addEventListener('close', () => {
    scheduleOpen = false;
    one('#schedule-toggle').setAttribute('aria-expanded','false');
    show();
  });
  let loaded = false, loading = false, scheduleOpen = false;
  async function loadBacktests() {
    if (loaded || loading) return;
    loading = true;
    backtests.textContent = 'Loading historical simulations…';
    try {
      const response = await fetch('/strategies/backtests');
      if (!response.ok) throw Error();
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const records = doc.querySelector('.card.wrap');
      if (!records) throw Error();
      backtests.replaceChildren(doc.querySelector('main > p'), records);
      loaded = true;
    } catch {
      backtests.innerHTML = "Historical simulations could not be loaded. <a href='/strategies/backtests'>Retry</a>";
    } finally { loading = false; }
  }
  const versions = document.createElement('section');
  versions.dataset.panel = 'versions';
  page.append(versions);
  async function loadVersions() {
    if (versions.childElementCount || versions.dataset.loading) return;
    versions.dataset.loading = 'true';
    versions.textContent = 'Loading version history…';
    try {
      const response = await fetch('/strategies/archive');
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const records = doc.querySelector('#version-records');
      if (!response.ok || !records) throw Error();
      versions.replaceChildren(records);
    } catch {
      versions.innerHTML = "Could not load versions. <a href='/strategies/archive'>Open version history</a>";
    } finally { delete versions.dataset.loading; }
  }
  one('#scan-fold').onclick = event => {
    const folded = one('.top2').classList.toggle('folded');
    event.currentTarget.setAttribute('aria-expanded', !folded);
  };
  function selectSchedule(value) {
    const radio = form.querySelector('input[name=when][value="' + value + '"]');
    if (radio) { radio.checked = true; radio.dispatchEvent(new Event('input', {bubbles:true})); }
  }
  function show() {
    let view = new URLSearchParams(location.search).get('tab');
    if (location.pathname === '/strategies/backtests') view = 'backtests';
    if (location.hash === '#results') view = 'history';
    if (location.hash === '#activity') view = 'scheduled';
    if (location.hash === '#go') view = 'configure';
    if (location.hash === '#data') view = 'settings';
    if (!names.has(view)) view = 'configure';
    page.dataset.view = view;

    all('[data-panel]').forEach(node => node.hidden = !node.dataset.panel.split(' ').includes(view));
    const previewPanel = one('.selbar');
    if (previewPanel) {
      if (view === 'scheduled') {
        scheduleDialog.append(previewPanel);
        previewPanel.hidden = !scheduleOpen;
      } else {
        if (scheduleDialog.open) scheduleDialog.close();
        panelHome.after(previewPanel);
      }
    }

    if (view === 'backtests') backtests.style.marginTop = '16px';
    if (view === 'history') loadRunHistory(one('#rh-host'));
    if (view === 'settings') loadStrategySettings(one('#ss-host'));
    if (view === 'backtests') loadBacktests();
    if (view === 'versions') loadVersions();
    tabs.forEach(tab => {
      if (tab.dataset.workspaceTab === view) tab.setAttribute('aria-current','page');
      else tab.removeAttribute('aria-current');
    });

  }
  document.addEventListener('click', event => {
    const tab = event.target.closest('[data-workspace-tab]');
    if (!tab || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const key = tab.dataset.workspaceTab;

    event.preventDefault();
    history.pushState(null,'','/strategies?tab=' + key);
    show();
    if (tab.dataset.library) {
      one('.slist')?.scrollIntoView({block:'start'});
      one('.srow input')?.focus({preventScroll:true});
    } else if (!tabs.includes(tab) && key !== 'settings') {
      tabs.find(link => link.dataset.workspaceTab === key)?.focus();
    }
  });
  addEventListener('popstate', show);
  addEventListener('hashchange', show);

  one('#schedule-toggle').disabled = !runPanel;
  one('#schedule-toggle').onclick = event => {
    scheduleOpen = !scheduleOpen;
    event.currentTarget.setAttribute('aria-expanded',scheduleOpen);
    if (scheduleOpen && form.elements.when?.value === 'now') {
      selectSchedule('weekdays');
    }
    show();
    if (scheduleOpen && runPanel) {
      scheduleDialog.showModal();
      form.querySelector('input[name=when]:checked')?.focus();
    }
  };
  function search(id, selector) {
    const field = one(id);
    field.oninput = () => all(selector).forEach(row =>
      row.hidden = !row.textContent.toLowerCase().includes(field.value.trim().toLowerCase()));
    field.onkeydown = event => { if (event.key === 'Enter') event.preventDefault(); };
  }
  search('#strategy-search','.srow');
  one('#find-strategy').onclick = () => {
    const search = one('#strategy-search');
    search.hidden = !search.hidden;
    if (!search.hidden) search.focus();
    else { search.value = ''; search.dispatchEvent(new Event('input')); }
  };
  page.addEventListener('click', async event => {
    const button = event.target.closest('[data-copy-name]');
    if (!button) return;
    try {
      await navigator.clipboard.writeText(button.dataset.copyName);
      button.title = 'Copied filename';
    } catch { window.prompt('Copy strategy filename', button.dataset.copyName); }
  });
  search('#schedule-search',"section[data-panel=scheduled] tbody tr");
  page.addEventListener('submit', event => {
    const action = event.submitter?.formAction || event.target.action;
    if ((action.endsWith('/schedule/cancel') || action.endsWith('/delete')) && !confirm('Confirm this change?')) {
      event.preventDefault(); return;
    }
    if (event.target === form && event.submitter?.id === 'gobtn') event.submitter.disabled = true;
  });
  show();
})();
"""
