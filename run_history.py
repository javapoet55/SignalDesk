"""Strategy run history and the performance of backtests linked to each run."""

import datetime as dt
import os
from html import escape
from math import ceil
from statistics import mean
from urllib.parse import quote, urlencode

import backtests
import strategies as sm
import vectorbt_workspace as vb
from backtest_dashboard import fmt
from strategy_design import icon

STATUS = {
    "ok": "Success",
    "error": "Failed",
    "running": "Running",
    "cancelled": "Cancelled",
    "unknown": "Unknown",
}
SIZE = 10


def started(record, field="started_utc"):
    try:
        value = dt.datetime.fromisoformat(record[field])
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    except (KeyError, TypeError, ValueError):
        return None


def seconds(record):
    start, finish = started(record), started(record, "finished_utc")
    if start and finish and finish >= start:
        return (finish - start).total_seconds()


def duration(value):
    if value is None:
        return "—"
    mins, secs = divmod(round(value), 60)
    return f"{mins}m {secs:02d}s" if mins else f"{secs}s"


def metrics(records):
    count = len(records)
    elapsed = [v for r in records if (v := seconds(r)) is not None]
    return (
        count,
        sum(r.get("status") == "ok" for r in records),
        sum(r.get("status") == "error" for r in records),
        mean(elapsed) if elapsed else None,
    )


def cards(records, now):
    total, success, failed, runtime = metrics(records)
    cutoff = now - dt.timedelta(days=30)
    earlier = cutoff - dt.timedelta(days=30)
    recent = []
    prior = []
    for record in records:
        when = started(record)
        if when and cutoff <= when <= now:
            recent.append(record)
        elif when and earlier <= when < cutoff:
            prior.append(record)
    current, previous = metrics(recent), metrics(prior)

    def delta(i):
        if not previous[0]:
            return "No runs in the prior 30 days"
        if i in (1, 2):
            if not current[0]:
                return "No runs in the latest 30 days"
            diff = 100 * (current[i] / current[0] - previous[i] / previous[0])
            return f"{diff:+.1f} pp vs. prior 30 days"
        if not previous[i] or current[i] is None:
            return "Comparison unavailable"
        return f"{100 * (current[i] / previous[i] - 1):+.1f}% vs. prior 30 days"

    tiles = (
        ("Total Runs", total, "", "play"),
        (
            "Successful",
            success,
            f" ({100 * success / total:.1f}%)" if total else "",
            "ok",
        ),
        (
            "Failed",
            failed,
            f" ({100 * failed / total:.1f}%)" if total else "",
            "failure",
        ),
        ("Average Runtime", duration(runtime), "", "scheduled"),
    )
    return (
        "<div class='rh-metrics'>"
        + "".join(
            f"<section>{icon(sym)}<div><small>{title}</small><strong>{value}<em>{part}</em></strong><span>{delta(i)}</span></div></section>"
            for i, (title, value, part, sym) in enumerate(tiles)
        )
        + "</div>"
    )


def url(fields, **changes):
    query = {**fields, **changes}
    return "/strategies?" + escape(
        urlencode({"tab": "history", **{k: v for k, v in query.items() if v}}),
        quote=True,
    )


def link(fields, title, **changes):
    return f"<a data-history-link href='{url(fields, **changes)}'>{title}</a>"


def select(name, title, items, value):
    return (
        f"<label>{title}<select aria-label='{title}' name='{name}'>"
        + "".join(
            f"<option value='{escape(k)}'"
            + (" selected" if k == value else "")
            + f">{escape(v)}</option>"
            for k, v in items
        )
        + "</select></label>"
    )


def local_day(record, zone):
    time = started(record)
    if time:
        return time.astimezone(zone).date().isoformat()
    return record["id"][:10]


def tickers(folder):
    if not sm.regular_file(folder / "tickers.txt", folder):
        return None
    try:
        lines = sm.read_text(folder / "tickers.txt", folder).splitlines()
        if lines and lines[0].startswith("Run:"):
            lines = lines[1:]
        return [
            ticker.strip().upper()
            for line in lines
            for ticker in line.split(",")
            if ticker.strip()
        ]
    except OSError:
        return None


def performance(rid):
    target = f"<a href='/vectorbt?rid={quote(rid)}'>Open in VectorBT PRO →</a>"
    if not (os.getenv("VBT_SERVICE_URL") and os.getenv("VBT_SERVICE_TOKEN")):
        return "<p>VectorBT PRO is unavailable.</p>" + target
    try:
        job = backtests.service("/runs/" + quote(rid) + "/latest-backtest")
    except ValueError:
        return "<p>Backtest performance could not be loaded.</p>" + target
    if not job or job["inputs"].get("run") != rid:
        return "<p>No completed backtest is linked to this run.</p>" + target
    m = vb.report_data(job, "")
    details = "".join(
        f"<div><dt>{label}</dt><dd>{fmt(m.get(key), kind)}</dd></div>"
        for label, key, kind in (
            ("Total Return", "total", "pct"),
            ("Sharpe Ratio", "sharpe", "number"),
            ("Max Drawdown", "maxdd", "pct"),
            ("Win Rate", "rate", "pct"),
        )
    )
    chart = vb.plot(m["days"], m["equity"], m.get("benchmark"), compact=True)
    legend = (
        "Strategy equity · Buy &amp; hold" if m.get("benchmark") else "Strategy equity"
    )
    return (
        f"<small>{escape(m['start'])} – {escape(m['end'])} · {m['hold']} session hold</small>"
        f"<div class='rh-performance'><dl>{details}</dl><figure><figcaption>{legend}</figcaption>{chart}</figure></div>"
        f"<a href='/vectorbt?job={quote(job['id'])}'>View completed backtest →</a>"
    )


def details(record, directory, zone):
    import server

    if not record:
        return "<h2>Run Details</h2><p>Select a run to see its signals, logs, and performance.</p>"
    rid = record["id"]
    folder = directory / rid
    has_csv = sm.regular_file(folder / "signals.csv", folder)
    names = server.signal_tickers(folder) if has_csv else []
    base = "/strategies/results/" + quote(rid) + "/"
    log = sm.read_text(folder / "output.txt", folder, 40000)
    title = escape(server._pretty_name(record.get("strategy", "")))
    exports = (
        (
            f"<a download='{rid}-signals.csv' href='{base}signals.csv'>↓　Download CSV</a>"
            f"<a download='{rid}-signals.txt' href='{base}{server.SIGNAL_TXT}'>↓　Download TXT</a>"
        )
        if has_csv
        else "<small>Signal exports are available after a run writes its results.</small>"
    )
    status = record.get("status")
    return (
        f"<header><h2>Run Details</h2><button type='button' class='rh-copy' data-copy='{rid}' title='{rid}' aria-label='Copy run ID'>Run ID · {rid}　⧉</button></header>"
        f"<div class='rh-run-title'>{icon('ok' if status == 'ok' else 'failure' if status == 'error' else 'scheduled')}<div><h3>{title}</h3>"
        f"<small>{escape(server._fmt_local(record.get('started_utc'), zone))} · Duration {duration(seconds(record))}</small></div></div>"
        "<nav class='rh-detail-nav' aria-label='Run details sections'>"
        + "".join(
            f"<button type='button' data-detail='{key}' aria-pressed='{'true' if key == 'summary' else 'false'}'>{label}</button>"
            for key, label in (
                ("summary", "Summary"),
                ("signals", f"Signals ({len(names)})" if has_csv else "Signals"),
                ("logs", "Logs"),
            )
        )
        + "</nav>"
        f"<section data-detail-panel='summary'><h3>Backtest Performance</h3><div data-performance='{rid}'>Loading linked performance…</div></section>"
        + "<section hidden data-detail-panel='signals'><h3>Buy signals</h3><p class='rh-symbols'>"
        + (
            ", ".join(map(escape, names))
            or ("No buy signals." if has_csv else "No signal file available.")
        )
        + "</p></section>"
        + "<section hidden data-detail-panel='logs'><h3>Strategy output</h3><pre>"
        + escape(log or record.get("error") or "No log available.")
        + "</pre></section>"
        + f"<p class='rh-fulllink'><a href='{base}'>Open full run report →</a></p><div class='rh-downloads'>{exports}</div>"
    )


def panel(app, params):
    import server

    fields = {
        k: params[k][:200]
        for k in (
            "strategy",
            "status",
            "range",
            "start",
            "end",
            "q",
            "sort",
            "page",
            "rid",
        )
        if params.get(k)
    }
    st = app.strategies
    zone = app.settings.tz
    all_runs = [
        r
        for r in st.results(None)
        if sm.RESULT_RE.fullmatch(r.get("id", ""))
        and not (st.results_dir / r["id"]).is_symlink()
    ]
    names = sorted({r.get("strategy", "") for r in all_runs if r.get("strategy")})
    strategy = fields.get("strategy", "")
    status = fields.get("status", "")
    span = fields.get("range", "")
    start = end = ""
    today = app.clock().astimezone(zone).date()
    if span in ("7", "30", "90"):
        start = (today - dt.timedelta(days=int(span) - 1)).isoformat()
        end = today.isoformat()
    if span == "custom":
        start, end = fields.get("start", ""), fields.get("end", "")
    q = fields.get("q", "").strip().lower()
    matched = []
    for record in all_runs:
        rid = record["id"]
        day = local_day(record, zone)
        if (
            (strategy and record.get("strategy") != strategy)
            or (status and record.get("status", "unknown") != status)
            or (start and day < start)
            or (end and day > end)
        ):
            continue
        if (
            q
            and q
            not in (rid + " " + server._pretty_name(record.get("strategy", ""))).lower()
        ) and q.upper() not in (tickers(st.results_dir / rid) or []):
            continue
        matched.append(record)
    matched.sort(
        key=lambda r: (started(r) or dt.datetime.min.replace(tzinfo=dt.UTC), r["id"]),
        reverse=fields.get("sort") != "oldest",
    )
    total = len(matched)
    pages = max(1, ceil(total / SIZE))
    number = min(
        max(
            1,
            int(fields.get("page", "1"))
            if fields.get("page", "1").isascii() and fields.get("page", "1").isdigit()
            else 1,
        ),
        pages,
    )
    offset = (number - 1) * SIZE
    rows = matched[offset : offset + SIZE]
    chosen = next(
        (r for r in rows if r["id"] == fields.get("rid")), rows[0] if rows else None
    )
    filters = (
        select(
            "strategy",
            "Strategy",
            [("", "All Strategies"), *((n, server._pretty_name(n)) for n in names)],
            strategy,
        )
        + select("status", "Status", [("", "All Status"), *STATUS.items()], status)
        + select(
            "range",
            "Date Range",
            [
                ("", "All dates"),
                ("7", "Latest 7 days"),
                ("30", "Latest 30 days"),
                ("90", "Latest 90 days"),
                ("custom", "Custom dates"),
            ],
            span,
        )
        + f"<label>Search<input type='search' name='q' value='{escape(fields.get('q', ''))}' placeholder='Search by strategy, ticker, or run ID…'></label>"
        + "<div class='rh-dates'"
        + ("" if span == "custom" else " hidden")
        + ">"
        + f"<label>From<input type='date' name='start' value='{escape(start)}'></label><label>Through<input type='date' name='end' value='{escape(end)}'></label></div>"
    )
    body = []
    for record in rows:
        rid = record["id"]
        folder = st.results_dir / rid
        code = record.get("status", "unknown")
        count = record.get("counts", {})
        signals = sum(count.get(k, 0) for k in (*server.BUY_STATUSES, "rows"))
        candidates = tickers(folder)
        rowlink = url(fields, rid=rid, page=str(number))
        base = "/strategies/results/" + quote(rid) + "/"
        exports = (
            (
                f"<a download href='{base}signals.csv' title='Download signals CSV' aria-label='Download signals CSV'>↓</a>"
                f"<a download href='{base}{server.SIGNAL_TXT}' title='Download tickers TXT' aria-label='Download tickers TXT'>≡</a>"
            )
            if sm.regular_file(folder / "signals.csv", folder)
            else ""
        )
        more = f"<a href='{base}'>Full run report</a>" + (
            server._cancel_strategy_button(rid, "Stop")
            if code == "running"
            else server._delete_run_button(rid)
        )
        body.append(
            f"<tr data-select='{rowlink}'"
            + (" class='rh-selected'" if chosen == record else "")
            + f"><td><a data-history-link href='{rowlink}'>{escape(server._fmt_short(record.get('started_utc'), zone))}</a></td>"
            + f"<td>{escape(server._pretty_name(record.get('strategy', '')))}</td><td>{len(candidates) if candidates is not None else '—'}</td>"
            + f"<td>{duration(seconds(record))}</td><td><span class='rh-status {escape(code)}'>{escape(STATUS.get(code, 'Unknown'))}</span></td>"
            + f"<td>{str(signals) + ' Buy' if count or code == 'ok' else '—'}</td><td><div class='rh-actions'>{exports}<details><summary aria-label='More run actions'>•••</summary><div>{more}</div></details></div></td></tr>"
        )

    def page_button(n, label):
        if 1 <= n <= pages and n != number:
            return link(fields, label, page=str(n), rid="")
        active = " aria-current='page'" if n == number else ""
        return f"<span{active}>{label}</span>"

    pagination = [page_button(number - 1, "‹")]
    previous = 0
    for n in sorted({1, pages, *range(max(1, number - 2), min(pages, number + 2) + 1)}):
        if n > previous + 1:
            pagination.append("<span>…</span>")
        pagination.append(page_button(n, str(n)))
        previous = n
    pagination.append(page_button(number + 1, "›"))
    return (
        "<div id='rh-root'><article class='ss-main'><div class='rh-title'><h1>Run History</h1>"
        "<a href='/strategies?tab=settings#notifications' aria-label='Portal notifications'>"
        + icon("bell")
        + "</a></div><p>View past strategy runs, results, and performance. Select a run to see detailed signals and analytics.</p>"
        + cards(all_runs, app.clock())
        + "<form class='rh-filters' id='rh-filters'>"
        + filters
        + "</form>"
        + "<div class='rh-columns'><section class='rh-results'><header><h2>Run Results <small>("
        + str(total)
        + ")</small></h2>"
        + select(
            "sort",
            "Sort",
            [("newest", "Newest first"), ("oldest", "Oldest first")],
            fields.get("sort", "newest"),
        )
        + "<button type='button' data-refresh>↻ Refresh</button></header><div class='rh-scroll'><table><thead><tr>"
        + "".join(
            f"<th>{h}</th>"
            for h in (
                "Run Time",
                "Strategy",
                "Tickers",
                "Duration",
                "Status",
                "Signals",
                "Actions",
            )
        )
        + "</tr></thead><tbody>"
        + (
            "".join(body)
            or "<tr><td colspan='7'>No runs match these filters.</td></tr>"
        )
        + "</tbody></table></div>"
        + f"<footer><span>Showing {offset + 1 if total else 0}–{offset + len(rows)} of {total} runs</span><nav aria-label='Results pages'>"
        + "".join(pagination)
        + "</nav></footer></section>"
        + "<section class='rh-details'>"
        + details(chosen, st.results_dir, zone)
        + "</section></div></article></div>"
    )


SCRIPT = r"""
let historyRequest, historySearchTimer;
async function loadRunHistory(host, force=false) {
  const query = location.search;
  if (!force && host.dataset.query === query && host.firstChild) return;
  historyRequest?.abort();
  const request = new AbortController();
  historyRequest = request;
  host.dataset.query = query;
  if (!host.querySelector('#rh-root')) host.textContent = 'Loading run history…';
  host.setAttribute('aria-busy', 'true');
  try {
    const response = await fetch('/strategies/history/panel' + query, {signal:request.signal});
    if (!response.ok) throw Error();
    const root = new DOMParser().parseFromString(await response.text(), 'text/html').getElementById('rh-root');
    if (!root) throw Error();
    if (request.signal.aborted) return;
    const typing = host.contains(document.activeElement) && document.activeElement.name === 'q';
    const cursor = typing && document.activeElement.selectionStart;
    host.replaceChildren(root);
    const filters = root.querySelector('#rh-filters');
    if (typing) { filters.q.focus({preventScroll:true}); filters.q.setSelectionRange(cursor,cursor); }
    root.querySelectorAll('[data-theme-choice]').forEach(b => b.setAttribute('aria-pressed',b.dataset.themeChoice === document.documentElement.dataset.theme));
    function navigate(url, replace=false) {
      clearTimeout(historySearchTimer);
      if (location.href !== new URL(url,location.href).href) history[replace?'replaceState':'pushState'](null,'',url);
      loadRunHistory(host,true);
    }
    function apply(replace=false) {
      const params = new URLSearchParams(new FormData(filters));
      params.set('tab','history');
      params.set('sort',root.querySelector('[name=sort]').value);
      if (params.get('range') !== 'custom') {params.delete('start');params.delete('end');}
      for (const [key,value] of [...params]) if (!value) params.delete(key);
      navigate('/strategies?'+params,replace);
    }
    filters.onsubmit = e => {e.preventDefault();apply();};
    filters.onchange = e => { if (e.target.name !== 'q') apply(); };
    filters.q.oninput = () => {
      historyRequest?.abort();
      clearTimeout(historySearchTimer);
      historySearchTimer = setTimeout(() => {if (root.isConnected && !host.hidden) apply(true);},350);
    };
    root.querySelector('[name=sort]').onchange = () => apply();
    root.onclick = async event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button) return;
      const target = event.target;
      const tab = target.closest('[data-detail]');
      if (tab) {
        root.querySelectorAll('[data-detail]').forEach(b => b.setAttribute('aria-pressed',b===tab));
        root.querySelectorAll('[data-detail-panel]').forEach(p => p.hidden = p.dataset.detailPanel !== tab.dataset.detail);
        return;
      }
      if (target.closest('[data-refresh]')) { loadRunHistory(host,true); return; }
      const copy = target.closest('[data-copy]');
      if (copy) {
        try {await navigator.clipboard.writeText(copy.dataset.copy);copy.textContent='Copied run ID';}
        catch { window.prompt('Copy run ID', copy.dataset.copy); } return;
      }
      const link = target.closest('a[data-history-link]');
      if (link) {event.preventDefault(); navigate(link.href);return;}
      const row = target.closest('tr[data-select]');
      if (row && !target.closest('a,summary,details,button,form')) navigate(row.dataset.select);
    };
    const performance = root.querySelector('[data-performance]');
    if (performance) {
      try {
        const result = await fetch('/strategies/history/performance?rid='+encodeURIComponent(performance.dataset.performance),{signal:request.signal});
        if (!result.ok || result.redirected) throw Error();
        performance.innerHTML = await result.text();
      } catch { if (!request.signal.aborted) performance.textContent='Performance is unavailable. Refresh to retry.'; }
    }
  } catch {
    if (request.signal.aborted) return;
    delete host.dataset.query;
    host.innerHTML = "<p role='alert'>Run history could not be loaded. <a href='/strategies?tab=history'>Retry</a> · <a href='/strategies'>Strategies</a></p>";
  } finally { if (historyRequest === request) host.removeAttribute('aria-busy'); }
}
"""

CSS = """
#rh-root{max-width:1360px;margin-inline:auto}
body:has(.studio-page[data-view=history]) :is(.sd-hero,main>p.muted){display:none}
.studio-page[data-view=history]>:not(#rh-host){display:none!important}
#rh-root .rh-title{display:flex;align-items:center;justify-content:space-between}
#rh-root .ss-main{padding:0 0 28px}
#rh-root .ss-brand{padding:0 5px 16px;margin:0 4px 17px;gap:13px;border-bottom:1px solid var(--ss-border)}
#rh-root .ss-side nav a{padding:10px 13px}
#rh-root .ss-side footer{border:1px solid var(--ss-border);border-radius:10px;padding:14px}
.rh-bar{height:60px;display:flex;align-items:center;gap:16px;border-bottom:1px solid var(--ss-border)}
.rh-bar strong{font-size:29px;letter-spacing:-1px;margin-right:auto}.rh-bar>details{position:relative}.rh-bar summary{display:flex;align-items:center;gap:6px;list-style:none;cursor:pointer;font-weight:600;margin:0}.rh-bar summary i{display:grid;place-items:center;border-radius:100%;width:30px;height:30px;font-style:normal;background:var(--ss-dim)}#rh-root .rh-bell{display:grid;place-items:center}.rh-bar .theme-switch{flex:none}.rh-bar>details form{position:absolute;right:0;z-index:2;background:var(--ss-surface);padding:8px}
#rh-root .theme-switch{display:flex;gap:2px}#rh-root .theme-switch button[aria-pressed=true]{background:var(--ss-green);color:#0a2822}
#rh-root .ss-tabs{display:flex;gap:0;margin:0 0 17px;border-bottom:1px solid var(--ss-border);overflow:auto}
#rh-root .ss-tabs a{border:0;border-bottom:3px solid transparent;flex:1 0 auto;white-space:nowrap;height:52px;border-radius:0;background:none;box-shadow:none}
#rh-root .ss-tabs a[aria-current]{border-color:var(--ss-green);background:transparent;box-shadow:none;color:var(--ss-green)}
#rh-root h1{font-size:31px;margin:12px 0 1px}#rh-root h1+p{font-size:14px;margin:0 0 18px;color:var(--ss-sub)}
.rh-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px}
.rh-metrics section,.rh-filters,.rh-results,.rh-details{border:1px solid var(--ss-border);border-radius:12px;background:linear-gradient(130deg,var(--ss-surface),color-mix(in srgb,var(--ss-field) 60%,var(--ss-surface)))}
.rh-metrics section{display:flex;align-items:flex-start;gap:clamp(14px,1.9vw,32px);padding:15px clamp(10px,1.4vw,24px);min-height:103px}
#rh-root .rh-metrics svg{width:49px;height:49px;border-radius:50%;padding:12px;color:var(--ss-green);background:#279d6926}
#rh-root .rh-metrics section:nth-child(3) svg{color:#f08996;background:#cc4b5b26}
#rh-root .rh-metrics section:nth-child(4) svg{color:#bb98f6;background:#9e65d72b}
.rh-metrics strong{display:block;font-size:24px}.rh-metrics em{font-weight:500;font-style:normal;font-size:14px}
.rh-metrics section span{display:block;margin-top:3px;color:var(--ss-sub);font-size:12px}
#rh-root .rh-filters{margin:16px 0;display:grid;grid-template-columns:1.08fr .82fr 1fr 1.28fr;gap:12px 22px;padding:10px 16px}
#rh-root label{font-size:12px;color:var(--ss-sub)}
#rh-root .rh-filters label{display:grid;gap:4px}
#rh-root .rh-filters :is(input,select),#rh-root [name=sort]{margin:0;min-width:0;width:100%;height:36px;background:var(--ss-field);color:var(--sd-ink);border:1px solid var(--ss-border);border-radius:7px;padding:6px 10px;font-size:14px}
.rh-dates{grid-column:1/-1;display:flex;gap:10px}.rh-columns{display:grid;grid-template-columns:minmax(0,1.59fr) minmax(320px,1fr);align-items:stretch;gap:16px}
.rh-columns>section{min-width:0;padding-bottom:12px}
.rh-columns header,.rh-results footer{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid var(--ss-border)}
#rh-root .rh-columns h2{margin:0 auto 0 0}#rh-root .rh-results h2 small{display:inline;font-size:15px}
.rh-results header label{display:flex;align-items:center;gap:6px}
#rh-root [name=sort]{width:auto;height:31px;font-size:13px}
#rh-root [data-refresh]{color:var(--ss-green);border-color:var(--ss-green)}
.rh-scroll{overflow-x:auto;min-height:92px}
#rh-root .rh-results :is(th,td){padding:6px;font-size:13px}
#rh-root .rh-results :is(th,td):first-child{padding-left:16px}#rh-root .rh-results :is(th,td):last-child{padding-right:12px}
tr[data-select]{cursor:pointer}.rh-selected{background:var(--ss-dim);box-shadow:inset 3px 0 var(--ss-green)}
.rh-status{display:inline-block;border-radius:16px;border:1px solid var(--ss-border);padding:2px 8px;font-size:11px}.rh-status.ok{color:var(--ss-green);background:#31976418}.rh-status.error{color:#dd797d;background:#bd4f6120}
.rh-actions{display:flex;align-items:center;gap:9px}.rh-actions a{font-size:14px}.rh-actions details>summary{cursor:pointer;list-style:none;margin:0}.rh-actions details[open]>div{display:grid;gap:4px;padding:5px;background:var(--ss-field);border-radius:6px}.rh-actions details form{margin:0!important}
.rh-results footer{justify-content:space-between;border:0;font-size:12px;color:var(--ss-sub)}
#rh-root .rh-results footer nav{display:flex;gap:4px;align-items:center;border:0;padding:0}
.rh-results footer :is(a,span[aria-current]){padding:4px 9px;min-width:26px;text-align:center;border-radius:6px}.rh-results footer span[aria-current]{background:var(--ss-green);color:#071f18}
#rh-root .rh-copy{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:55%;font-size:11px}
.rh-run-title{display:flex;gap:12px;align-items:flex-start;margin:18px}
#rh-root .rh-run-title svg{color:var(--ss-green);width:26px;height:26px}
#rh-root h3{margin:0 0 5px;font-size:16px;letter-spacing:-.3px}
#rh-root .rh-detail-nav{display:flex;gap:6px;border-bottom:1px solid var(--ss-border);padding:0 10px}
#rh-root .rh-detail-nav button{border:0;border-bottom:3px solid transparent;background:none;border-radius:0;padding:10px 13px}
#rh-root .rh-detail-nav [aria-pressed=true]{color:var(--ss-green);border-color:var(--ss-green)}
.rh-details>section{margin:14px 12px;padding:13px;border:1px solid var(--ss-border);border-radius:9px}
.rh-details>p{padding:0 15px}.rh-details section p{font-size:13px;color:var(--ss-sub);overflow-wrap:anywhere}
#rh-root .rh-details>p.rh-fulllink{margin:8px 0}#rh-root .rh-details :is([data-performance],.rh-fulllink) a{color:var(--ss-green);font-size:13px}
.rh-performance{display:grid;grid-template-columns:minmax(100px,.75fr) minmax(0,1.3fr);gap:8px;align-items:center}.rh-performance dl{margin:8px 0}.rh-performance dl>div{display:flex;justify-content:space-between;gap:4px;border-bottom:1px solid var(--ss-border);padding:5px 0;font-size:11px}.rh-performance dd{margin:0;font-weight:650}
.rh-performance figure{margin:0;min-width:0}.rh-performance figcaption{font-size:10px;color:var(--ss-sub)}
#rh-root .vb-plot{--green:var(--ss-green);--benchmark:#8ca9bd;width:100%;height:auto;min-width:0}#rh-root .vb-plot path{stroke:var(--ss-border)}#rh-root .vb-plot text{fill:var(--ss-sub);font:14px system-ui}
.rh-details pre{max-height:220px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
.rh-downloads{display:flex;gap:10px;margin:12px}.rh-downloads a{display:block;flex:1;text-align:center;padding:11px 4px;border:1px solid var(--ss-green);border-radius:7px;font-weight:650}
#rh-root .rh-downloads a:first-child{color:#08231b;background:var(--ss-green)}
@media(max-width:1250px){.rh-metrics{gap:8px}.rh-metrics section{gap:10px}.rh-columns{grid-template-columns:minmax(0,1.5fr) minmax(260px,1fr)}#rh-root .rh-filters{gap:8px}}
@media(max-width:1000px){.rh-columns{grid-template-columns:minmax(0,1fr)}.rh-metrics{grid-template-columns:1fr 1fr}.rh-metrics section{min-height:80px}}
@media(max-width:850px){#rh-root .ss-brand{margin:0;border:0;padding:0}.rh-bar{height:48px}.rh-bar strong{font-size:24px}#rh-root .ss-tabs a{height:43px}.rh-results footer{gap:4px}}
@media(max-width:600px){.rh-bar{height:auto;min-height:45px;gap:6px;flex-wrap:wrap;padding:6px 0}.rh-bar strong{font-size:21px}#rh-root .theme-switch button{padding:4px}.rh-bar summary i{width:23px;height:23px}.rh-bar summary{font-size:12px}.rh-bar details{max-width:100%;overflow-wrap:anywhere}#rh-root .rh-filters{grid-template-columns:1fr 1fr}.rh-metrics section{padding:10px;gap:6px}#rh-root .rh-metrics svg{width:31px;height:31px;padding:6px}.rh-metrics strong{font-size:21px}.rh-metrics span{font-size:11px}.rh-columns header{gap:6px}.rh-dates label{min-width:0}}
"""
