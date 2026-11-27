"""VectorBT PRO indicator page: technical indicators on the daily Finviz prices saved with a strategy run.

A separate page with its own routes. It reads a saved run's price file and never changes it, and its
jobs are marked kind="indicators" so they are not listed with, or run as, backtests.
"""

import csv
import io
import json
import math
import os
import re
import time
from urllib.parse import urlencode

import backtests
import strategies as sm
import vectorbt_workspace as vb
from backtest_dashboard import e as esc
from strategy_design import icon

SOURCE = "Finviz daily prices"
MAX_SYMBOLS = 10      # Must match vbt_service.indicators.MAX_SYMBOLS.
MAX_INDICATORS = 12   # Must match vbt_service.indicators.MAX_INDICATORS.
MAX_BARS = 600        # Must match vbt_service.indicators.MAX_BARS.
DEFAULT_SESSIONS = 126  # About six months shown when no dates are chosen.
POLL_SECONDS = 3
POLL_LIMIT = 40
SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,14}")

# name -> title, where it is drawn, whether several windows may be listed, and its parameters as
# (key, form label, default, minimum, maximum, whole number). The limits must match vbt_service.indicators.PARAMS.
CATALOG = {
    "sma": dict(title="Simple moving average (SMA)", panel="price", many=True,
                params=(("window", "Days", 20, 2, 250, True),)),
    "ema": dict(title="Exponential moving average (EMA)", panel="price", many=True,
                params=(("window", "Days", 20, 2, 250, True),)),
    "bbands": dict(title="Bollinger Bands", panel="price", many=False,
                   params=(("window", "Days", 20, 2, 250, True), ("alpha", "Width (standard deviations)", 2, 0.5, 5, False))),
    "rsi": dict(title="Relative Strength Index (RSI)", panel="rsi", many=False,
                params=(("window", "Days", 14, 2, 100, True),)),
    "macd": dict(title="MACD", panel="macd", many=False,
                 params=(("fast_window", "Fast", 12, 2, 100, True), ("slow_window", "Slow", 26, 3, 250, True),
                         ("signal_window", "Signal", 9, 2, 100, True))),
    "atr": dict(title="Average True Range (ATR)", panel="atr", many=False,
                params=(("window", "Days", 14, 2, 100, True),)),
    "obv": dict(title="On-Balance Volume (OBV)", panel="obv", many=False, params=()),
}
DEFAULT_ON = ("sma", "rsi")
DEFAULT_TEXT = {("sma", "window"): "20, 50"}
COLORS = ("#c88720", "#4f86c6", "#8a63d2", "#d1495b", "#2a9d8f", "#7a7a7a")


def field(name, key):
    return f"{name}_{key}"


def number(text, name, label, low, high, whole):
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    title = CATALOG[name]["title"]
    if not math.isfinite(value) or (whole and value != int(value)) or not low <= value <= high:
        kind = "a whole number" if whole else "a number"
        raise ValueError(f"{title}: enter {label.lower()} as {kind} from {low:g} to {high:g}.")
    return int(value) if whole else value


def parse_specs(fields):
    """The ticked indicators as engine requests. A moving average with several windows becomes one request each."""
    specs = []
    for name, item in CATALOG.items():
        if not fields.get("use_" + name):
            continue
        choices = [{}]
        for key, label, default, low, high, whole in item["params"]:
            text = (fields.get(field(name, key)) or "").strip() or format(default, "g")
            parts = [p.strip() for p in text.split(",") if p.strip()] if item["many"] else [text]
            values = list(dict.fromkeys(number(p, name, label, low, high, whole) for p in parts))
            choices = [dict(choice, **{key: value}) for choice in choices for value in values]
        for params in choices:
            if name == "macd" and params["fast_window"] >= params["slow_window"]:
                raise ValueError("MACD: the fast window must be shorter than the slow window.")
            specs.append(dict(name=name, params=params))
    if not specs:
        raise ValueError("Tick at least one indicator.")
    if len(specs) > MAX_INDICATORS:
        raise ValueError(f"That is {len(specs)} indicators. Choose at most {MAX_INDICATORS} at a time.")
    return specs


def parse_symbols(text):
    symbols = list(dict.fromkeys(s.strip().upper() for s in (text or "").split(",") if s.strip()))
    if not symbols:
        raise ValueError("Enter at least one symbol.")
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError(f"Enter at most {MAX_SYMBOLS} symbols at a time.")
    bad = [s for s in symbols if not SYMBOL_RE.fullmatch(s)]
    if bad:
        raise ValueError(f"{bad[0][:20]} is not a valid symbol.")
    return symbols


def price_history(st, rid):
    """The saved daily prices of one completed run: {day: {symbol: bar}}. Read only."""
    folder = st.results_dir / rid
    cache = folder / "history_cache.json"
    if not sm.RESULT_RE.fullmatch(rid or "") or folder.is_symlink() or not sm.regular_file(cache, folder):
        raise ValueError("Choose a saved strategy run with daily prices.")
    meta = sm.read_meta(folder)
    if meta.get("status") != "ok" or meta.get("mode") != "close" or meta.get("candles", "1d") != "1d":
        raise ValueError("Choose a saved strategy run with daily prices.")
    try:
        history = json.loads(cache.read_text()).get("history")
    except (OSError, ValueError):
        history = None
    if not isinstance(history, dict) or not history:
        raise ValueError("This run's saved prices could not be read.")
    return history


def positive(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else None


def bars_for(history, symbol, end):
    """The symbol's daily bars up to the end day, oldest first, limited to the most recent MAX_BARS."""
    bars = []
    for day in sorted(history):
        raw = history[day].get(symbol) if day <= end and isinstance(history[day], dict) else None
        close = positive(raw.get("c")) if isinstance(raw, dict) else None
        if close is None:
            continue
        volume = raw.get("v")
        bars.append(dict(day=day, open=positive(raw.get("o")), high=positive(raw.get("h")), low=positive(raw.get("l")), close=close,
                         volume=volume if isinstance(volume, (int, float)) and not isinstance(volume, bool)
                         and math.isfinite(volume) and volume >= 0 else None))
    return bars[-MAX_BARS:]


def prepare(st, fields):
    """Check the form and build the indicator request from the run's saved prices."""
    started = time.monotonic()
    specs = parse_specs(fields)
    symbols = parse_symbols(fields.get("symbols"))
    start, end = (fields.get("start") or "").strip(), (fields.get("end") or "").strip()
    if bool(start) != bool(end):
        raise ValueError("Choose both dates, or leave both blank for the most recent six months.")
    for day in (start, end):
        if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("Enter the dates as YYYY-MM-DD.")
    if start and start > end:
        start, end = end, start
    history = price_history(st, fields.get("rid", ""))
    end = end or max(history)
    series, missing = {}, []
    for symbol in symbols:
        bars = bars_for(history, symbol, end)
        if len(bars) < 2:
            missing.append(symbol)
        else:
            series[symbol] = bars
    if missing:
        raise ValueError(f"No saved daily prices for {', '.join(missing)} in this run. Choose symbols the run priced.")
    longest = max(series.values(), key=len)
    first = start or longest[-min(DEFAULT_SESSIONS, len(longest))]["day"]
    if all(bars[-1]["day"] < first for bars in series.values()):
        raise ValueError(f"The saved prices end before {first}. Choose earlier dates.")
    return dict(kind="indicators", price_run=fields.get("rid", ""), source=SOURCE, display_from=first, indicators=specs,
                series=series, preparation_seconds=time.monotonic() - started)


def submit(st, fields):
    return backtests.service("/indicators", prepare(st, fields))["id"]


def spec_label(entry):
    params = entry.get("params") or {}
    name = entry["name"]
    if name in ("sma", "ema", "rsi", "atr"):
        return f"{name.upper()} {params.get('window', '')}".strip()
    if name == "bbands":
        return f"Bollinger {params.get('window', '')}, {params.get('alpha', 0):g}"
    if name == "macd":
        return f"MACD {params.get('fast_window', '')}/{params.get('slow_window', '')}/{params.get('signal_window', '')}"
    return name.upper()


OUTPUT_NAMES = {"upper": "upper band", "middle": "middle band", "lower": "lower band", "signal": "signal", "hist": "histogram"}


def columns(entry):
    """(heading, output key) for each line an indicator produced."""
    label = spec_label(entry)
    outputs = entry.get("outputs") or {}
    return [(label if key in ("sma", "ema", "rsi", "atr", "obv", "macd") else f"{label} {OUTPUT_NAMES.get(key, key)}", key)
            for key in outputs]


def last_value(values):
    return next((v for v in reversed(values or []) if v is not None), None)


def show(value, whole=False):
    if value is None:
        return "—"
    return f"{value:,.0f}" if whole or abs(value) >= 10000 else f"{value:,.2f}"


def chart(days, lines, *, guides=(), bounds=None, label="Indicator"):
    """A line chart. lines is [(name, values, colour, width)]; None values leave a gap (indicator warm-up)."""
    values = [v for _, series, _, _ in lines for v in series if v is not None]
    if not days or not values:
        return "<p class='vb-empty'>Not enough price history in this date range to draw this indicator.</p>"
    top, bottom, left, right = 12, 150, 70, 807
    lo, hi = bounds or (min(values + list(guides)), max(values + list(guides)))
    if lo == hi:
        lo, hi = lo - 1, hi + 1

    def x(i):
        return left + (right - left) * i / max(1, len(days) - 1)

    def y(v):
        return bottom - (v - lo) * (bottom - top) / (hi - lo)

    out = [f"<svg class='vb-plot vi-plot' viewBox='0 0 875 {bottom + 26}' role='img' aria-label='{esc(label)}'>"]
    digits = 0 if max(abs(lo), abs(hi)) >= 10000 else 2
    for i in range(5):
        value = lo + (hi - lo) * i / 4
        out.append(f"<path d='M{left} {y(value):.1f}H{right}'/><text x='62' y='{y(value) + 4:.1f}' text-anchor='end'>"
                   f"{value:,.{digits}f}</text>")
    for idx in sorted({round(i * (len(days) - 1) / 5) for i in range(6)}):
        out.append(f"<path d='M{x(idx):.1f} {top}V{bottom}'/><text x='{x(idx):.1f}' y='{bottom + 18}' text-anchor='middle'>{esc(days[idx])}</text>")
    for guide in guides:
        out.append(f"<path class='vi-guide' d='M{left} {y(guide):.1f}H{right}'/>")
    for _, series, color, width in lines:
        run = []
        for i, value in enumerate(list(series) + [None]):
            if value is not None and i < len(days):
                run.append(f"{x(i):.1f},{y(value):.1f}")
                continue
            if len(run) > 1:
                out.append(f"<polyline points='{' '.join(run)}' fill='none' style='stroke:{color}' stroke-width='{width}'/>")
            elif run:
                cx, cy = run[0].split(",")
                out.append(f"<circle cx='{cx}' cy='{cy}' r='2' style='fill:{color}'/>")
            run = []
    return "".join(out) + "</svg>"


def legend(lines):
    return "<span class='vb-legend'>" + "".join(
        f"<span class='vi-key' style='--key:{color}'>{esc(name)} <b>{esc(show(last_value(series)))}</b></span>"
        for name, series, color, _ in lines) + "</span>"


def symbol_section(symbol, data, opened):
    days, entries = data.get("days") or [], data.get("indicators") or []
    palette = iter(COLORS * 4)
    price = [("Close", data.get("close") or [], "var(--green)", 2.4)]
    panels, problems = [], []
    for entry in entries:
        if entry.get("error"):
            problems.append(f"{spec_label(entry)} could not be calculated: {entry['error']}")
            continue
        lines = [(heading, entry["outputs"][key], next(palette), 1.6) for heading, key in columns(entry)]
        panel = CATALOG.get(entry["name"], {}).get("panel")
        if panel == "price":
            if entry["name"] == "bbands":  # One colour for the three bands.
                lines = [(name, series, lines[0][2], 1.2 if "middle" not in name else 1.6) for name, series, _, _ in lines]
            price += lines
        elif panel == "rsi":
            panels.append((spec_label(entry), lines, dict(guides=(30, 70), bounds=(0, 100))))
        elif panel == "macd":
            panels.append((spec_label(entry), lines, dict(guides=(0,))))
        else:
            panels.append((spec_label(entry), lines, {}))
    body = [f"<header><h3>Price</h3>{legend(price)}</header>", chart(days, price, label=f"{symbol} closing price with overlays")]
    for title, lines, options in panels:
        body.append(f"<header><h3>{esc(title)}</h3>{legend(lines)}</header>" + chart(days, lines, label=f"{symbol} {title}", **options))
    notes = "".join(f"<p class='warn' role='alert'>{esc(text)}</p>" for text in problems)
    span = f"{days[0]} – {days[-1]} · {len(days)} sessions" if days else "No sessions in this date range"
    return (f"<details class='vb-card vi-symbol'{' open' if opened else ''}><summary><strong>{esc(symbol)}</strong> "
            f"<span>{esc(span)}</span></summary>{notes}{''.join(body)}</details>")


def latest_table(symbols):
    """The most recent value of every line, one row per symbol."""
    headings = []
    for data in symbols.values():
        for entry in data.get("indicators") or []:
            for heading, _ in columns(entry):
                if heading not in headings:
                    headings.append(heading)
    rows = []
    for symbol, data in symbols.items():
        latest = {}
        for entry in data.get("indicators") or []:
            for heading, key in columns(entry):
                latest[heading] = show(last_value(entry["outputs"][key]), whole=entry["name"] == "obv")
        days = data.get("days") or []
        rows.append(f"<tr><th scope='row'>{esc(symbol)}</th><td>{esc(days[-1] if days else '—')}</td>"
                    f"<td>{esc(show(last_value(data.get('close'))))}</td>"
                    + "".join(f"<td>{esc(latest.get(h, '—'))}</td>" for h in headings) + "</tr>")
    return ("<div class='vi-scroll'><table><thead><tr><th>Symbol</th><th>Last day</th><th>Close</th>"
            + "".join(f"<th>{esc(h)}</th>" for h in headings) + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def export_csv(symbols):
    out = io.StringIO()
    writer = csv.writer(out)
    headings = []
    for data in symbols.values():
        for entry in data.get("indicators") or []:
            headings += [h for h, _ in columns(entry) if h not in headings]
    writer.writerow(["Symbol", "Date", "Close", *headings])
    for symbol, data in symbols.items():
        lines = {heading: entry["outputs"][key] for entry in data.get("indicators") or [] for heading, key in columns(entry)}
        for i, day in enumerate(data.get("days") or []):
            cells = [lines[h][i] if h in lines and i < len(lines[h]) and lines[h][i] is not None else "" for h in headings]
            writer.writerow([symbol, day, data["close"][i] if data["close"][i] is not None else "", *cells])
    return out.getvalue().encode("utf-8")


def result_html(job, key):
    if not job:
        return ""
    if job.get("error"):
        return ("<section class='vb-card'><p class='warn' role='alert'>The indicators could not be calculated. "
                f"{esc(job['error'])}</p><p class='vi-note'>If the backtest worker has not been redeployed since this page was added, "
                "redeploy it and run this again.</p></section>")
    result = job.get("result")
    if not result:
        return ("<section class='vb-card'><p role='status'>Calculating… This page refreshes every "
                f"{POLL_SECONDS} seconds until the result is ready.</p></section>")
    symbols = result.get("symbols") or {}
    link = "/vectorbt/indicators?" + esc(urlencode({"job": key, "download": "csv"}))
    return (f"<section class='vb-card vi-latest'><header><h2>Latest values</h2><a class='vb-history-link' href='{link}'>Download CSV</a></header>"
            + latest_table(symbols)
            + f"<p class='vi-note'>Calculated by {esc(result.get('engine', 'VectorBT PRO'))} on {esc(SOURCE)}. Each indicator uses the earlier "
            "saved prices to warm up, so its first days in the chart are already valid where that history exists.</p></section>"
            + "".join(symbol_section(symbol, data, i == 0) for i, (symbol, data) in enumerate(symbols.items())))


def selected(fields, inputs):
    """Form values: what was just typed, else the settings of the job on screen, else the defaults."""
    if fields:
        return fields
    values = {}
    if inputs:
        values.update(rid=inputs.get("price_run", ""), symbols=",".join(inputs.get("symbols") or []))
        many = {}
        for entry in inputs.get("indicators") or []:
            name, item = entry.get("name"), CATALOG.get(entry.get("name"))
            if not item:
                continue
            values["use_" + name] = "1"
            for key, *_ in item["params"]:
                many.setdefault(field(name, key), []).append(format((entry.get("params") or {}).get(key, 0), "g"))
        values.update({k: ", ".join(dict.fromkeys(v)) for k, v in many.items()})
        return values
    return {"use_" + name: "1" for name in DEFAULT_ON}


def controls(runs, rid, values, enabled, cutoff):
    options = "".join(
        f"<option value='{esc(r['id'])}'" + (" selected" if r["id"] == rid else "")
        + f">{esc(r['strategy'].replace('_', ' '))} · {esc(r.get('asof', ''))} · {esc(r['id'][11:17])}</option>" for r in runs)
    boxes = []
    for name, item in CATALOG.items():
        inputs = "".join(
            f"<label>{esc(label)}<input name='{field(name, key)}' inputmode='decimal' size='7' "
            f"value='{esc(values.get(field(name, key), DEFAULT_TEXT.get((name, key), format(default, 'g'))))}' "
            f"title='{'One or more values separated by commas. ' if item['many'] else ''}{low:g} to {high:g}'></label>"
            for key, label, default, low, high, _ in item["params"])
        boxes.append(f"<fieldset class='vi-choice'><label class='vi-toggle'><input type='checkbox' name='use_{name}' value='1'"
                     + (" checked" if values.get("use_" + name) else "") + f">{esc(item['title'])}</label>{inputs}</fieldset>")
    limit = f" max='{esc(cutoff)}'" if cutoff else ""
    return (
        "<form class='vb-card vi-form' action='/vectorbt/indicators/run' method='post'>"
        f"<div class='vi-row'><label>Prices from strategy run<select name='rid' required>{options or '<option>No completed daily runs</option>'}</select></label>"
        f"<label>Symbols<input name='symbols' required value='{esc(values.get('symbols', ''))}' placeholder='Up to {MAX_SYMBOLS}, comma-separated'></label>"
        f"<label>Start<input name='start' type='date'{limit} value='{esc(values.get('start', ''))}'></label>"
        f"<label>End<input name='end' type='date'{limit} value='{esc(values.get('end', ''))}'></label></div>"
        "<small>Leave both dates blank for the most recent six months. Symbols must be ones the chosen run saved prices for"
        + (f"; this run has prices through {esc(cutoff)}" if cutoff else "") + ".</small>"
        f"<div class='vi-choices'>{''.join(boxes)}</div>"
        "<button class='vb-run' type='submit'" + ("" if runs and enabled else " disabled") + ">▶　Calculate Indicators</button></form>"
    )


def page(host, query, fields=None, error=""):
    import server

    st = host.app.strategies
    runs = vb.eligible(st)
    enabled = bool(os.environ.get("VBT_SERVICE_URL") and os.environ.get("VBT_SERVICE_TOKEN"))
    key = query.get("job", "")
    if key and not re.fullmatch(r"[a-f0-9-]{36}", key):
        return host._text(400, "Invalid indicator job ID.")
    job = None
    if not enabled:
        error = error or "VectorBT PRO is unavailable. The backtest service is not configured."
    elif key and not fields:
        try:
            job = backtests.service("/jobs/" + key)
            if (job.get("inputs") or {}).get("kind") != "indicators":
                job, error = None, "That link is a backtest, not an indicator result."
        except backtests.JobNotFound:
            error = "This indicator result was not found. It may have been removed."
        except ValueError:
            error = "Backtest service unavailable. Refresh to retry."
    if query.get("download"):
        if query["download"] != "csv" or not job or not job.get("result"):
            return host._text(400, "Select a finished indicator result to export.")
        return host._send(200, export_csv(job["result"].get("symbols") or {}), "text/csv; charset=utf-8",
                          {"Content-Disposition": f'attachment; filename="indicators-{key}.csv"'})
    inputs = (job or {}).get("inputs") or {}
    values = dict(selected(fields, inputs))
    if job and job.get("result") and not fields:
        spans = [data["days"] for data in (job["result"].get("symbols") or {}).values() if data.get("days")]
        if spans:
            values.update(start=min(days[0] for days in spans), end=max(days[-1] for days in spans))
    valid = {r["id"] for r in runs}
    rid = values.get("rid") or query.get("rid", "")
    rid = rid if rid in valid else (runs[0]["id"] if runs else "")
    cutoff = next((r.get("asof") for r in runs if r["id"] == rid), None)
    pending = bool(job and not job.get("error") and not job.get("result"))
    html = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>VectorBT PRO Indicators · Finviz Agent</title>"
        + server._THEME_INIT
        + f"<style>{server._CSS}{server._JOURNAL_DESIGN_CSS}</style></head><body class='journal-app vb-app'><main>"
        + server.render_tabs("vectorbt")
        + f"<style>{vb.CSS}{CSS}</style>"
        f"<div class='vb-content'><header class='vb-heading'>{icon('backtests')}<div><h1>VectorBT PRO Indicators</h1>"
        "<p>Moving averages, Bollinger Bands, RSI, MACD, ATR and OBV, calculated by VectorBT PRO on your saved Finviz daily prices.</p></div>"
        "<div class='vb-heading-tools'><a class='vb-history-link' href='/vectorbt'>Backtesting</a></div></header>"
        + (f"<p role='alert' class='warn'>{esc(error)}</p>" if error else "")
        + controls(runs, rid, values, enabled, cutoff)
        + result_html(job, key)
        + "</div></main><script>"
        + f"const VB_PENDING={'true' if pending else 'false'},VB_POLL_MS={int(POLL_SECONDS * 1000)},VB_POLL_LIMIT={POLL_LIMIT};"
        + vb.COMPARE_SCRIPT + SCRIPT
        + "</script></body></html>"
    )
    return host._html(400 if fields and error else 200, html)


SCRIPT = """
const viForm=document.querySelector('.vi-form');
viForm.addEventListener('submit',()=>{const b=viForm.querySelector('.vb-run');b.disabled=true;b.textContent='Calculating…'});
// A parameter box only matters while its indicator is ticked.
viForm.querySelectorAll('.vi-choice').forEach(choice=>{
  const box=choice.querySelector('input[type=checkbox]');
  const sync=()=>choice.classList.toggle('vi-off',!box.checked);
  box.addEventListener('change',sync);sync();
  choice.querySelectorAll('input:not([type=checkbox])').forEach(input=>input.addEventListener('input',()=>{box.checked=true;sync()}));
});
"""

CSS = """
.vi-form{display:block}.vi-row{display:grid;grid-template-columns:2fr 2fr 1fr 1fr;gap:12px;margin-bottom:6px}.vi-form label{display:block;font-size:13px;color:var(--sd-muted)}
.vi-form .vi-row :is(input,select){display:block;width:100%;box-sizing:border-box;margin-top:4px}.vi-form>small{display:block;color:var(--sd-muted);margin-bottom:12px}
.vi-choices{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;margin-bottom:14px}
.vi-choice{border:1px solid var(--edge);border-radius:8px;padding:10px 12px;margin:0;display:flex;flex-wrap:wrap;gap:6px 12px;align-items:flex-end;min-width:0}
.vi-choice .vi-toggle{flex:1 0 100%;display:flex;gap:8px;align-items:center;font-weight:600;color:var(--sd-ink);font-size:14px;cursor:pointer}
.vi-choice .vi-toggle input{width:16px;height:16px;margin:0;accent-color:var(--green)}.vi-choice label:not(.vi-toggle) input{display:block;width:7.5em;margin-top:3px}
.vi-choice.vi-off label:not(.vi-toggle){opacity:.55}.vi-form .vb-run{width:auto;padding-left:22px;padding-right:22px}
.vi-latest header,.vi-symbol header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:10px 0 4px}.vi-latest header{justify-content:space-between;margin-top:0}
.vi-latest h2,.vi-symbol h3{margin:0;font-size:15px}.vi-scroll{overflow:auto}.vi-latest table{width:100%;border-collapse:collapse}
.vi-latest :is(th,td){text-align:right;padding:7px 10px;white-space:nowrap}.vi-latest :is(th,td):first-child{text-align:left}.vi-latest tbody th{font:inherit;font-weight:600}
.vi-note{color:var(--sd-muted);font-size:13px;margin:10px 0 0}.vi-symbol{margin-top:16px}.vi-symbol summary{cursor:pointer;font-size:16px}.vi-symbol summary span{color:var(--sd-muted);font-size:13px;margin-left:8px}
.vi-plot polyline{stroke-linejoin:round}.vi-plot path.vi-guide{stroke:var(--sd-muted);stroke-dasharray:4 4;stroke-width:1}
.vi-key:before{content:'';display:inline-block;width:12px;height:3px;margin-right:4px;vertical-align:middle;background:var(--key)}.vi-key b{color:var(--sd-ink);font-weight:600}
@media(max-width:850px){.vi-row{grid-template-columns:1fr 1fr}}@media(max-width:510px){.vi-row{grid-template-columns:1fr}}
"""
