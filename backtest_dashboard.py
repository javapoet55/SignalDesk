"""Performance report for saved single-symbol and equal-weight batch backtests."""

import calendar
import html
import math
import statistics
from datetime import date

CAPITAL = 100000
CSS = """
:root{color-scheme:dark;--bg:#0a1019;--panel:#121d2c;--edge:#293649;--text:#e6eef6;--muted:#a5b4c5;--green:#61dcac;--red:#ff897f;--gold:#e7be75}
:root[data-theme=light]{color-scheme:light;--bg:#f4f7f7;--panel:white;--edge:#cfdddf;--text:#142c33;--muted:#4a626a;--green:#14775b;--red:#ab3b46;--gold:#85601d}
:root[data-theme=light] main a{color:#146653}:root[data-theme=light] tbody tr:hover{background:#e7f2ee}
:root[data-theme=light] .notice{color:#815c1c}:root[data-theme=light] .switch a[aria-current]{color:white}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif;font-variant-numeric:tabular-nums}main{max-width:1480px;margin:auto;padding:26px clamp(14px,3vw,48px) 60px}
nav,.top,.legend,.switch,.strip{display:flex;align-items:center;gap:16px;flex-wrap:wrap}nav{font-size:13px;padding-bottom:20px;border-bottom:1px solid var(--edge)}a{color:#a6d9ef;text-decoration:none}a:hover{text-decoration:underline}a:focus-visible,summary:focus-visible{outline:2px solid var(--gold)}
.top{justify-content:space-between;margin:24px 0}.top h1{font-size:clamp(26px,3vw,42px);letter-spacing:-.045em;line-height:1.1;margin:4px 0 8px}.muted,small,.legend,footer{color:var(--muted)}.meta{text-align:right}.strip{gap:0;margin:18px 0}.strip div{padding:0 24px;border-left:1px solid var(--edge)}.strip div:first-child{padding-left:0;border:0}.strip b{display:block;font-size:21px}
.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:12px}.panel{min-width:0;border:1px solid var(--edge);border-radius:12px;background:var(--panel);padding:16px 18px}.half{grid-column:span 6}.month{grid-column:span 6}.stats{grid-column:span 3}.wide{grid-column:1/-1}
h2{font-size:15px;margin:0 0 14px;letter-spacing:-.015em}.legend{font-size:12px;gap:14px}.legend span:before{content:'';display:inline-block;width:14px;height:3px;margin-right:5px;vertical-align:middle;background:var(--green)}.legend .gold:before{background:var(--gold)}
svg{width:100%;height:auto;display:block}svg text{font:11px system-ui;fill:var(--muted)}.scroll{overflow:auto;max-height:65vh}.scroll thead th{position:sticky;top:0;background:var(--panel)}tbody tr:hover{background:#1b2a3d}table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{text-align:right;padding:7px 9px;border-bottom:1px solid var(--edge);font-size:13px}th{font-weight:600;color:var(--muted)}th:first-child,td:first-child{text-align:left}.heat td{text-align:center;padding:7px 3px;font-size:11px;border:2px solid var(--panel);border-radius:6px}.up{color:var(--green)}.down{color:var(--red)}dl{margin:0}dl div{display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-bottom:1px solid var(--edge)}dt{color:var(--muted)}dd{margin:0;font-weight:600}
.empty{color:var(--muted);border-left:2px solid var(--edge);padding:12px;margin:20px 0}.switch{margin:12px 0}.switch a{padding:3px 12px;border:1px solid var(--edge);border-radius:14px}.switch a[aria-current]{color:#101b22;background:var(--green)}footer,details{margin-top:16px}summary{cursor:pointer}.notice{color:#eac88c}.detail{margin:8px 0 16px}
@media(max-width:1050px){.month{grid-column:span 12}.stats{grid-column:span 6}}@media(max-width:670px){.half,.stats,.month{grid-column:1/-1}.meta{text-align:left}.strip{gap:10px}.strip div{padding:0 10px}.panel{padding:14px}.strip b{font-size:17px}}
"""


def e(value):
    return html.escape(str(value), quote=True)


def fmt(v, kind="number"):
    if v is None:
        return "—"
    if kind == "usd":
        return ("−" if v < 0 else "") + f"${abs(v):,.2f}"
    return f"{v:,.2f}" + ("%" if kind == "pct" else "")


def table(head, rows, cls=""):
    def cell(label, value):
        tone = ""
        if label in ("Return", "Net P/L"):
            tone = (
                "down"
                if str(value).startswith(("-", "−"))
                else "up"
                if value != "—"
                else ""
            )
        return f"<td class='{tone}'>{e(value)}</td>"

    return (
        f"<div class='scroll'><table class='{cls}'><thead><tr>"
        + "".join(f"<th scope='col'>{e(h)}</th>" for h in head)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join(cell(label, v) for label, v in zip(head, row)) + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def panel(title, body, size):
    return f"<section class='panel {size}'><h2>{e(title)}</h2>{body}</section>"


def empty(message):
    return f"<p class='empty'>{e(message)}</p>"


def chart(days, series, benchmark=None, draw=False):
    if not series:
        return empty("Daily valuations will appear when prices are available.")
    values = list(series) + list(benchmark or [])
    lo, hi = (
        min(values + [0]) if draw else min(values),
        max(values + [0]) if draw else max(values),
    )
    if hi == lo:
        if draw:
            lo -= 1
        else:
            lo -= 1
            hi += 1

    def x(i):
        return 67 + 458 * i / max(1, len(series) - 1)

    def y(v):
        return 15 + 145 * (hi - v) / (hi - lo)

    def line(vals):
        return " ".join(f"{x(i):.2f},{y(v):.2f}" for i, v in enumerate(vals))

    out = [
        f"<svg viewBox='0 0 540 194' role='img' aria-label='{'Drawdown in percent' if draw else 'Portfolio equity in dollars'}'>"
    ]
    for i in range(4):
        value = lo + (hi - lo) * i / 3
        label = f"{value:.1f}%" if draw else f"${value:,.0f}"
        out.append(
            f"<path d='M67 {y(value):.2f}H525' stroke='#2c3a4b'/><text x='60' y='{y(value) + 4:.2f}' text-anchor='end'>{label}</text>"
        )
    color = "#ff897f" if draw else "#61dcac"
    baseline = y(0) if draw else 160
    out.append(
        f"<polygon points='{x(0)},{baseline} {line(series)} {x(len(series) - 1)},{baseline}' fill='{color}' opacity='.14'/>"
    )
    if benchmark:
        out.append(
            f"<polyline points='{line(benchmark)}' stroke='#e7be75' stroke-width='1.7' stroke-dasharray='4 3' fill='none'/>"
        )
    out.append(
        f"<polyline points='{line(series)}' stroke='{color}' stroke-width='2.6' fill='none'/>"
    )
    for i in sorted({0, len(days) // 2, len(days) - 1}):
        anchor = "start" if i == 0 else "end" if i == len(days) - 1 else "middle"
        out.append(
            f"<text x='{x(i)}' y='185' text-anchor='{anchor}'>{e(days[i])}</text>"
        )
    return "".join(out) + "</svg>"


def heat(days, series):
    monthly = {}
    prev = CAPITAL
    for day, value in zip(days, series):
        key = day[:7]
        monthly[key] = monthly.get(key, 1) * (value / prev)
        prev = value
    if not monthly:
        return empty("Monthly returns require observed valuations.")
    rows = []
    for year in sorted({k[:4] for k in monthly}):
        cells = [f"<th scope='row'>{e(year)}</th>"]
        for month in range(1, 13):
            v = monthly.get(f"{year}-{month:02}")
            if v is None:
                cells.append("<td class='muted'>·</td>")
                continue
            pct = 100 * (v - 1)
            rgb = "32,156,113" if pct > 0 else "195,76,73" if pct < 0 else "98,114,133"
            cells.append(
                f"<td style='background:rgba({rgb},{min(0.78, 0.23 + abs(pct) / 18):.2f})'>{pct:+.1f}%</td>"
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<div class='scroll'><table class='heat'><thead><tr><th>Year</th>"
        + "".join(f"<th>{m}</th>" for m in calendar.month_abbr[1:])
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div><small>Returns cover observed dates within each month.</small>"
    )


def facts(pairs):
    return (
        "<dl>"
        + "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in pairs)
        + "</dl>"
    )


def result_html(job, refresh_path, hold_choice="", *, metrics_only=False):
    info = job["inputs"]
    result = job["result"]
    out = [
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Backtest Results · {e(info['symbol'])}</title><style>{CSS}</style></head><body><main>",
        f"<nav><a href='/strategies'>Strategies</a><a href='/strategies/results/{e(info['run'])}/'>Strategy run</a><a href='/strategies/backtests'>Backtest history</a></nav>",
        f"<header class='top'><div><small>Backtest Results</small><h1>{e(info['strategy'])} <span class='muted'>/</span> {e(info['symbol'])}</h1><div class='muted'>{e(info.get('source', 'Archived signals'))}</div></div>",
    ]
    if not result or job["error"]:
        if metrics_only:
            return {}
        out.append(
            "</header>"
            + (
                empty(job["error"])
                if job["error"]
                else f"<p>Queued or running.</p><a href='{e(refresh_path)}'>Refresh result</a>"
            )
        )
        return "".join(out) + "</main></body></html>"
    batch = "outcomes" in result
    rows = []
    row_pnl = []
    fills = []
    benchmark = None
    extra = ""
    context = ""
    if batch:
        holds = info["holds"]
        hold = next((v for v in holds if str(v) == hold_choice), holds[0])
        idx = holds.index(hold)
        start, end = info["dates"][0], info["dates"][idx + 1]
        outcomes = result["outcomes"]
        prices = info.get("prices", {})
        allocation = CAPITAL / len(outcomes)
        days = []
        equity = []
        for sym, values in sorted(outcomes.items()):
            v = values[idx]
            quotes = prices.get(sym, [None] * len(info["dates"]))
            buy, sell = quotes[0], quotes[idx + 1]
            complete = isinstance(v, (float, int))
            pnl = allocation * v / 100 if complete else None
            row_pnl.append(pnl)
            if complete:
                fills.append(dict(pnl=pnl, hold=hold))
            rows.append(
                [
                    sym,
                    "Long",
                    start,
                    fmt(buy, "usd"),
                    end,
                    fmt(sell, "usd"),
                    fmt(pnl, "usd"),
                    fmt(v if complete else None, "pct"),
                    "Closed" if complete else v,
                ]
            )
        marks = info.get("close_marks", {})
        for day, ratio in sorted(marks.items()):
            if day >= end:
                break
            if ratio is None:
                days = []
                equity = []
                break
            days.append(day)
            equity.append(CAPITAL * ratio / (1 + info["fee_bps"] / 10000))
        if len(fills) == len(outcomes) and days:
            days.append(end)
            equity.append(CAPITAL + sum(t["pnl"] for t in fills))
        context = (
            "<div class='switch'>Holding period "
            + "".join(
                f"<a href='{e(refresh_path)}?hold={v}'"
                + (" aria-current='true'" if v == hold else "")
                + f">{v} sessions</a>"
                for v in holds
            )
            + "</div>"
        )
        context += f"<p class='muted'>Signal date {e(info['signal_day'])} · Equal allocation of {fmt(allocation, 'usd')} per symbol from {fmt(CAPITAL, 'usd')}. Open this run again to submit a fresh price snapshot.</p>"
        if all(v[idx] == "Pending entry" for v in outcomes.values()):
            context += f"<p class='notice'>All {len(outcomes)} signals are pending entry on {e(start)}.</p>"
        n = len(fills)
        net = sum(t["pnl"] for t in fills) if n else None
    else:
        full = result["full_period"]
        days = result["days"]
        equity = full["equity"]
        start, end = days[0], days[-1]
        fills = full.get("fills", [])
        n = full["trades"]
        net = equity[-1] - CAPITAL
        hold = full["hold"]
        benchmark = full.get("benchmark")
        for t in fills:
            row_pnl.append(t["pnl"])
            rows.append(
                [
                    t.get("symbol", info["symbol"]),
                    "Long",
                    t["entry"],
                    fmt(t["buy"], "usd"),
                    t["exit"],
                    fmt(t["sell"], "usd"),
                    fmt(t["pnl"], "usd"),
                    fmt(t["pct"], "pct"),
                    f"{t['hold']} sessions",
                ]
            )
        extra = panel(
            "Training results",
            table(
                ["Hold (sessions)", "Trades", "Return", "Max drawdown"],
                [
                    [
                        p["hold"],
                        p["trades"],
                        fmt(p["return_pct"], "pct"),
                        fmt(-p["drawdown_pct"], "pct"),
                    ]
                    for p in result["parameters"]
                ],
            ),
            "half",
        )
        extra += panel(
            "Walk-forward validation",
            table(
                ["From", "Through", "Selected hold", "Trades", "Return"],
                [
                    [
                        f["start"],
                        f["end"],
                        f["hold"] or "—",
                        f["trades"] if f["trades"] is not None else "—",
                        fmt(f["return_pct"], "pct"),
                    ]
                    for f in result.get("folds", [])
                ],
            ),
            "half",
        )
    out.append(
        f"<div class='meta'>{e(start)} — {e(end)}<br><small>Daily · VectorBT PRO {e(result['engine'])}</small></div></header>{context}"
    )
    out.append(f"<p><a href='{e(refresh_path)}/copilot?hold={hold}'>Open AI Research Copilot →</a></p>")
    peak = CAPITAL
    dd = []
    daily = []
    prior = CAPITAL
    for val in equity:
        peak = max(peak, val)
        dd.append(100 * (val / peak - 1))
        daily.append(val / prior - 1)
        prior = val
    pnl = [t["pnl"] for t in fills]
    wins = [v for v in pnl if v > 0]
    losses = [v for v in pnl if v < 0]
    total = 100 * (equity[-1] / CAPITAL - 1) if equity else None
    if batch and len(fills) == len(result["outcomes"]):
        total = net / CAPITAL * 100
    maxdd = min(dd) if dd else None
    rate = 100 * len(wins) / len(fills) if fills else None
    annual = None
    sharpe = None
    if len(equity) > 1:
        span = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days
        exponent = (
            math.log(equity[-1] / CAPITAL) * 365.25 / span
            if span and equity[-1] > 0
            else None
        )
        if exponent is not None and exponent < 700:
            annual = 100 * math.expm1(exponent)
        sd = statistics.stdev(daily)
        if sd > 0:
            sharpe = statistics.mean(daily) / sd * math.sqrt(252)

    def mean(seq):
        return statistics.mean(seq) if seq else None

    if metrics_only:
        return dict(days=days, equity=equity, benchmark=benchmark, drawdown=dd, hold=hold,
                    start=start, end=end, total=total, sharpe=sharpe, maxdd=maxdd, rate=rate,
                    annual=annual, trades=n, wins=len(wins), losses=len(losses), rows=rows, row_pnl=row_pnl,
                    avg_win=mean(wins), avg_loss=mean(losses), expectancy=mean(pnl),
                    profit_factor=(sum(wins) / -sum(losses) if losses else math.inf if wins else None),
                    avg_hold=mean([t["hold"] for t in fills]))
    out.append(
        "<div class='strip'>"
        + "".join(
            f"<div><small>{k}</small><b>{v}</b></div>"
            for k, v in [
                ("Total return", fmt(total, "pct")),
                ("Max drawdown", fmt(maxdd, "pct")),
                ("Closed trades", str(n)),
                ("Win rate", fmt(rate, "pct")),
            ]
        )
        + "</div><div class='grid'>"
    )
    legend = (
        "<div class='legend'><span>Strategy equity</span>"
        + ("<span class='gold'>Buy &amp; hold</span>" if benchmark else "")
        + "</div>"
    )
    out.extend(
        [
            panel("Equity curve", legend + chart(days, equity, benchmark), "half"),
            panel(
                "Drawdown",
                "<small>Decline from peak equity</small>" + chart(days, dd, draw=True),
                "half",
            ),
            panel("Monthly returns", heat(days, equity), "month"),
        ]
    )
    out.append(
        panel(
            "Strategy statistics",
            facts(
                [
                    (
                        "Net profit" if not batch else "Realized net profit",
                        fmt(net, "usd"),
                    ),
                    (
                        "Profit factor",
                        fmt(sum(wins) / -sum(losses))
                        if losses
                        else "∞"
                        if wins
                        else "—",
                    ),
                    ("Average trade", fmt(mean(pnl), "usd")),
                    ("Annualized return", fmt(annual, "pct")),
                    ("Sharpe ratio", fmt(sharpe)),
                    ("Fee per side", f"{info['fee_bps']:g} bps"),
                ]
            ),
            "stats",
        )
    )
    out.append(
        panel(
            "Trade analysis",
            facts(
                [
                    ("Winning / losing", f"{len(wins)} / {len(losses)}"),
                    ("Average win", fmt(mean(wins), "usd")),
                    ("Average loss", fmt(mean(losses), "usd")),
                    ("Largest win", fmt(max(wins, default=None), "usd")),
                    ("Largest loss", fmt(min(losses, default=None), "usd")),
                    (
                        "Average hold",
                        fmt(mean([t["hold"] for t in fills])) + " sessions"
                        if fills
                        else "—",
                    ),
                    ("Long / short", f"{n} / 0"),
                ]
            ),
            "stats",
        )
    )
    out.append(
        panel(
            "Trades overview",
            table(
                [
                    "Symbol",
                    "Side",
                    "Entry date",
                    "Entry price",
                    "Exit date",
                    "Exit price",
                    "Net P/L",
                    "Return",
                    "Status" if batch else "Hold time",
                ],
                rows,
            )
            if rows
            else empty("No closed trades in this period."),
            "wide",
        )
    )
    out.append(
        extra
        + "</div><footer>Daily closing valuations · Returns include simulated fees · Sharpe uses 252 sessions and a zero risk-free rate.</footer></main></body></html>"
    )
    return "".join(out)
