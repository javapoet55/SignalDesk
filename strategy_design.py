"""Presentation for the Strategies module."""

PATHS = {
    "failure": "M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0ZM8 8l8 8m0-8-8 8",
    "database": "M3 6a9 4 0 1 0 18 0 9 4 0 1 0-18 0Zm0 0v12c0 5 18 5 18 0V6M3 12c0 5 18 5 18 0",
    "bell": "M18 8a6 6 0 0 0-12 0c0 7-3 8-3 10h18c0-2-3-3-3-10ZM10 21c1 2 3 2 4 0",
    "users": "M8 12a4 4 0 1 0 0-8 4 4 0 1 0 0 8ZM1 21v-2a7 5 0 0 1 14 0v2ZM16 4a4 4 0 0 1 0 8m2 3c4 0 5 2 5 5",
    "trash": "M3 6h18M8 6V3h8v3M5 6l1 16h12l1-16M10 10v8m4-8v8",
    "warning": "M12 2 1 22h22ZM12 9v5m0 3v1",
    "configure": "M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0ZM10 8l6 4-6 4Z",
    "scheduled": "M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0ZM12 5v7l5 3",
    "history": "M4 21v-8m6 8V4m6 17v-6m5 6V9",
    "backtests": "m2 7 10-5 10 5-10 5Zm0 5 10 5 10-5M2 17l10 5 10-5",
    "versions": "M5 7v10m0-10a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Zm0 15a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Zm2-6c8 0 12-4 12-9m0 0a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z",
    "settings": "m9 3 1-2h4l1 2 3 2 2 0 2 4-2 2v3l2 2-2 4-3-1-2 2-1 2h-4l-1-2-3-2-2 1-2-4 2-2v-3L2 9l2-4h2Zm7 9a4 4 0 1 0-8 0 4 4 0 0 0 8 0Z",
    "calendar": "M2 5h20v16H2zM7 2v6m10-6v6M2 10h20",
    "chain": "m10 15 5-6M8 9l-4 4a4 4 0 0 0 6 6l4-4m2 0 4-4a4 4 0 0 0-6-6l-4 4",
    "ok": "M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Zm-15 0 3 3 7-7",
    "play": "M6 3l15 9-15 9Z",
    "copy": "M8 8h12v13H8zM5 16H3V3h13v2",
}


def icon(key):
    return (f"<svg class='sd-icon' aria-hidden='true' viewBox='0 0 24 24' fill='none' "
            f"stroke='currentColor' stroke-width='1.65' stroke-linejoin='round' "
            f"stroke-linecap='round'><path d='{PATHS[key]}'/></svg>")


def overview(values):
    cards = "".join(f"<div class='sd-metric'>{icon(key)}<div>{title}<strong>{value:,}</strong></div></div>"
                    for (key, title), value in zip((("scheduled", "Total Strategies"), ("play", "Scheduled Runs"),
                                                   ("ok", "Completed Runs"), ("history", "Total Signals")), values))
    return ("<section class='sd-hero'><div><h1>Strategies</h1><p>Manage, run, and monitor your trading strategies.</p></div>"
            "<div class='sd-metrics'>" + cards + "</div></section>")


def schedule_options(selected):
    options = (("now", "Run now", "Execute selected strategies immediately"),
               ("weekdays", "Schedule (recurring)", "Run every weekday at the selected times"),
               ("once", "Custom time (one-time)", "Run at a specific date and time"))
    return "".join(f"<label class='sd-choice'><input type='radio' name='when' value='{key}'"
                   + (" checked" if key == selected else "") + f"><span><b>{title}</b><small>{hint}</small></span></label>"
                   for key, title, hint in options)


CSS = """
.sd-left .top2.folded>:not(h2){display:none}.sd-left .top2.folded{padding-bottom:0}.sd-left button.sd-fold{background:transparent;color:inherit;padding:0;border:0;font:inherit;cursor:pointer}.sd-left .sd-buttons button{background:transparent;color:var(--sd-ink);border:1px solid var(--sd-line);border-radius:7px}.sd-actions button[data-copy-name]{color:var(--sd-mint)}.sd-help{display:inline;margin:0}.sd-help summary{display:inline;cursor:pointer}.sd-help[open]{display:block}.sd-columns input{width:17px;height:17px;accent-color:var(--sd-mint)}.sd-rows .tvedit.add{border:1px solid var(--sd-mint)}.sd-left .shead small{font-weight:400;font-size:14px;letter-spacing:0}.sd-hero{display:flex;align-items:center;justify-content:space-between;gap:20px;margin:24px 0}.sd-hero h1{font-size:34px;letter-spacing:-1.2px;margin:0;line-height:1.15}.sd-hero p{font-size:15px;margin:6px 0 0;color:var(--sd-muted)}
.sd-metrics{display:grid;grid-template-columns:repeat(4,minmax(132px,1fr));gap:18px;width:61%}.sd-metric{display:flex;align-items:center;gap:16px;padding:10px 16px;border:1px solid var(--sd-line);border-radius:10px;background:var(--sd-card);font-size:13px}.sd-metric strong{display:block;font-size:23px;line-height:1.2;margin-top:3px}.sd-metric .sd-icon{box-sizing:content-box;padding:10px;border-radius:9px;background:color-mix(in srgb,var(--sd-mint) 13%,transparent);color:var(--sd-mint)}
.studio-page[data-view=configure] #go{grid-template-columns:minmax(0,2.62fr) minmax(295px,1fr);gap:24px}.sd-left,.sd-right{display:grid;min-width:0;gap:18px;align-content:start}
.sd-left .card,.sd-right .card,.sd-right .selbar{background:var(--sd-card);border:1px solid var(--sd-line);border-radius:12px;box-shadow:none;margin:0;padding:16px}
.sd-left .top2{display:grid;align-items:end;grid-template-columns:1.6fr 1.5fr 1fr 1fr;gap:10px 24px;padding:0 24px 20px}
.sd-left .top2 h2,.sd-right h2,.sd-left .shead{margin:0;color:var(--sd-ink);font-size:17px;letter-spacing:-.4px}.sd-left .top2 h2,.sd-right h2{display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--sd-line);padding:0 4px 12px}.sd-left .top2 h2{padding:14px 0 12px}
.sd-left .top2 input,.sd-left .top2 select{box-sizing:border-box;min-width:0;height:34px;padding:5px 10px;color:var(--sd-ink);background:var(--bg);border:1px solid var(--sd-line);border-radius:7px}.sd-left .cpick .row{margin:0}.sd-left .cpick input,.sd-left .cpick select{width:50%}.sd-left .run-filters{grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;padding:8px 0 0}.sd-left .chint{margin:0}.sd-left .run-filters legend{font-weight:650}.sd-left .slist{padding:0;overflow:hidden}
.sd-left .shead{display:flex;align-items:center;gap:12px;padding:12px 18px}.shead .sd-buttons{margin-left:auto;display:flex;gap:14px;align-items:center}.shead .sd-buttons button{padding:4px 10px}.sd-rows{padding:0 15px 12px;overflow:auto}
.sd-rows .srow,.sd-columns{display:grid;grid-template-columns:36px minmax(165px,2.3fr) minmax(70px,1fr) minmax(90px,1fr) minmax(68px,.8fr) minmax(145px,auto);align-items:center;gap:12px;min-width:690px;padding:5px 10px;font-size:14px;border-bottom:1px solid var(--sd-line)}
.sd-columns{color:var(--sd-muted);font-size:12px;padding-block:8px}.sd-columns span:last-child{text-align:right}.sd-rows .srow .nm{min-width:0}.sd-rows .srow .t1,.sd-rows .srow .t2{display:block;line-height:1.35}.sd-actions{display:flex;align-items:center;justify-content:flex-end;gap:7px}.sd-actions button,.sd-actions a{padding:3px 5px;min-width:0}.sd-rows .sd-actions button{min-height:26px;min-width:25px;padding:3px}.sd-actions svg{width:16px;height:16px}.sd-actions .del{color:var(--down)}
.sd-right .selbar{position:static!important}.sd-right .when,.schedule-editor .when{display:block;margin:0;padding:0;border:0}
.when .sd-choice{display:flex;flex-direction:row;align-items:flex-start;gap:12px;cursor:pointer;margin:14px 0}.sd-choice input{margin:3px 0;width:20px;height:20px;accent-color:var(--sd-mint)}.sd-choice b,.sd-choice small{display:block;font-size:14px;color:var(--sd-ink)}.sd-choice small{color:var(--sd-muted);font-size:13px;margin-top:2px}
.when .go{display:block;margin:20px 0 0}.when .go button{width:100%;min-height:44px}.sd-right h2>a{margin-left:auto;font-size:12px;letter-spacing:0}.sd-right .sd-item{padding:12px 5px;border-bottom:1px solid var(--sd-line);display:block}.sd-right .sd-item:last-child{border:0;padding-bottom:3px}.sd-item small{display:block;color:var(--sd-muted)}.sd-item em{float:right;color:var(--sd-mint);font:12px system-ui;border:1px solid var(--sd-line);border-radius:6px;padding:4px}
@media(max-width:1100px){.sd-metrics{width:65%;gap:7px;grid-template-columns:repeat(4,minmax(0,1fr))}.sd-metric{gap:7px;padding:8px}.sd-metric .sd-icon{padding:6px}.studio-page[data-view=configure] #go{grid-template-columns:minmax(0,2fr) minmax(245px,1fr);gap:12px}.sd-left .top2{grid-template-columns:1fr 1fr}}
@media(max-width:760px){.sd-left .top2.folded>:not(h2){display:none}.sd-left .top2.folded{padding-bottom:0}.sd-left button.sd-fold{background:transparent;color:inherit;padding:0;border:0;font:inherit;cursor:pointer}.sd-left .sd-buttons button{background:transparent;color:var(--sd-ink);border:1px solid var(--sd-line);border-radius:7px}.sd-actions button[data-copy-name]{color:var(--sd-mint)}.sd-help{display:inline;margin:0}.sd-help summary{display:inline;cursor:pointer}.sd-help[open]{display:block}.sd-columns input{width:17px;height:17px;accent-color:var(--sd-mint)}.sd-rows .tvedit.add{border:1px solid var(--sd-mint)}.sd-left .shead small{font-weight:400;font-size:14px;letter-spacing:0}.sd-hero{display:block;margin:18px 0}.sd-metrics{width:100%;grid-template-columns:1fr 1fr;margin-top:15px}.sd-hero h1{font-size:28px}.studio-page[data-view=configure] #go{grid-template-columns:minmax(0,1fr)}.sd-left .top2{padding:0 12px 14px}.sd-left .run-filters{grid-template-columns:1fr 1fr}.sd-left .shead{flex-wrap:wrap}.sd-left .shead .sd-buttons{margin-left:0}.sd-left .card.slist{overflow:hidden}.sd-header .account-controls{display:flex;width:auto}}
"""
