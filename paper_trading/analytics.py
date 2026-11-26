"""Sample the virtual account using observed marks and compute close-to-close statistics."""

import datetime as dt
import logging
import math
import os
import statistics
from decimal import Decimal as D

from finviz_prices import ET

from .ledger import money, rows
from .market import Problem, exchange, fresh, open_minute
from .policy import entitlements


def snapshot(db, owner, pid):
    v = db.valuation(owner, pid)
    now = db.now()
    if v["equity"] is None:
        raise Problem("Valuation has missing marks.")
    benchmark = db.market.quote(v["portfolio"]["benchmark"])
    for q in [benchmark, *v["quotes"].values()]:
        fresh(q, now)
    with db.connection(True) as tx:
        p = db.account(tx, owner, pid, True)
        if p["version"] != v["portfolio"]["version"]:
            return
        tx.execute(
            "INSERT OR IGNORE INTO paper_portfolio_snapshots(pid,created,equity,benchmark,version) VALUES(?,?,?,?,?)",
            (pid, now, v["equity"], benchmark["price"], p["version"]),
        )


def snapshot_loop(app, stop, log):
    from .web import service

    while not stop.wait(60):
        try:
            if not (app.settings.data_dir / "paper" / "ledger.sqlite3").exists():
                continue
            db = service(app)
            db.expire_pending()
            now = db.now()
            if not open_minute(now):
                continue
            with db.connection() as tx:
                portfolios = rows(
                    tx,
                    "SELECT id,owner,status FROM paper_portfolios WHERE status='active'",
                )
            for p in portfolios:
                if stop.is_set():
                    return
                if p["status"] != "active":
                    continue
                from .exits import check

                check(db, p["owner"], p["id"])
                recent = db.records(p["owner"], p["id"], "snapshots", 1)
                if (
                    recent
                    and recent[0]["benchmark"]
                    and now - recent[0]["created"]
                    < int(os.getenv("PAPER_SNAPSHOT_SECONDS", "900"))
                ):
                    continue
                try:
                    snapshot(db, p["owner"], p["id"])
                except Problem as err:
                    log(f"paper snapshot deferred portfolio={p['id']} reason={err}")
        except Exception:
            logging.getLogger(__name__).exception("Paper snapshot cycle failed")


def analytics(db, owner, pid):
    policy = entitlements(owner)
    cutoff = db.now() - policy["history_days"] * 86400
    with db.connection() as tx:
        p = db.account(tx, owner, pid)
        records = rows(
            tx,
            "SELECT created,equity,benchmark FROM paper_portfolio_snapshots WHERE pid=? AND created>=? ORDER BY created,id",
            (pid, cutoff),
        )
        trades = tx.execute(
            "SELECT count(*) FROM paper_transactions WHERE pid=?", (pid,)
        ).fetchone()[0]
        durations = []
        opened = {}
        for trade in tx.execute(
            "SELECT t.symbol,t.side,t.created,s.preview FROM paper_transactions t JOIN paper_signals s ON s.id=t.signal WHERE t.pid=? ORDER BY t.created,t.rowid",
            (pid,),
        ):
            import json

            preview = json.loads(trade["preview"])
            if D(preview["held_before"]) == 0:
                opened[trade["symbol"]] = trade["created"]
            if trade["side"] == "Sell" and trade["symbol"] in opened:
                durations.append((trade["created"] - opened[trade["symbol"]]) / 86400)
            if not D(preview["held_after"]):
                opened.pop(trade["symbol"], None)
    peak = capital = D(p["capital"])
    benchmark_start_equity = next(
        (D(r["equity"]) for r in records if r["benchmark"]), None
    )
    first_bench = next((D(r["benchmark"]) for r in records if r["benchmark"]), None)
    series = []
    days = {}
    for r in records:
        equity = D(r["equity"])
        peak = max(peak, equity)
        series.append(
            dict(
                r,
                return_pct=float(money(100 * (equity / capital - 1))),
                benchmark_pct=float(money(100 * (D(r["benchmark"]) / first_bench - 1)))
                if first_bench and r["benchmark"]
                else None,
                drawdown=float(money(100 * (equity / peak - 1))),
            )
        )
        days[dt.datetime.fromtimestamp(r["created"], ET).date()] = equity
    values = list(days.values())
    calendar = exchange()
    pairs = [
        (a, b)
        for a, b in zip(days, list(days)[1:])
        if calendar.is_session(str(a))
        and calendar.next_session(str(a)).date() == b
        and days[a]
    ]
    returns = [float(days[b] / days[a] - 1) for a, b in pairs]
    months = {}
    for day, equity in days.items():
        months[day.strftime("%Y-%m")] = equity
    monthly = []
    prior = capital
    for m, equity in months.items():
        monthly.append(
            {
                "month": m,
                "pct": str(money((equity / prior - 1) * 100)) if prior else None,
            }
        )
        prior = equity
    annual = None
    if days:
        elapsed = (max(days) - dt.datetime.fromtimestamp(p["created"], ET).date()).days
        if elapsed >= 365:
            annual = 100 * ((float(values[-1] / capital)) ** (365.25 / elapsed) - 1)
    sd = statistics.stdev(returns) if len(returns) >= 20 else None
    down = (
        math.sqrt(statistics.mean(min(0, r) ** 2 for r in returns))
        if len(returns) >= 20
        else None
    )

    def fmt(x, suffix=""):
        return "N/A" if x is None else f"{x:.2f}{suffix}"

    metrics = {
        "Completed fills": trades,
        "Maximum drawdown": fmt(
            min(r["drawdown"] for r in series) if len(series) > 1 else None, "%"
        ),
        "Annualized return": fmt(annual, "%"),
        "Annualized volatility": fmt(
            sd * math.sqrt(252) * 100 if sd is not None else None, "%"
        ),
        "Sharpe ratio": fmt(
            statistics.mean(returns) * math.sqrt(252) / sd if sd else None
        ),
        "Sortino ratio": fmt(
            statistics.mean(returns) * math.sqrt(252) / down if down else None
        ),
    }
    if policy["advanced"]:
        metrics["Average days held at sale"] = fmt(
            statistics.mean(durations) if durations else None
        )
        if (
            len(series) > 1
            and benchmark_start_equity
            and series[-1]["benchmark_pct"] is not None
        ):
            metrics["Benchmark-relative return"] = fmt(
                float(100 * (D(series[-1]["equity"]) / benchmark_start_equity - 1))
                - series[-1]["benchmark_pct"],
                " pp",
            )
    else:
        metrics = {"Completed fills": trades}
        monthly = []
    daily = [{"day": str(b), "pnl": str(days[b] - days[a])} for a, b in pairs]
    # Retain intraday detail for the latest week and daily closing points before it.
    old_days = {}
    recent = []
    for sample in series:
        if sample["created"] < db.now() - 7 * 86400:
            old_days[dt.datetime.fromtimestamp(sample["created"], ET).date()] = sample
        else:
            recent.append(sample)
    return {
        "daily": daily if policy["advanced"] else [],
        "series": [*old_days.values(), *recent],
        "metrics": metrics,
        "monthly": monthly,
        "daily_pnl": str(values[-1] - values[-2])
        if pairs
        and pairs[-1][1] == max(days)
        and max(days) == dt.datetime.fromtimestamp(db.now(), ET).date()
        else None,
        "method": "USD equity equals cash plus marked holdings. Drawdown uses the initial capital and subsequent equity peaks. Monthly returns compare the last available sample of successive observed months. Daily observations use the last sampled regular-session mark. Annualized volatility and ratios use 252 sessions, a zero risk-free rate and at least 20 observed returns. Annualized return needs one year. Gaps in observations can affect estimates. Simulations do not predict future performance.",
    }
