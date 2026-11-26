"""VectorBT research workspace for the portal's saved strategy runs and jobs."""

import csv
import io
import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode

import backtests
import dow_benchmark
import strategies as sm
from backtest_dashboard import CAPITAL, entry_label, fmt, result_html, target_rule, warmup_note
from backtest_dashboard import e as esc
from strategy_design import icon

NAV = (
    ("backtests", "Strategy Library", "/strategies"),
    ("configure", "Run Configuration", "/strategies?tab=configure"),
    ("calendar", "Scheduled Runs", "/strategies?tab=scheduled"),
    ("history", "Run History", "/strategies?tab=history"),
    ("backtests", "VectorBT PRO", "/vectorbt"),
    ("settings", "Settings", "/strategies?tab=settings"),
)
POLL_SECONDS = 30  # How often a queued backtest's page reloads itself.
POLL_LIMIT = 40    # Reloads before it stops: 20 minutes at 30 seconds.
TRADE_HEADERS = [
    "Symbol",
    "Side",
    "Entry date",
    "Entry price",
    "Exit date",
    "Exit price",
    "Net P/L",
    "Return",
    "Hold / status",
]


def eligible(st):
    return [
        r
        for r in st.results(None)
        if r.get("status") == "ok"
        and r.get("mode") == "close"
        and r.get("candles", "1d") == "1d"
        and sm.RESULT_RE.fullmatch(r.get("id", ""))
    ]


def submit(st, fields, buys):
    started = time.monotonic()
    rid = fields.get("rid", "")
    target = backtests.profit_target(fields.get("profit_target_pct"))
    stop = backtests.stop_loss(fields.get("stop_loss_pct"))
    if stop and not target:
        raise ValueError("Enter a profit target to use a stop loss.")
    try:
        fee = float(fields.get("commission") or "0") * 100  # Portal percent to engine basis points.
    except ValueError:
        raise ValueError("Enter the commission as a percentage from 0 to 1.") from None
    try:
        holds = [int(value.strip()) for value in fields.get("holds", "1,5,10").split(",")] if not target else []
    except ValueError:
        raise ValueError("Enter a holding period in whole trading days, or a comma-separated list to compare.") from None
    if target:
        holds = [backtests.target_hold(fields.get("holds"))]
    symbols = list(
        dict.fromkeys(
            s.strip().upper() for s in fields.get("symbols", "").split(",") if s.strip()
        )
    )
    if not symbols:
        raise ValueError(
            "Choose at least one symbol or enter ALL for every buy in the run."
        )
    start, end = fields.get("start", ""), fields.get("end", "")
    if bool(start) != bool(end):
        raise ValueError("Choose both dates for historical replay.")
    basket = symbols == ["ALL"] or len(symbols) > 1
    if not sm.RESULT_RE.fullmatch(rid) or (st.results_dir / rid).is_symlink():
        raise ValueError("Choose a saved strategy run.")

    def selected(folder):
        available = buys(folder)
        if symbols == ["ALL"]:
            if not available:
                raise ValueError("This run has no buy signals to select.")
            return available
        if set(symbols) - set(available):
            raise ValueError(
                "Each symbol in a basket must be a buy from the selected strategy run."
            )
        return symbols

    bar_days = backtests.candle_days(fields.get("candle_days"))
    if bar_days > 1 and not start:
        raise ValueError("Choose a date range to use 2–5 day price bars. Saved signals use the run's own daily bars.")
    if basket and not start:
        data = backtests.cohort(st, rid, holds, fee, selected, target=target)
    else:
        tickers = selected(st.results_dir / rid) if basket else symbols
        data = backtests.dataset(
            st.results_dir,
            rid,
            tickers[0] if len(tickers) == 1 else tickers,
            holds,
            fee,
            buys,
            st,
            interval=(start, end) if start else None,
            **({"candle_days": bar_days} if bar_days > 1 else {}),
        )
    data["profit_target_pct"] = target
    data["stop_loss_pct"] = stop
    return backtests.submit_job(data, started)


def report_data(job, hold):
    return result_html(job, "", hold, metrics_only=True) if job else {}


def time_taken(job):
    if not job:
        return "—"
    seconds = job.get("elapsed_seconds")
    if seconds is None:
        return "In progress" if job.get("result") is None and job.get("error") is None else "Not recorded"
    if seconds < 1:
        return "Less than 1 second"
    minutes, seconds = divmod(round(seconds), 60)
    units = [(minutes, "minute"), (seconds, "second")]
    return " ".join(f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in units if n)


def stats(m):
    return [
        ("Total trades", str(m.get("trades", "—"))),
        ("Winning trades", str(m.get("wins", "—"))),
        ("Losing trades", str(m.get("losses", "—"))),
        ("Win rate", fmt(m.get("rate"), "pct")),
        ("Average positive P/L" if m.get("profit_target_pct") else "Average win", fmt(m.get("avg_win"), "usd")),
        ("Average negative P/L" if m.get("profit_target_pct") else "Average loss", fmt(m.get("avg_loss"), "usd")),
        (
            "Profit factor",
            "∞"
            if m.get("profit_factor") == float("inf")
            else fmt(m.get("profit_factor")),
        ),
        ("Expectancy", fmt(m.get("expectancy"), "usd")),
        ("Average hold (sessions)", fmt(m.get("avg_hold"))),
    ]


def export(kind, info, m):
    output = io.BytesIO()
    if kind == "csv":
        string = io.StringIO(newline="")
        writer = csv.writer(string)
        # Display rows use "$", thousands separators and a typographic minus, which spreadsheets
        # read as text. Export the underlying numbers so the columns can be summed and sorted.
        headers = trade_headers(info)
        for column, unit in CSV_UNITS.items():
            headers[column] += unit
        writer.writerow(headers)
        values = m.get("row_values") or [None] * len(m["rows"])
        for row, numbers in zip(m["rows"], values):
            row = list(row)
            if numbers is not None:
                for column, number, places in zip(CSV_UNITS, numbers, (4, 4, 2, 4)):
                    row[column] = "" if number is None else round(number, places)
            writer.writerow(row)
        return string.getvalue().encode("utf-8-sig")
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(output)
    blocks = [
        Paragraph("VectorBT PRO Backtest Report", styles["Title"]),
        Paragraph(
            esc(info["strategy"]) + " · " + esc(info["symbol"]), styles["Heading2"]
        ),
        Paragraph(
            f"{entry_label(info)} entry · {esc(m['start'])} through {esc(m['end'])} · {'Up to ' if info.get('exit_on_target') else ''}{m['hold']} sessions · "
            f"{info['fee_bps']:g} bps per side" + (f" · Profit target {info['profit_target_pct']:g}%" if info.get("profit_target_pct") else "")
            + (f" · Stop loss {info['stop_loss_pct']:g}%" if info.get("stop_loss_pct") else ""),
            styles["BodyText"],
        ),
        Spacer(1, 14),
        Table(
            [
                ("Initial capital", fmt(CAPITAL, "usd")),
                ("Total return", fmt(m["total"], "pct")),
                ("Sharpe ratio", fmt(m["sharpe"])),
                ("Max drawdown", fmt(m["maxdd"], "pct")),
                ("CAGR", fmt(m["annual"], "pct")),
                *stats(m),
            ],
            hAlign="LEFT",
        ),
    ]
    doc.build(blocks)
    return output.getvalue()


CSV_UNITS = {3: " (USD)", 5: " (USD)", 6: " (USD)", 7: " (%)"}  # Column index in TRADE_HEADERS.
TRADE_GROUPS = {"all": "Total trades", "wins": "Winning trades", "losses": "Losing trades"}


def trade_headers(info):
    return TRADE_HEADERS + ([f"Target outcome ({info['profit_target_pct']:g}%)"] if info.get("profit_target_pct") else [])


def trade_text(info, m, group):
    rows = [
        row
        for row, outcome in zip(m["rows"], m["row_groups"])
        if outcome != "pending" and (group == "all" or outcome == group)
    ]
    heading = (
        f"{info['strategy']} · {TRADE_GROUPS[group]}\n"
        f"{m['start']} through {m['end']} · Holding period {'up to ' if info.get('exit_on_target') else ''}{holding_label(m['hold'])}\n\n"
    )
    if not rows:
        if m["trades"] and not m["rows"]:
            return heading + "Individual fills were not saved for this backtest.\n"
        return heading + "No closed trades match this list.\n"
    return (
        heading
        + "\t".join(trade_headers(info))
        + "\n"
        + "".join("\t".join(map(str, row)) + "\n" for row in rows)
    )


def skipped_note(skipped):
    names = sorted(skipped)
    listed = ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "")
    return (f"Skipped {len(names)} symbol{'s' if len(names) != 1 else ''} without complete daily prices "
            f"in this range: {listed}.")


def holding_label(hold):
    return f"{hold} trading day" + ("" if hold == 1 else "s")


def plot(days, values, benchmark=None, *, dow=None, draw=False, compact=False):
    if not values:
        return "<p class='vb-empty'>Daily valuations will appear when prices are available.</p>"
    top, bottom, left, right = 14, (98 if draw else 186), 70, 807
    if compact:
        top, bottom, left, right = 9, 108, 4, 234
    combined = list(values) + list(benchmark or []) + list(dow or []) + ([0] if draw else [])
    lo, hi = min(combined), max(combined)
    if lo == hi:
        lo -= 1

    def x(i):
        return left + (right - left) * i / max(1, len(values) - 1)

    def y(v):
        return bottom - (v - lo) * (bottom - top) / (hi - lo)

    def points(seq):
        return " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(seq))

    color = "var(--red)" if draw else "var(--green)"
    label = "Drawdown in percent" if draw else "Equity in dollars"
    out = [
        f"<svg class='vb-plot' viewBox='0 0 {290 if compact else 875} {bottom + 26}' role='img' aria-label='{label}'>"
    ]
    for i in range(5):
        value = lo + (hi - lo) * i / 4
        tick = f"{value:.1f}%" if draw else f"{value:,.0f}"
        out.append(
            f"<path d='M{left} {y(value):.1f}H{right}'/><text x='{right + 4 if compact else 62}' y='{y(value) + 4:.1f}' text-anchor='{'start' if compact else 'end'}'>{tick}</text>"
        )
    ticks = 2 if compact else 5
    for idx in sorted({round(i * (len(days) - 1) / ticks) for i in range(ticks + 1)}):
        out.append(
            f"<path d='M{x(idx):.1f} {top}V{bottom}'/><text x='{x(idx):.1f}' y='{bottom + 18}' text-anchor='{'start' if compact and not idx else 'middle'}'>{esc(days[idx][:7] if compact else days[idx])}</text>"
        )
    baseline = top if draw else bottom
    pts = points(values)
    out.append(
        f"<polygon points='{left},{baseline} {pts} {x(len(values) - 1):.1f},{baseline}' fill='{color}' opacity='.16'/>"
    )
    for series, stroke, key in ((benchmark, "var(--benchmark)", "basket"), (dow, "var(--dow)", "dow"), (values, color, "strategy")):
        if series:
            out.append(
                f"<g data-series='{key}'><polyline points='{points(series)}' fill='none' stroke='{stroke}' stroke-width='2.7'/>"
            )
            out.append(
                f"<circle cx='{x(len(series) - 1):.1f}' cy='{y(series[-1]):.1f}' r='3' fill='{stroke}'/></g>"
            )
    last = values[-1]
    tag = f"{last:.2f}%" if draw else f"${last:,.0f}"
    if not compact:
        out.append(
            f"<text class='vb-end' x='{right + 5}' y='{y(last) + 4:.1f}' style='fill:{color}'>{tag}</text>"
        )
    return "".join(out) + "</svg>"


def view_url(key, **params):
    return "/vectorbt?" + esc(urlencode({"job": key, **params}))


def links(key, m, choice):
    return "".join(
        f"<a href='{view_url(key, hold=m.get('hold', ''), window=w)}'"
        + (" aria-current='true'" if choice == w else "")
        + f">{w}</a>"
        for w in ("All", "YTD", "1Y", "3Y", "5Y")
    )


def report(job, key, m, window):
    info = job["inputs"] if job else {}
    window = window if window in ("YTD", "1Y", "3Y", "5Y") else "All"
    days = m.get("days", [])
    offset = 0
    if days and window != "All":
        end = date.fromisoformat(days[-1])
        cutoff = (
            date(end.year, 1, 1)
            if window == "YTD"
            else end - timedelta(days=365.25 * int(window[0]))
        )
        offset = next(
            (i for i, d in enumerate(days) if d >= cutoff.isoformat()), len(days) - 1
        )
    benchmark = m.get("benchmark")
    tiles = (
        ("Total Return", "total", "pct", "history"),
        ("Sharpe Ratio", "sharpe", "number", "versions"),
        ("Max Drawdown", "maxdd", "pct", "scheduled"),
        ("Win Rate", "rate", "pct", "ok"),
        ("CAGR", "annual", "pct", "history"),
    )

    def tone(field):
        v = m.get(field)
        if field not in ("total", "rate", "annual") or not v:
            return ""
        return "positive" if v > 0 else "negative"

    metrics = "".join(
        f"<article class='vb-card vb-kpi'>{icon(symbol)}<div>{name}<strong class='{tone(field)}'>{fmt(m.get(field), kind)}</strong>"
        f"<small>{esc(note)}</small></div></article>"
        for (name, field, kind, symbol), note in zip(
            tiles,
            (
                ("Buy & hold " + fmt((benchmark[-1] / CAPITAL - 1) * 100, "pct"))
                if benchmark
                else "Portfolio return",
                "Annualized",
                "Peak to trough",
                (f"{m['wins']} / {m['scored']} hit target" if m.get("profit_target_pct")
                 else f"{m.get('wins', 0)} / {m.get('trades', 0)} trades"),
                "Annualized",
            ),
        )
    )
    ranges = links(key, m, window) if days else ""
    symbol = info.get("symbol", "—")
    symbol_names = info.get("symbols") or list(info.get("prices", {}))
    basket_label = ", ".join(symbol_names[:6]) + (
        f" +{len(symbol_names) - 6}" if len(symbol_names) > 6 else ""
    )
    dow = dow_benchmark.equity(days, CAPITAL)
    legend = "<span class='vb-legend'><span class='vb-key strategy'>Strategy equity</span>"
    if benchmark:
        legend += f"<label><input type='checkbox' checked data-benchmark='basket'><span class='vb-key basket'>Buy &amp; hold · {esc(symbol)}</span></label>"
    if dow:
        legend += ("<label><input type='checkbox' checked data-benchmark='dow'><span class='vb-key dow'>Dow Jones (DJIA)</span></label>"
                   f"<a href='{dow_benchmark.SOURCE}' target='_blank' rel='noopener' aria-label='Dow Jones (DJIA) source'>Source</a>")
    elif days:
        legend += "<span>Dow Jones closes unavailable for this period</span>"
    legend += "</span>"
    equity = plot(
        days[offset:],
        m.get("equity", [])[offset:],
        benchmark[offset:] if benchmark else None,
        dow=dow[offset:],
    )
    dd = plot(days[offset:], m.get("drawdown", [])[offset:], draw=True)
    rows = stats(m)

    def facts(pairs):
        return (
            "<dl>"
            + "".join(
                f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in pairs
            )
            + "</dl>"
        )

    def stat_value(label, value):
        for group, name in TRADE_GROUPS.items():
            if label == name and m:
                return (f"<a class='vb-trade-count' aria-label='View {name.lower()} as text' "
                        f"href='{view_url(key, hold=m['hold'], trades=group)}' target='_blank' rel='noopener'>{esc(value)}</a>")
        return esc(value)

    target = m.get("profit_target_pct")
    explanations = {
        "Total trades": "Number of positions with both an entry and an exit in this result.",
        "Winning trades": "Closed trades that reached the profit target." if target else "Closed trades with positive net P/L.",
        "Losing trades": "Closed trades that missed the profit target." if target else "Closed trades with negative net P/L.",
        "Win rate": "Target hits divided by scored closed trades." if target else "Profitable closed trades divided by all closed trades.",
        "Average positive P/L": "Average dollar gain across closed trades with positive net P/L, after fees.",
        "Average negative P/L": "Average dollar loss across closed trades with negative net P/L, after fees.",
        "Profit factor": "Total gains on profitable trades divided by the absolute total losses on losing trades, after fees.",
        "Expectancy": "Total net P/L divided by the number of closed trades. This is the average dollar gain or loss per trade.",
        "Average hold (sessions)": ("Average number of trading days from entry through exit, counting the entry day."
                                    if info.get("exit_on_target") and info.get("entry_basis") != "signal_close" else "Average number of trading sessions between entry and exit."),
    }
    explanations["Average win"] = explanations["Average positive P/L"]
    explanations["Average loss"] = explanations["Average negative P/L"]

    def stat_table(pairs):
        return (
            "<table><thead><tr><th>Metric</th><th>Strategy</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td><details class='vb-info'><summary>{esc(label)} <span aria-hidden='true'>ⓘ</span></summary>"
                f"<p>{esc(explanations[label])}</p></details></td><td>{stat_value(label, value)}</td></tr>"
                for label, value in pairs
            )
            + "</tbody></table>"
        )

    config = facts(
        [
            ("Strategy", info.get("strategy", "—")),
            ("Symbols", basket_label or symbol),
            ("Price bars", "Daily" if info.get("candle_days", 1) == 1
             else f"{info['candle_days']}-day for signals, daily for trades"),
            ("Entry price", entry_label(info) if info else "Signal-day close"),
            ("Date range", m["start"] + " – " + m["end"] if m else "—"),
            ("Initial capital", fmt(CAPITAL, "usd")),
            ("Profit target", f"{info['profit_target_pct']:g}%" if info.get("profit_target_pct") else "—"),
            ("Stop loss", f"{info['stop_loss_pct']:g}%" if info.get("stop_loss_pct") else "—"),
            ("Holding period", ("Up to " if info.get("exit_on_target") else "") + holding_label(m["hold"]) if m else "—"),
            ("Signals", info.get("source", "—")),
            (
                "Engine",
                "VectorBT PRO " + str(job["result"]["engine"]) if m else "VectorBT PRO",
            ),
            ("Time taken", time_taken(job)),
        ]
    )
    if not job:
        notes = ["Choose a completed daily strategy run to start a backtest."]
    elif job.get("error"):
        notes = [job["error"]]
    elif not m:
        notes = [f"Queued or running. This page refreshes every {POLL_SECONDS} seconds until the result is ready."]
    else:
        notes = ["Backtest completed" + (
            f" · {sum('Pending' in r[8] for r in m['rows'])} pending"
            if "outcomes" in job["result"]
            else ""
        ) + "."]
        if info.get("symbols"):
            notes.append("Starting capital is split equally across the selected tickers. The ticker list is fixed for the entire replay.")
        if info.get("warmup_sessions"):
            notes.append(warmup_note(info))
        if info.get("skipped_symbols"):
            notes.append(skipped_note(info["skipped_symbols"]))
        if info.get("entry_basis") == "signal_close":
            notes.append("Entries assume a fill at the closing price used to calculate the daily signal.")
        if m.get("profit_target_pct"):
            notes.append(target_rule(info))
            if m.get("stop_loss_pct"):
                notes.append(f"{m['stopped']} of {m['trades']} closed trades were stopped out.")
            if m["unscored"]:
                notes.append(f"Incomplete daily highs left {m['unscored']} closed trades unscored.")
        if open_count := job["result"].get("full_period", {}).get("open_trades", 0):
            notes.append(f"{open_count} open at period end, valued at the last close.")
    status = " ".join(notes)
    actions = ""
    if m:
        for title, kind in (
            ("Export Results (CSV)", "csv"),
            ("Download Report (PDF)", "pdf"),
        ):
            actions += f"<a class='vb-action' href='{view_url(key, hold=m['hold'], download=kind)}'>{title}</a>"
        actions += "<a class='vb-action' id='vb-saved' href='#saved-backtests'>✓　Saved to Strategy History</a>"
        actions += f"<a class='vb-textlink' href='/strategies/backtests/{esc(key)}?hold={m['hold']}'>View full backtest report</a>"
        actions += f"<a class='vb-textlink' href='/strategies/backtests/{esc(key)}/copilot?hold={m['hold']}'>Open AI Research Copilot →</a>"
    elif job and not job.get("error"):
        actions = f"<a class='vb-action' href='{view_url(key)}'>Refresh result</a>"
    holds = ""
    if m and "prices" in info and not info.get("exit_on_target"):
        holds = (
            "<div class='vb-periods'>Hold "
            + " ".join(
                f"<a href='{view_url(key, hold=h)}'"
                + (" aria-current='true'" if h == m["hold"] else "")
                + f">{h} session{'' if h == 1 else 's'}</a>"
                for h in info["holds"]
            )
            + "</div>"
        )
    return (
        f"<div class='vb-kpis'>{metrics}</div><div class='vb-grid'><div><section class='vb-card vb-charts'>"
        f"<header><h2>Equity Curve</h2>{legend}<span class='vb-ranges'>{ranges}</span></header>{equity}"
        f"<h2 class='vb-divider'>Drawdown</h2>{dd}</section><section class='vb-card'><h2>Trade Statistics</h2>"
        f"<div class='vb-stats'>{stat_table(rows[:4])}{stat_table(rows[4:])}</div></section></div><aside>"
        f"<section class='vb-card'><h2>{icon('settings')} Backtest Configuration</h2>{config}{holds}</section>"
        f"<section class='vb-card'><h2>{icon('ok')} Results Summary</h2><p>{esc(status)}</p>{actions}</section></aside></div>"
    )


def controls(runs, rid, info, symbols, enabled, fields):
    opts = "".join(
        f"<option value='{esc(r['id'])}'"
        + (" selected" if r["id"] == rid else "")
        + f">{esc(r['strategy'].replace('_', ' '))} · {esc(r.get('asof', ''))} · {esc(r['id'][11:17])}</option>"
        for r in runs
    )
    value = fields.get("symbols")
    if value is None:
        selected_symbols = info.get("symbols") or list(info.get("prices", {}))
        value = ",".join(selected_symbols) if selected_symbols else info.get("symbol", "ALL")
        if selected_symbols and set(selected_symbols) == set(symbols):
            value = "ALL"
    commission = fields.get("commission", format(info.get("fee_bps", 0) / 100, "g"))
    target = fields.get("profit_target_pct", info.get("profit_target_pct") or "")
    stop = fields.get("stop_loss_pct", info.get("stop_loss_pct") or "")
    bar_days = str(fields.get("candle_days", info.get("candle_days") or 1)).strip() or "1"
    bar_options = "".join(
        f"<option value='{n}'" + (" selected" if str(n) == bar_days else "") + f">{'Daily' if n == 1 else f'{n}-day'}</option>"
        for n in range(1, backtests.MAX_CANDLE_DAYS + 1)
    )
    start, end = fields.get("start", ""), fields.get("end", "")
    if not fields and info.get("source") == "Historical date-range replay":
        start, end = info.get("_start", ""), info.get("_end", "")
    if start and end:
        start, end = sorted((start, end))
    cutoff = next((r.get("asof") for r in runs if r["id"] == rid), None)
    date_limit = f" max='{esc(cutoff)}'" if cutoff else ""
    holds = fields.get("holds")
    if holds is None:
        holds = str(info["_hold"]) if "_hold" in info else ",".join(map(str, info.get("holds", [1])))
    presets = "".join(f"<option value='{h}'>{holding_label(h)}</option>" for h in (1, 2, 3, 5, 10, 20, 60))
    options = "".join(
        f"<label><input type='checkbox' value='{esc(v)}'>{esc(v)}</label>"
        for v in ["ALL", *symbols]
    )
    return (
        f"<form class='vb-card vb-controls' action='/vectorbt/run' method='post'><label>Strategy<select name='rid' required "
        f"onchange=\"location.href='/vectorbt?rid='+encodeURIComponent(this.value)\">{opts or '<option>No completed daily runs</option>'}</select></label>"
        "<details class='vb-picker'><summary>Symbols<span id='vb-chips'></span></summary><div>"
        f"<label>Symbols<input name='symbols' aria-label='Symbols' value='{esc(value)}' required placeholder='ALL or comma-separated tickers'></label>"
        f"<div class='vb-options'>{options}</div></div></details>"
        f"<label><span id='vb-hold-label'>{'Maximum hold (trading days)' if target else 'Hold (trading days)'}</span>"
        f"<input name='holds' list='vb-holds' value='{esc(holds)}' required "
        "title='Enter one holding period, or comma-separated periods to compare'></label>"
        f"<datalist id='vb-holds'>{presets}<option value='1,5,10'>Compare 1, 5 and 10 days</option></datalist>"
        f"<label>Price bars<select name='candle_days' title='Bar size used to calculate the signals. "
        f"Longer bars need a date range and a strategy that supports them. Trades are always priced on daily bars.'>{bar_options}</select></label>"
        f"<input type='hidden' name='commission' value='{esc(commission)}'>"
        f"<label>Profit target (%)<input name='profit_target_pct' type='number' min='0' step='any' value='{esc(target)}' placeholder='Optional' "
        "title='Buy at the signal close. Sell on the first target touch within the maximum hold, or at the close of its last trading day.'></label>"
        f"<label>Stop loss (%)<input name='stop_loss_pct' type='number' min='0' max='99.99' step='any' value='{esc(stop)}' placeholder='Optional' "
        "title='Used with a profit target. Sell when the price falls this far below entry. If one day touches both the stop and the target, the stop counts first.'></label>"
        "<details class='vb-dates'><summary>Date Range<span>"
        + (esc(start) + " – " + esc(end) if start and end else "Saved signal dates")
        + " ▾</span></summary>"
        f"<div><label>Historical start<input name='start' type='date'{date_limit} value='"
        + esc(start)
        + "'></label>"
        f"<label>Historical end<input name='end' type='date'{date_limit} value='"
        + esc(end)
        + "'></label>"
        "<small>Choose 4–252 trading sessions. You can enter the dates in either order. "
        + (f"This run has data through {esc(cutoff)}. " if cutoff else "")
        + "Leave both blank to use saved signals.</small>"
        "</div></details>"
        "<button class='vb-run' type='submit'"
        + ("" if runs and enabled else " disabled")
        + ">▶　Run Backtest</button></form>"
    )


def coverage_options(error, fields):
    options = []
    if error.common_range:
        start, end = error.common_range
        options.append((f"Run {error.symbol_count} selected symbol{'s' if error.symbol_count != 1 else ''} · {start} – {end}",
                        dict(fields, start=start, end=end)))
    if error.covered_symbols:
        start, end = error.requested_range
        count = len(error.covered_symbols)
        options.append((f"Run {count} fully covered symbol{'s' if count != 1 else ''} · {start} – {end}",
                        dict(fields, symbols=",".join(error.covered_symbols))))
    forms = []
    for label, values in options:
        inputs = "".join(f"<input type='hidden' name='{name}' value='{esc(values.get(name, ''))}'>"
                         for name in ("rid", "symbols", "start", "end", "holds", "profit_target_pct", "stop_loss_pct", "candle_days", "commission")
                         if name in values)
        forms.append(f"<form method='post' action='/vectorbt/run'>{inputs}<button type='submit'>{esc(label)}</button></form>")
    return "<div class='vb-recovery'>" + "".join(forms) + "</div>" if forms else ""


def page(host, query, fields=None, error=""):
    import os

    import server

    st = host.app.strategies
    runs = eligible(st)
    enabled = bool(
        os.environ.get("VBT_SERVICE_URL") and os.environ.get("VBT_SERVICE_TOKEN")
    )
    key = query.get("job", "")
    if key and not re.fullmatch(r"[a-f0-9-]{36}", key):
        return host._text(400, "Invalid backtest ID.")
    jobs, job = [], None
    rid = (fields or {}).get("rid", query.get("rid", ""))
    if enabled:
        try:
            jobs = backtests.service("/jobs")
            if not key and not rid:
                key = next(
                    (j["id"] for j in jobs if j["status"] == "Completed"),
                    jobs[0]["id"] if jobs else "",
                )
            if key:
                job = backtests.service("/jobs/" + key)
        except backtests.JobNotFound as missing:
            error = error or str(missing)
        except ValueError:
            error = error or "Backtest service unavailable. Refresh to retry."
    else:
        error = (
            error
            or "VectorBT PRO is unavailable. The backtest service is not configured."
        )
    hold = query.get("hold", "")
    m = report_data(job, hold)
    info = dict(job["inputs"]) if job else {}
    if "trades" in query:
        group = query["trades"]
        if not key or not m or group not in TRADE_GROUPS:
            return host._text(400, "Select a trade count from a completed backtest.")
        return host._send(200, trade_text(info, m, group).encode("utf-8"), "text/plain; charset=utf-8",
                          {"Content-Disposition": f'inline; filename="backtest-{key}-{m["hold"]}-{group}.txt"'})
    if query.get("download"):
        kind = query["download"]
        if kind not in ("csv", "pdf") or not m:
            return host._text(400, "Select a completed backtest to export.")
        return host._send(
            200,
            export(kind, info, m),
            "text/csv; charset=utf-8" if kind == "csv" else "application/pdf",
            {
                "Content-Disposition": f'attachment; filename="backtest-{key}-{m["hold"]}.{kind}"'
            },
        )
    valid = {r["id"] for r in runs}
    preferred = rid or info.get("run")
    rid = preferred if preferred in valid else (runs[0]["id"] if runs else "")
    symbols = server.signal_tickers(st.results_dir / rid) if rid else []
    defaults = info if rid == info.get("run") else {}
    if defaults and m:
        defaults.update(_start=m["start"], _end=m["end"], _hold=m["hold"])
    form = controls(runs, rid, defaults, symbols, enabled, fields or {})
    tabs = "".join(
        f"<a href='{url}'"
        + (" aria-current='page'" if url == "/vectorbt" else "")
        + f">{icon(key_)}{name}</a>"
        for key_, name, url in NAV
    )
    history = "".join(
        f"<tr><td><a href='{view_url(j['id'])}'>{esc(j['strategy'])}</a></td><td>{esc(j['symbol'])}</td>"
        f"<td>{esc(j['status'])}</td><td>{esc(j['created'])}</td></tr>"
        for j in jobs
    )
    html = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>VectorBT PRO Backtesting · Finviz Agent</title>"
        + server._THEME_INIT
        + f"<style>{server._CSS}{server._JOURNAL_DESIGN_CSS}</style></head><body class='journal-app vb-app'><main>"
        + server.render_tabs("vectorbt")
        + f"<style>{CSS}</style><nav class='studio-tabs' aria-label='Strategies Studio'>{tabs}</nav>"
        f"<div class='vb-content'><header class='vb-heading'>{icon('backtests')}<div><h1>VectorBT PRO Backtesting</h1>"
        "<p>Run and analyze historical backtests for your trading strategies using VectorBT PRO.</p></div>"
        f"<p class='vb-capital'>Initial capital <strong>{fmt(CAPITAL, 'usd')[:-3]}</strong></p></header>"
        + (f"<p role='alert' class='warn'>{esc(error)}</p>" if error else "")
        + (coverage_options(error, fields) if fields and isinstance(error, backtests.ReplayCoverageError) else "")
        + form
        + report(job, key, m, query.get("window", "All"))
        + "<details class='vb-card vb-history' id='saved-backtests'><summary>Saved backtest history</summary>"
        + (
            "<table><thead><tr><th>Strategy</th><th>Symbols</th><th>Status</th><th>Created</th></tr></thead><tbody>"
            + history
            + "</tbody></table>"
            if history
            else "<p>No saved backtests available.</p>"
        )
        + "</details></div></main><script>"
        + f"const VB_PENDING={'true' if job and not job.get('error') and not m else 'false'};"
        + f"const VB_POLL_MS={int(POLL_SECONDS * 1000)},VB_POLL_LIMIT={POLL_LIMIT};"
        + SCRIPT
        + "</script></body></html>"
    )
    return host._html(400 if fields and error else 200, html)


SCRIPT = """
const form=document.querySelector('.vb-controls');
form.addEventListener('input',()=>document.querySelector('.vb-recovery')?.remove(),{once:true});
const holdField=form.elements.holds, targetField=form.elements.profit_target_pct;
let holdEdited=false, targetWasActive=Number(targetField.value)>0;
holdField.addEventListener('input',()=>holdEdited=true);
function syncHold(){
  // With a profit target the hold is one editable maximum (1-60 days). Turning a target on suggests
  // the default of 7 unless the hold was typed by hand; a list such as "1,5,10" never applies.
  const active=Number(targetField.value)>0;
  if(active && ((!targetWasActive && !holdEdited) || !/^\s*\d+\s*$/.test(holdField.value))) holdField.value='7';
  targetWasActive=active;
  document.querySelector('#vb-hold-label').textContent=active?'Maximum hold (trading days)':'Hold (trading days)';
  holdField.title=active?'Sell at the target, or at the close of this trading day. Enter 1 to 60.':'Enter one holding period, or comma-separated periods to compare';
}
targetField.addEventListener('input',syncHold);
syncHold();
const dates=form.querySelector('.vb-dates'), caption=dates.querySelector('summary span');
const {start,end}=form.elements;
function updateRange(commit=false){
  let first=start.value, last=end.value;
  if(first && last && first>last){
    [first,last]=[last,first];
    if(commit){start.value=first;end.value=last}
  }
  start.required=Boolean(last);
  end.required=Boolean(first);
  caption.textContent=(first||last)?(first||'Start')+' – '+(last||'End')+' ▾':'Saved signal dates ▾';
}
updateRange();
dates.oninput=()=>updateRange();
dates.addEventListener('toggle',()=>{if(!dates.open)updateRange(true)});
const picker=form.querySelector('.vb-picker'), input=form.elements.symbols, chips=document.querySelector('#vb-chips');
const boxes=[...picker.querySelectorAll('input[type=checkbox]')];
function refreshSymbols(){
  const selected=[...new Set(input.value.split(',').map(s=>s.trim().toUpperCase()).filter(Boolean))];
  const selectedSet=new Set(selected);
  boxes.forEach(box=>box.checked=selectedSet.has(box.value));
  chips.replaceChildren();
  for(const name of selected.slice(0,3)){
    const chip=document.createElement('b');chip.textContent=name;chips.append(chip);
  }
  if(selected.length>3) chips.append(' +'+(selected.length-3));
  if(!selected.length) chips.textContent='Choose symbols';
}
refreshSymbols();
input.oninput=refreshSymbols;
boxes.forEach(box=>box.onchange=()=>{
  let values=input.value.split(',').map(v=>v.trim().toUpperCase()).filter(v=>v && v!==box.value && v!=='ALL');
  if(box.checked) values=box.value==='ALL'?['ALL']:[...values,box.value];
  input.value=values.join(',');refreshSymbols();
});
for(const pop of [picker,dates]){
  pop.addEventListener('toggle',()=>{if(pop.open)(pop===picker?dates:picker).open=false});
  document.addEventListener('click',ev=>{if(!pop.contains(ev.target))pop.open=false});
  pop.onkeydown=ev=>{if(ev.key==='Escape'){pop.open=false;pop.querySelector('summary').focus()}};
}
document.querySelector('#vb-saved')?.addEventListener('click',()=>document.querySelector('#saved-backtests').open=true);
form.addEventListener('invalid',e=>{if(dates.contains(e.target))dates.open=true;if(picker.contains(e.target))picker.open=true},true);
form.onsubmit=()=>{updateRange(true);form.querySelector('button').disabled=true;form.querySelector('button').textContent='Running…'};
// The recovery buttons post their own forms. Show that the click was taken and block a second one,
// because a repeated click cancels the request that is already being worked on.
document.querySelectorAll('.vb-recovery form').forEach(recovery=>recovery.addEventListener('submit',()=>{
  recovery.querySelector('button').textContent='Running…';
  setTimeout(()=>document.querySelectorAll('.vb-recovery button, .vb-run').forEach(button=>button.disabled=true));
}));
window.addEventListener('pageshow',e=>{if(e.persisted)location.reload()});
// A queued backtest: reload on a fixed interval, up to a limit, unless the form is being edited.
const here=new URL(location.href), polls=Number(here.searchParams.get('poll'))||0;
if(VB_PENDING){
  let editing=false;
  form.addEventListener('input',()=>editing=true);
  if(polls<VB_POLL_LIMIT) setTimeout(()=>{
    if(editing||picker.open||dates.open) return;
    here.searchParams.set('poll',polls+1);location.replace(here);
  },VB_POLL_MS);
}else if(here.searchParams.has('poll')){
  here.searchParams.delete('poll');history.replaceState(null,'',here);
}
"""


CSS = """
.vb-app{--green:var(--accent);--red:var(--down);--benchmark:#82a4b7;--dow:#c88720;--edge:var(--sd-line)}
.vb-content{min-width:0}
.vb-recovery{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.vb-recovery form{margin:0}
.vb-heading .vb-capital{margin:0 0 0 auto;text-align:right;color:var(--sd-muted);font-size:13px;white-space:nowrap}.vb-heading .vb-capital strong{display:block;font-size:19px;color:var(--sd-ink)}
.vb-heading{display:flex;align-items:center;gap:17px;margin:0 0 18px}.vb-heading>.sd-icon{width:38px;height:38px;color:var(--green)}.vb-heading h1{margin:0;font-size:29px;line-height:1.2;letter-spacing:-.8px}.vb-heading p{margin:3px 0 0;color:var(--sd-muted)}
.vb-card{background:linear-gradient(110deg,var(--sd-card),color-mix(in srgb,var(--sd-card) 88%,var(--bg)));border:1px solid var(--edge);border-radius:13px;box-sizing:border-box;padding:12px 16px;min-width:0}
.vb-controls{display:grid;grid-template-columns:1.3fr 1.5fr .78fr .85fr .8fr .8fr 1.35fr auto;align-items:end;gap:16px;margin-bottom:16px}
.vb-controls label,.vb-controls summary{display:grid;gap:6px;font-size:13px}.vb-controls :is(input,select),.vb-dates summary span{box-sizing:border-box;width:100%;min-width:0;font:inherit;font-size:14px;color:var(--sd-ink);background:var(--bg);border:1px solid var(--edge);border-radius:7px;height:38px;padding:8px 10px}
.vb-app .vb-controls summary{padding:0;margin:0;color:var(--sd-ink);font-weight:400}
.vb-app .vb-controls details{position:relative;margin:0;padding:0;border:0;background:none}
.vb-picker>div{position:absolute;top:100%;left:0;z-index:4;width:min(270px,80vw);padding:12px;background:var(--sd-card);border:1px solid var(--edge);border-radius:8px}
.vb-options{max-height:235px;overflow:auto;margin-top:8px}.vb-options label{display:flex;gap:9px;align-items:center;padding:4px}.vb-options input[type=checkbox]{width:16px;height:16px;accent-color:var(--green)}
#vb-chips{display:flex;align-items:center;gap:4px;cursor:pointer;overflow:hidden;padding:4px 8px;box-sizing:border-box;height:38px;background:var(--bg);border:1px solid var(--edge);border-radius:7px}#vb-chips:after{content:'⌄';margin-left:auto}#vb-chips b{font-size:12px;border-radius:5px;background:var(--jsoft);padding:3px 6px}
.vb-kpi strong.positive{color:var(--green)}.vb-kpi strong.negative{color:var(--red)}
.vb-dates{position:relative}.vb-dates summary{cursor:pointer}.vb-dates summary span{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.vb-dates>div{position:absolute;z-index:3;right:0;top:100%;width:min(280px,80vw);border:1px solid var(--edge);background:var(--sd-card);border-radius:8px;padding:15px;display:grid;gap:12px;box-shadow:0 8px 24px #0004}
.vb-run{min-height:42px;background:var(--btn);color:var(--btnink);border:0;border-radius:8px;padding:0 15px;font-weight:700}.vb-run:disabled{opacity:.55}
.vb-kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:16px;margin-bottom:16px}.vb-kpi{display:flex;align-items:flex-start;gap:12px;min-height:100px;padding:14px 16px}.vb-kpi .sd-icon{background:var(--jsoft);color:var(--green);border-radius:8px;padding:8px;box-sizing:content-box}.vb-kpi:nth-child(3) .sd-icon{color:var(--red);background:color-mix(in srgb,var(--red) 12%,transparent)}.vb-kpi strong{display:block;font-size:25px;line-height:1.5}.vb-kpi small{color:var(--sd-muted)}
.vb-grid{display:grid;grid-template-columns:minmax(0,2.75fr) minmax(255px,1fr);gap:16px}.vb-grid>div,.vb-grid>aside{display:flex;min-width:0;flex-direction:column;gap:15px}.vb-card h2{font-size:17px;margin:0 0 9px;letter-spacing:-.4px;display:flex;align-items:center;gap:8px}
.vb-charts header{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.vb-charts header h2{margin:0}.vb-legend{display:inline-flex;flex-wrap:wrap;gap:10px;font-size:12px;color:var(--sd-muted)}.vb-key:before{content:'';display:inline-block;width:12px;height:3px;margin-right:4px;vertical-align:middle;background:var(--green)}.vb-key.basket:before{background:var(--benchmark)}.vb-key.dow:before{background:var(--dow)}.vb-key.dow{color:inherit}.vb-trade-count{text-decoration:underline;text-underline-offset:3px}
.vb-legend label{display:inline-flex;gap:4px;align-items:center;cursor:pointer;font:inherit}.vb-legend input{margin:0;width:15px;height:15px;accent-color:var(--green)}
.vb-charts:has([data-benchmark="basket"]:not(:checked)) [data-series="basket"],.vb-charts:has([data-benchmark="dow"]:not(:checked)) [data-series="dow"]{display:none}
.vb-stats details.vb-info{border:0;margin:0;padding:0;background:none}.vb-stats .vb-info summary{list-style:none;cursor:pointer;padding:0;color:inherit;font:inherit}.vb-info summary::-webkit-details-marker{display:none}.vb-info summary span{color:var(--green)}.vb-info p{color:var(--sd-muted);max-width:33em;margin:5px 0}
.vb-ranges,.vb-periods{margin-left:auto;display:inline-flex;gap:4px;align-items:center}.vb-ranges a,.vb-periods a{font-size:12px;color:var(--sd-muted);padding:3px 9px;border-radius:12px}.vb-ranges a[aria-current],.vb-periods a[aria-current]{background:var(--btn);color:var(--btnink)}.vb-plot{display:block;width:100%;height:auto}.vb-plot text{fill:var(--sd-muted);font:12px system-ui}.vb-plot path{stroke:var(--edge);stroke-width:.7}.vb-plot .vb-end{font-size:10px;font-weight:700}.vb-divider{border-top:1px solid var(--edge);padding-top:8px}.vb-empty{padding:32px;color:var(--sd-muted)}
.vb-grid dl{margin:0}.vb-grid dl div{display:flex;justify-content:space-between;gap:7px;border-top:1px solid var(--edge);padding:4px 0;font-size:13px}.vb-grid dd{margin:0;text-align:right;font-weight:550;overflow-wrap:anywhere}.vb-grid dt{color:var(--sd-muted)}
.vb-stats{display:grid;grid-template-columns:1fr 1fr;gap:25px}.vb-stats table{width:100%;border-collapse:collapse;align-self:start}.vb-stats :is(th,td){font-size:13px;padding:5px 4px;border-width:0 0 1px;text-align:left}.vb-stats :is(th,td):last-child{text-align:right}.vb-stats dl:first-child{border-right:1px solid var(--edge);padding-right:18px}
.vb-action{display:block;text-align:center;border:1px solid var(--edge);border-radius:8px;padding:10px;color:var(--sd-ink);margin:8px 0;font-weight:600}.vb-action:first-of-type{border-color:var(--green);color:var(--green);background:var(--jsoft)}.vb-textlink{display:block;text-align:center;margin:12px 0 0}.vb-history{margin-top:16px;overflow:auto}.vb-history summary{cursor:pointer;font-weight:600}
@media(max-width:1200px){.vb-controls{grid-template-columns:repeat(4,minmax(0,1fr))}.vb-kpis{gap:8px}.vb-kpi{padding:10px;gap:6px}.vb-kpi .sd-icon{padding:4px}}
@media(max-width:850px){.vb-grid{grid-template-columns:minmax(0,1fr)}.vb-kpis{grid-template-columns:repeat(3,minmax(0,1fr))}.vb-controls{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:510px){.vb-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.vb-kpi{min-height:75px}.vb-kpi strong{font-size:21px}.vb-heading h1{font-size:23px}.vb-heading .sd-icon{width:28px}.vb-stats{display:block}.vb-stats dl:first-child{border:0;padding:0}.vb-controls{gap:9px}.vb-ranges{margin-left:0}}
"""
