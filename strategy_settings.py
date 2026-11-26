"""Authenticated Strategy Settings panel and its persisted preferences."""

import datetime as dt
import hashlib
from html import escape

import portal_workspace as studio
from strategies import _atomic_json, parse_candles, regular_file
from strategy_design import icon

ROOT = "/strategies/settings/"
ALERTS = (
    ("completion", "Run completion", "When a strategy run finishes", "ok"),
    ("failure", "Run failure / error", "When a strategy run fails", "ok"),
    ("reminder", "Scheduled run reminders", "Upcoming time-based runs", "calendar"),
    ("summary", "Result summary", "Recent run and signal counts", "history"),
    ("large", "Large signal alert", "When a run returns 50 or more signals", "play"),
)
DEFAULT_ALERTS = ["completion", "failure", "summary", "large"]
CANDLES = ("1d", "5d", "1m", "5m", "15m", "30m")
ACTIONS = ("data", "alerts", "snapshot", "purge", "reset")


def prefs(manager):
    value = manager._load_all().get("workspace_settings")
    return value if isinstance(value, dict) else {}


def token(rows):
    return hashlib.sha256(" ".join(sorted(r["id"] for r in rows)).encode()).hexdigest()


def change(st, action, fields, owner):
    if action == "snapshot":
        name = fields.get("name", "")
        with st._file_lock:
            meta = st.meta().get(name)
            if not meta or not regular_file(
                st.scripts_dir / meta["file"], st.scripts_dir
            ):
                raise ValueError("Choose an existing strategy.")
            st.vault.save(
                "versions",
                name,
                dict(meta, snapshot_by=owner),
                (st.scripts_dir / meta["file"]).read_bytes(),
            )
        return "Version saved."
    if action == "purge":
        with st._file_lock:
            rows = st.vault.entries("trash")
            if not rows or token(rows) != fields.get("review"):
                raise ValueError("The recycle bin changed. Review its contents again.")
            entry = fields.get("entry")
            targets = [r for r in rows if not entry or r["id"] == entry]
            if not targets:
                raise ValueError("Recycle bin item no longer available.")
            for row in targets:
                st.vault.remove("trash", row["id"])
        return f"Permanently deleted {len(targets)} recycle bin item(s)."
    with st._meta_lock:
        data = st._load_all()
        current = dict(data.get("workspace_settings", {}))
        if action == "data":
            mode, candles = fields.get("mode"), fields.get("candles", "")
            size, unit = parse_candles(candles)
            if mode not in ("close", "intraday"):
                raise ValueError("Choose a supported price basis.")
            current.update(mode=mode, candles=f"{size}{unit}")
        elif action == "alerts":
            current["alerts"] = [key for key, *_ in ALERTS if fields.get(key) == "on"]
        elif action == "reset":
            current = {}
        else:
            raise ValueError("Unknown settings change.")
        data["workspace_settings"] = current
        _atomic_json(st.meta_path, data)
    return (
        "Settings reset to inherited defaults."
        if action == "reset"
        else "Preferences saved."
    )


def h(text):
    return escape(str(text), quote=True)


def hidden(key, value):
    return f"<input type='hidden' name='{key}' value='{h(value)}'>"


def post(action, label, fields="", *, disabled=False, danger=False):
    return (
        f"<form method='post' action='{ROOT + action}'>{fields}<button"
        + (" disabled" if disabled else "")
        + (" class='danger'" if danger else "")
        + f">{label}</button></form>"
    )


def heading(key, title, subtitle):
    return f"<header>{icon(key)}<div><h2>{title}</h2><small>{subtitle}</small></div></header>"


def options(values, selected):
    return "".join(
        f"<option value='{h(key)}'"
        + (" selected" if key == selected else "")
        + f">{h(label)}</option>"
        for key, label in values
    )


def static(label, value):
    return f"<label>{label}<output>{h(value)}</output></label>"


def render(app):
    import server

    st, user = app.strategies, app.settings.user
    configured = prefs(st)
    selection = st.selection()
    enabled = configured.get("alerts", DEFAULT_ALERTS)
    strategies = st.list()
    archived = st.archive_entries("versions")
    trash = st.archive_entries("trash")
    recent = st.results(50)
    jobs = app.schedules.jobs("strategies") if app.schedules else []
    zone = app.settings.tz

    def stamp(value):
        return server._fmt_short(value, zone)

    fmtname = server._pretty_name
    side, tabs = studio.strategy_navigation(server.TABS, "settings")
    basis = configured.get("mode", selection.get("mode", "close"))
    candle = configured.get("candles", selection.get("candles", "1d"))
    provider = "FinViz Elite" if st.price_source == "finviz" else "Massive (Polygon)"
    candle_list = dict.fromkeys((*CANDLES, candle))
    config = (
        heading(
            "database",
            "Data Configuration",
            "Manage market data providers, cache, and data preferences.",
        )
        + f"<form method='post' action='{ROOT}data'><div class='ss-fields'>"
        + static("Market Data Provider", provider)
        + static("Fallback Provider", "None configured")
        + "<label>Price Basis<select aria-label='Price Basis' name='mode'>"
        + options([("close", "Latest close"), ("intraday", "Include intraday")], basis)
        + "</select></label>"
        + "<label>Default Candles<select aria-label='Default Candles' name='candles'>"
        + options(
            [
                (
                    v,
                    v[:-1]
                    + (" day" if v == "1d" else " days" if v.endswith("d") else " min"),
                )
                for v in candle_list
            ],
            candle,
        )
        + "</select></label>"
        + static("Historical Data Range", f"{st.prices.history_days} sessions")
        + static(
            "Cache Strategy",
            "Session-based" if st.price_source == "finviz" else "Stored daily bars",
        )
        + static(
            "Cache refresh",
            "After close / on demand"
            if st.price_source == "finviz"
            else "Provider updates",
        )
        + "<button>Save defaults</button></div></form><div id='ss-advanced'></div>"
    )
    strategy_names = sorted(
        {name for name, _ in strategies} | {r["name"] for r in archived}
    )
    active_names = {name for name, _ in strategies}
    initial = next((n for n in selection.get("names", []) if n in active_names), None)
    initial = initial or next(iter(strategy_names), "")
    selector = (
        "<select name='name' aria-label='Strategy versions' id='ss-filter' required>"
        + "".join(
            f"<option value='{h(n)}' data-active='{int(n in active_names)}'"
            + (" selected" if n == initial else "")
            + f">{h(fmtname(n))}</option>"
            for n in strategy_names
        )
        + "</select>"
    )
    create = post(
        "snapshot", "＋ Create Version", selector, disabled=initial not in active_names
    )

    def restore(bucket, row):
        return (
            "<form method='post' action='/strategies/restore'>"
            + hidden("bucket", bucket)
            + hidden("entry", row["id"])
            + hidden("origin", "settings")
            + "<button>↶ Restore</button></form>"
        )

    def row(name, meta, digest, when, current, entry=None):
        description = meta.get("title") or meta.get("file") or name
        status = "Current" if current else "Archived"
        detail = meta.get("description") or meta.get("file") or name
        return (
            f"<tr data-strategy='{h(name)}'"
            + (" hidden" if name != initial else "")
            + f"><td><code>{h((digest or '—')[:10])}</code></td><td>{h(description)}</td>"
            f"<td>{h(stamp(when))}</td><td>{h(meta.get('snapshot_by', '—'))}</td><td><span class='ss-badge'>{status}</span></td>"
            "<td><div class='ss-actions'><button type='button' data-version-info='"
            + h(detail)
            + "' data-digest='"
            + h(digest or "Unavailable")
            + "' data-title='"
            + h(fmtname(name))
            + "'>View</button>"
            + (restore("versions", entry) if entry else "")
            + "</div></td></tr>"
        )

    rows = [
        row(n, m, m.get("sha256"), m.get("uploaded_utc"), True) for n, m in strategies
    ]
    rows += [
        row(r["name"], r["metadata"], r["sha256"], r["saved_utc"], False, r)
        for r in archived
    ]

    def table(headers, rows, empty):
        return (
            "<div class='ss-scroll'><table><thead><tr>"
            + "".join(f"<th>{t}</th>" for t in headers)
            + "</tr></thead><tbody>"
            + ("".join(rows) or f"<tr><td colspan='{len(headers)}'>{empty}</td></tr>")
            + "</tbody></table></div>"
        )

    versions = (
        heading(
            "versions",
            "Strategy Versions",
            "View and manage version history for your strategies.",
        )
        + "<div class='ss-tools'>"
        + create
        + "</div>"
        + table(
            ["Version", "Description", "Updated", "Author", "Status", "Actions"],
            rows,
            "No strategy versions yet.",
        )
    )
    review = hidden("review", token(trash))
    deleted = [
        f"<tr><td>{h(fmtname(r['name']))}<small>{h(r['name'])}.py</small></td><td>{h(stamp(r['saved_utc']))}</td>"
        f"<td>{h(stamp(dt.datetime.fromtimestamp(r['expires'], dt.UTC).isoformat()))}</td><td>{r['bytes']:,} B</td>"
        "<td><div class='ss-actions'>"
        + restore("trash", r)
        + post("purge", "Delete", review + hidden("entry", r["id"]), danger=True)
        + "</div></td></tr>"
        for r in trash
    ]
    recycle = (
        heading(
            "trash",
            "Recycle Bin",
            "Deleted strategies are kept for 30 days before permanent removal.",
        )
        + "<span class='ss-retention'>Retention · 30 Days</span>"
        + table(
            ["Strategy", "Deleted on", "Recover until", "Size", "Actions"],
            deleted,
            "The recycle bin is empty.",
        )
    )
    notices = []
    for key, *_ in ALERTS:
        if key not in enabled:
            continue
        if key == "reminder":
            future = sorted(
                at
                for j in jobs
                if j.get("status") == "scheduled" and (at := app.schedules.next_fire(j))
            )
            if future:
                notices.append("Next scheduled run · " + stamp(future[0].isoformat()))
        elif key == "summary":
            notices.append(
                f"{sum(r.get('status') == 'ok' for r in recent)} successful runs in the latest {len(recent)} records."
            )
        else:
            for r in recent:
                buys = sum(
                    v
                    for k, v in r.get("counts", {}).items()
                    if k in server.BUY_STATUSES or k == "rows"
                )
                if (
                    (key == "completion" and r.get("status") == "ok")
                    or (key == "failure" and r.get("status") == "error")
                    or (key == "large" and buys >= 50 and r.get("status") == "ok")
                ):
                    notices.append(
                        fmtname(r.get("strategy", ""))
                        + " · "
                        + (
                            f"{buys} signals"
                            if key == "large"
                            else "completed"
                            if key == "completion"
                            else "failed"
                        )
                        + " · "
                        + stamp(r.get("started_utc"))
                    )
                    break
    bell = (
        "<details class='ss-bell'><summary aria-label='Open notifications'>"
        + icon("bell")
        + "</summary><div><b>Notifications</b>"
        + "".join("<p>" + h(n) + "</p>" for n in notices or ["No matching activity."])
        + "</div></details>"
    )
    alerts = (
        heading(
            "bell",
            "Notifications",
            "Choose alerts for the portal notification center.",
        )
        + f"<form method='post' action='{ROOT}alerts'>"
        + "".join(
            f"<label class='ss-alert'>{icon(sym)}<span><b>{title}</b><small>{subtitle}</small></span><input role='switch' type='checkbox' name='{key}'"
            + (" checked" if key in enabled else "")
            + "></label>"
            for key, title, subtitle, sym in ALERTS
        )
        + "<button class='ss-alerts-save' hidden>Save notifications</button></form>"
    )
    access = (
        heading(
            "users",
            "Access Controls",
            "View who can access your strategy workspace.",
        )
        + "<div class='ss-access'><b>Workspace Access</b><output>Private · Owner only</output></div><div class='ss-access'><span><b>Collaboration</b><small>Shared strategy access is unavailable.</small></span><span class='ss-badge'>Off</span></div>"
        + f"<div class='ss-members'><b>Workspace members (1)</b><div><i>{h(user[:1].upper())}</i>{h(user)}<span class='ss-badge'>Owner</span></div></div>"
    )
    danger = (
        heading(
            "warning",
            "Destructive Actions",
            "Reset run defaults and notification preferences, or permanently empty the recycle bin.",
        )
        + post("reset", "↶　Reset Strategy Settings", danger=True)
        + post(
            "purge",
            "Permanently Delete Recycle Bin Items",
            review,
            danger=True,
            disabled=not trash,
        )
    )
    search = "<input type='search' id='ss-search' aria-keyshortcuts='Control+K Meta+K' aria-label='Search settings and strategies' placeholder='Search settings and strategies…　⌘ K'>"
    return (
        "<div id='ss-root'><aside class='ss-side'><a class='ss-brand' href='/'><i class='ss-mark' aria-hidden='true'><em></em><em></em><em></em><em></em></i><span>Finviz Agent<small>AI Trading Research Studio</small></span></a><nav aria-label='Portal modules'>"
        + side
        + "</nav>"
        + "<details class='ss-workspace'><summary>Workspace</summary>"
        + server.theme_control()
        + "<form method='post' action='/logout'><button>Logout</button></form></details>"
        + f"<footer><i>{h(user[:1].upper())}</i><div>{h(user)}<small>Workspace owner</small></div></footer></aside><article class='ss-main'>"
        + "<div class='ss-bar'><span><a href='/strategies'>Strategies</a>　 /　 <b>Strategy Settings</b></span>"
        + search
        + bell
        + "<button class='ss-gear' type='button' aria-label='Workspace settings'>"
        + icon("settings")
        + "</button>"
        + "</div>"
        + "<h1>Strategy Settings</h1><p>Configure data sources, versions, notifications, and access for your trading strategies.</p>"
        + "<nav class='ss-tabs' aria-label='Settings workspace'>"
        + tabs
        + "</nav>"
        + server._flash_html(app, "strategy-settings")
        + "<div class='ss-grid'><div>"
        + "".join(
            "<section class='ss-card'>" + v + "</section>"
            for v in (config, versions, recycle)
        )
        + "</div><div>"
        + "".join(
            "<section class='ss-card'>" + v + "</section>" for v in (alerts, access)
        )
        + "</div></div>"
        + "<section class='ss-card ss-danger'>"
        + danger
        + "</section><p hidden id='ss-no-match'>No matching settings or strategies.</p>"
        + "<dialog id='ss-dialog'><h2></h2><p></p><code></code><form method='dialog'><button>Close</button></form></dialog></article></div>"
    )


SCRIPT = r"""
async function loadStrategySettings(host) {
  const advanced = document.getElementById('data');
  if (location.hash === '#data') advanced.open = true;
  if (host.querySelector('#ss-root') || host.dataset.loading) return;
  host.dataset.loading = 'true';
  host.textContent = 'Loading strategy settings…';
  try {
    const response = await fetch('/strategies/settings/panel');
    if (!response.ok) throw Error();
    const root = new DOMParser().parseFromString(await response.text(), 'text/html').getElementById('ss-root');
    if (!root) throw Error();
    host.replaceChildren(root);
    root.querySelector('#ss-advanced').append(advanced);
    if (location.hash === '#data') advanced.scrollIntoView();
    root.querySelectorAll('[data-theme-choice]').forEach(button =>
      button.setAttribute('aria-pressed', button.dataset.themeChoice === document.documentElement.dataset.theme));
    const selector = root.querySelector('#ss-filter');
    selector.onchange = () => {
      root.querySelectorAll('[data-strategy]').forEach(row => row.hidden = row.dataset.strategy !== selector.value);
      selector.form.querySelector('button').disabled = selector.selectedOptions[0]?.dataset.active !== '1';
    };
    const alertsForm = root.querySelector('.ss-alerts-save').form;
    alertsForm.onchange = () => alertsForm.querySelector('button').hidden = false;
    const search = root.querySelector('#ss-search');
    search.oninput = () => {
      const query = search.value.toLowerCase().trim();
      const cards = [...root.querySelectorAll('.ss-card')];
      cards.forEach(card => card.hidden = !(card.querySelector('header').textContent + ' ' + [...card.querySelectorAll('label,option,tbody,.ss-members')].filter(n => !n.closest('#ss-advanced')).map(n => n.textContent).join(' ')).toLowerCase().includes(query));
      root.querySelector('#ss-no-match').hidden = cards.some(card => !card.hidden);
      const match = query && [...selector.options].find(option => option.text.toLowerCase().includes(query) ||
        option.value.toLowerCase().includes(query));
      if (match) { selector.value = match.value; selector.onchange(); }
    };
    root.querySelector('.ss-gear').onclick = () => {
      const menu = root.querySelector('.ss-workspace');
      menu.open = !menu.open;
      if (menu.open) menu.querySelector('button').focus();
    };
    if (location.hash === '#notifications') {
      root.querySelector('.ss-bell').open = true;
      root.querySelector('.ss-bell summary').focus();
    }
    root.onclick = event => {
      const button = event.target.closest('[data-version-info]');
      if (!button) return;
      const dialog = root.querySelector('#ss-dialog');
      dialog.querySelector('h2').textContent = button.dataset.title;
      dialog.querySelector('p').textContent = button.dataset.versionInfo;
      dialog.querySelector('code').textContent = 'SHA-256 · ' + button.dataset.digest;
      dialog.showModal();
    };
    root.addEventListener('submit', event => {
      const form = event.target;
      if (form.action.endsWith('/settings/purge') || form.action.endsWith('/settings/reset')) {
        const prompt = form.action.endsWith('/settings/reset') ? 'Reset run defaults and notification preferences?' :
          (form.elements.entry ? 'Permanently delete this strategy from the recycle bin?' : 'Permanently delete every item shown in the recycle bin?');
        if (!window.confirm(prompt)) event.preventDefault();
      }
    });
  } catch {
    host.innerHTML = "<p role='alert'>Strategy settings could not be loaded. <a href='/strategies?tab=settings'>Reload settings</a> · <a href='/strategies'>Return to Strategies</a></p>";
  } finally { delete host.dataset.loading; }
}
document.addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' &&
      document.querySelector('.studio-page[data-view=settings] #ss-search')) {
    event.preventDefault(); document.getElementById('ss-search').focus();
  }
});
"""

CSS = """
body:has(.studio-page[data-view=settings]) main{max-width:none;padding:0}
body:has(.studio-page[data-view=settings]) :is(.sd-header,.sd-hero,main>.studio-tabs,main>p.muted){display:none}
.studio-page[data-view=settings]>:not(#ss-host){display:none!important}
#ss-host{min-height:100vh}
:is(#ss-root,#rh-root){--ss-border:var(--sd-line);--ss-surface:var(--sd-card);--ss-field:var(--bg);--ss-green:var(--sd-mint);--ss-sub:var(--sd-muted);--ss-dim:color-mix(in srgb,var(--ss-green) 10%,var(--ss-surface));color:var(--sd-ink);display:grid;grid-template-columns:clamp(188px,15.1vw,242px) minmax(0,1fr);min-height:100vh;font:14px/1.4 system-ui,-apple-system,sans-serif}
html[data-theme=dark] :is(#ss-root,#rh-root){--ss-border:#344853;--ss-field:#0c171f;--ss-green:#70e8ba;--ss-surface:#13212a;--ss-sub:#c0d0da;background:radial-gradient(ellipse at 58% 30%,#0f1b23,#09131b 80%)}
:is(#ss-root,#rh-root) *{box-sizing:border-box}
:is(#ss-root,#rh-root) form,:is(#ss-root,#rh-root) nav{margin:0}
:is(#ss-root,#rh-root) a{color:inherit;text-decoration:none}
:is(#ss-root,#rh-root) svg{width:21px;height:21px;flex:none}
:is(#ss-root,#rh-root) :is(button,input,select,output){font:inherit;color:inherit;max-width:100%}
:is(#ss-root,#rh-root) :is(button,select,input[type=search],output){border:1px solid var(--ss-border);border-radius:7px;background:var(--ss-field);padding:6px 10px}
:is(#ss-root,#rh-root) button{cursor:pointer;font-weight:600}
:is(#ss-root,#rh-root) :is(a,button,select,summary,input):focus-visible{outline:2px solid var(--ss-green);outline-offset:2px}
:is(#ss-root,#rh-root) :is(a,button):hover{color:var(--ss-green)}
:is(#ss-root,#rh-root) button:disabled{opacity:.53;cursor:default}
.ss-side{position:sticky;top:0;min-width:0;height:100dvh;overflow:auto;display:flex;flex-direction:column;border-right:1px solid var(--ss-border);padding:14px 9px 20px}
.ss-brand{display:flex;align-items:center;gap:13px;margin:0 12px 30px;font-size:21px;letter-spacing:-.6px;font-weight:730}
:is(#ss-root,#rh-root) small{display:block;font-weight:400;font-size:12px;line-height:1.45;color:var(--ss-sub)}
.ss-brand small{letter-spacing:.1px}
.ss-mark{display:flex;height:30px;align-items:end;gap:2px}.ss-mark em{width:5px;border-radius:3px;background:#43dfaa;height:40%}.ss-mark em:nth-child(2){height:70%}.ss-mark em:nth-child(3){height:100%}.ss-mark em:nth-child(4){height:76%}
:is(#ss-root,#rh-root) .ss-side nav{display:grid;gap:2px;border:0;padding:0}
.ss-side nav a,.ss-workspace summary{display:flex;align-items:center;gap:14px;border:1px solid transparent;padding:10px 15px;border-radius:9px;font-weight:520}
:is(#ss-root,#rh-root) .ss-side nav a[aria-current],:is(#ss-root,#rh-root) .ss-tabs a[aria-current]{border-color:var(--ss-green);background:color-mix(in srgb,var(--ss-green) 18%,var(--ss-field));box-shadow:inset 0 0 18px #32ca8911}
.ss-side nav a[aria-current] svg{color:var(--ss-green)}
.ss-workspace{margin:14px 8px;padding:9px 0;border-top:1px solid var(--ss-border)}
.ss-workspace summary{cursor:pointer;padding:9px}.ss-workspace summary:after{content:'⌄';margin-left:auto}.ss-workspace .theme-switch{margin:8px 0}
.ss-side footer{display:flex;align-items:center;gap:12px;margin:auto 8px 0;border-top:1px solid var(--ss-border);padding:20px 2px 0}
.ss-side footer i,.ss-members i{display:grid;place-items:center;width:36px;height:36px;background:#258962;color:white;border-radius:100%;font-weight:700;font-style:normal}
.ss-main{min-width:0;padding:0 clamp(14px,1.2vw,24px) 28px}
.ss-bar{display:flex;align-items:center;gap:18px;min-height:50px;border-bottom:1px solid var(--ss-border)}
:is(#ss-root,#rh-root) .ss-bar a{color:var(--ss-green)}
:is(#ss-root,#rh-root) .ss-bar input{margin-left:auto;width:min(31%,365px);height:34px}
#ss-root .ss-gear{display:grid;place-items:center;background:transparent;border:0;padding:4px}
@media(max-width:850px){#ss-root .ss-side .ss-workspace[open]{display:block;position:absolute;right:14px;top:110px;z-index:5;background:var(--ss-surface);border:1px solid var(--ss-border);border-radius:8px;padding:8px}}
.ss-bell{position:relative}.ss-bell summary{display:flex;align-items:center;padding:6px;cursor:pointer}.ss-bell>div{position:absolute;z-index:3;right:0;top:100%;width:min(310px,84vw);padding:16px;background:var(--ss-surface);border:1px solid var(--ss-border);border-radius:8px;box-shadow:0 12px 30px #0003}.ss-bell p{margin:8px 0 0}
:is(#ss-root,#rh-root) h1{font-size:30px;line-height:1.15;letter-spacing:-.85px;margin:13px 10px 4px}
.ss-main>p{margin:0 10px 16px;color:var(--sd-ink)}
:is(#ss-root,#rh-root) .ss-tabs{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;padding:0;border:0;margin:17px 0 20px}
.ss-tabs a{display:flex;align-items:center;justify-content:center;gap:12px;height:47px;padding:6px;text-align:center;font-weight:600;font-size:14px;border:1px solid var(--ss-border);border-radius:10px;background:linear-gradient(130deg,var(--ss-surface),var(--ss-field))}
.ss-tabs a[aria-current] svg{color:var(--ss-green)}
.ss-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(290px,1fr);gap:18px}
.ss-grid>div{min-width:0;display:flex;flex-direction:column;gap:16px}
.ss-card{position:relative;min-width:0;margin:0;border:1px solid var(--ss-border);border-radius:12px;padding:14px 17px;background:linear-gradient(145deg,var(--ss-surface),color-mix(in srgb,var(--ss-surface) 67%,var(--ss-field)));box-shadow:inset 0 1px 0 #ffffff05}
.ss-card>header{display:flex;align-items:flex-start;gap:15px;padding:0 2px 9px;margin:0 0 10px;border-bottom:1px solid var(--ss-border)}
:is(#ss-root,#rh-root) h2{margin:0 0 3px;font-size:17px;letter-spacing:-.4px;line-height:1.25}
.ss-card>header svg{color:var(--ss-green);width:25px!important;height:25px!important}
.ss-fields{display:grid;grid-template-columns:1.08fr 1.08fr .94fr .94fr;gap:15px 13px;align-items:end}
.ss-fields label{display:grid;gap:4px;font-size:12px;font-weight:550}
:is(#ss-root,#rh-root) .ss-fields :is(output,select,button){display:block;width:100%;height:35px;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
:is(#ss-root,#rh-root) .ss-fields button,.ss-tools button,:is(#ss-root,#rh-root) .ss-actions button,:is(#ss-root,#rh-root) .ss-alerts-save{color:var(--ss-green);border-color:color-mix(in srgb,var(--ss-green) 60%,var(--ss-border))}
#ss-advanced #data{margin:9px 0 0;padding:0;border:0;background:transparent;box-shadow:none}
#ss-advanced summary{cursor:pointer;font-size:12px;color:var(--ss-sub)!important}
.ss-tools{display:flex;justify-content:flex-end;margin:0 0 8px}
.ss-tools form,.ss-actions{display:flex;gap:7px;align-items:center}
.ss-tools select{min-width:0;width:clamp(115px,13vw,210px)}
.ss-scroll{max-height:247px;overflow:auto;min-width:0}
:is(#ss-root,#rh-root) table{width:100%;border-collapse:collapse;font-size:12px;margin:0;color:inherit}
:is(#ss-root,#rh-root) :is(th,td){border:0;border-bottom:1px solid color-mix(in srgb,var(--ss-border) 80%,transparent);background:transparent;text-align:left;padding:7px 5px;white-space:nowrap}
:is(#ss-root,#rh-root) th{font-size:11px;font-weight:600;color:var(--ss-sub)}
:is(#ss-root,#rh-root) .ss-actions button{padding:3px 7px;font-size:11px}
.ss-badge,:is(#ss-root,#rh-root) td code{display:inline-block;color:var(--ss-green);background:var(--ss-dim);border:1px solid var(--ss-border);border-radius:20px;font-size:11px;padding:2px 8px}
.ss-retention{position:absolute;right:17px;top:12px;padding:5px 9px;font-size:12px;border:1px solid var(--ss-border);border-radius:7px}
.ss-alert,.ss-access{display:flex;align-items:center;gap:13px;padding:7px 0;margin:0;border-bottom:1px solid var(--ss-border);cursor:pointer}
.ss-alert svg{color:var(--ss-green)}.ss-alert span,.ss-access span{line-height:1.22}
:is(#ss-root,#rh-root) .ss-alert input{appearance:none;margin:0 0 0 auto;flex:none;width:37px;height:21px;border-radius:12px;border:1px solid var(--ss-border);background:#79848d;cursor:pointer;position:relative}
.ss-alert input:before{content:'';position:absolute;top:2px;left:2px;width:15px;height:15px;border-radius:50%;background:#fff;transition:transform .12s}
:is(#ss-root,#rh-root) .ss-alert input:checked{background:#39c881}.ss-alert input:checked:before{transform:translateX(15px)}
.ss-alert+button{margin-top:8px;font-size:12px!important}
.ss-access{cursor:default;justify-content:space-between;min-height:50px}.ss-members{margin:13px 0 5px;padding:12px;border:1px solid var(--ss-border);border-radius:8px}.ss-members>div{display:flex;align-items:center;gap:10px;margin-top:10px;padding-top:12px;border-top:1px solid var(--ss-border)}.ss-members .ss-badge{margin-left:auto}.ss-members i{width:28px;height:28px}
.ss-danger{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:20px;border-color:#d77e74;background:color-mix(in srgb,#bd5042 10%,var(--ss-surface));min-height:75px}
.ss-danger>header{align-items:center;flex:1;margin:0;border:0;padding:0}.ss-danger>header svg{color:#ee8278}
:is(#ss-root,#rh-root) button.danger{color:#bc443e;border-color:#cd726c}
html[data-theme=dark] :is(#ss-root,#rh-root) button.danger{color:#ff978d}
#ss-dialog{background:var(--ss-surface);color:var(--sd-ink);border:1px solid var(--ss-border);border-radius:12px;padding:22px;max-width:min(430px,90vw);overflow-wrap:anywhere}#ss-dialog form{margin-top:15px}
@media(min-width:1350px){.ss-tools{position:absolute;top:12px;right:16px}.ss-tools button{white-space:nowrap}}
@media(max-width:1150px){:is(#ss-root,#rh-root){grid-template-columns:170px minmax(0,1fr)}.ss-grid{grid-template-columns:minmax(0,1.65fr) minmax(255px,1fr);gap:12px}.ss-card{padding:13px}.ss-fields{grid-template-columns:1fr 1fr}.ss-tabs a{gap:6px;font-size:12px}.ss-retention{position:static;display:inline-block}.ss-brand{font-size:18px;gap:8px}.ss-danger>header{flex-basis:100%}}
@media(max-width:850px){:is(#ss-root,#rh-root){display:block}.ss-side{position:static;height:auto;padding:7px 14px;border-right:0;border-bottom:1px solid var(--ss-border)}.ss-brand{margin:0 0 6px}.ss-brand small,.ss-side footer,.ss-side .ss-workspace{display:none}:is(#ss-root,#rh-root) .ss-side nav,:is(#ss-root,#rh-root) .ss-tabs{display:flex;overflow:auto;flex-wrap:nowrap}.ss-side nav a,.ss-tabs a{flex:none;white-space:nowrap}.ss-side nav a{padding:6px 10px}.ss-tabs a{padding:6px 12px}.ss-bar{gap:8px}.ss-grid{grid-template-columns:minmax(0,1fr)}.ss-main{padding:0 12px 20px}:is(#ss-root,#rh-root) .ss-tabs{margin:12px 0}.ss-danger>header{align-items:flex-start}.ss-main>p,:is(#ss-root,#rh-root) h1{margin-left:0}}
@media(max-width:480px){.ss-bar>svg{display:none}.ss-bar span{font-size:12px}.ss-bar input{min-width:110px}:is(#ss-root,#rh-root) .ss-brand small{display:none}.ss-fields{gap:9px}.ss-danger form,.ss-danger button{width:100%}}
"""
