"""Pre-authorized, linked simulated sell orders on observed completed minute closes."""

import json
from decimal import Decimal as D

from .ledger import number, rows, uid
from .market import Problem, fresh


def terms(data):
    out = {}
    for key in ("gain_pct", "stop_price", "limit_price"):
        raw = data.get(key)
        if raw not in ("", None):
            out[key] = str(number(raw, key.replace("_", " "), D(".000001")))
    if out and data.get("side") != "Buy":
        raise Problem("Attach sell conditions to a buy signal.")
    if ("stop_price" in out) != ("limit_price" in out):
        raise Problem("Enter both a stop price and a sell limit price.")
    if "stop_price" in out and D(out["limit_price"]) > D(out["stop_price"]):
        raise Problem("Sell limit must be at or below the stop price.")
    return out


def levels(ticket, price):
    spec = ticket.get("exit", {})
    if "stop_price" in spec and D(spec["stop_price"]) >= price:
        raise Problem("Stop price must be below the buy price.")
    target = price * (1 + D(spec["gain_pct"]) / 100) if "gain_pct" in spec else None
    return {
        "target": format(target, "f") if target else None,
        "stop": spec.get("stop_price"),
        "floor": spec.get("limit_price"),
    }


def cancel(ledger, db, pid, reason, *, entry=None, symbol=None):
    sql = "SELECT entry FROM paper_exits WHERE pid=? AND status IN ('Armed','Stop triggered')"
    args = [pid]
    if entry is not None:
        sql += " AND entry=?"
        args.append(entry)
    if symbol is not None:
        sql += " AND symbol=?"
        args.append(symbol)
    for r in rows(db, sql, args):
        db.execute(
            "UPDATE paper_exits SET status='Canceled' WHERE entry=?", (r["entry"],)
        )
        ledger.audit(db, pid, r["entry"], "Exit canceled", reason)


def check(ledger, owner, pid):
    with ledger.connection() as db:
        ledger.account(db, owner, pid, True)
        active = rows(
            db,
            "SELECT entry,symbol FROM paper_exits WHERE pid=? AND status IN ('Armed','Stop triggered')",
            (pid,),
        )
    for rule in active:
        try:
            evaluate(ledger, owner, pid, rule)
        except Problem:
            # Missing/stale quotes or temporarily insufficient cash leave the exit pending.
            continue


def evaluate(ledger, owner, pid, rule):
    ticker, entry = rule["symbol"], rule["entry"]
    quote = ledger.market.quote(ticker)
    fresh(quote, ledger.now())
    price = number(quote["price"], "reference price", D(".000001"))
    with ledger.connection(True) as db:
        ledger.account(db, owner, pid, True)
        r = db.execute(
            "SELECT * FROM paper_exits WHERE pid=? AND entry=?", (pid, entry)
        ).fetchone()
        if (
            not r
            or r["status"] not in ("Armed", "Stop triggered")
            or quote["stamp"] <= r["last_stamp"]
        ):
            return
        stopped = r["status"] == "Stop triggered" or (
            r["stop"] and price <= D(r["stop"])
        )
        if stopped and r["status"] == "Armed":
            db.execute(
                "UPDATE paper_exits SET status='Stop triggered' WHERE entry=?", (entry,)
            )
            ledger.audit(db, pid, entry, "Stop triggered", quote)
        db.execute(
            "UPDATE paper_exits SET last_stamp=? WHERE entry=?", (quote["stamp"], entry)
        )
        ready = (
            price >= D(r["floor"])
            if stopped
            else r["target"] and price >= D(r["target"])
        )
        if not ready:
            return
        origin = json.loads(
            db.execute(
                "SELECT ticket FROM paper_signals WHERE id=?", (entry,)
            ).fetchone()[0]
        )
    v = ledger.valuation(owner, pid, ticker, True)
    observed = v["quotes"][ticker]
    if observed["stamp"] != quote["stamp"] or D(observed["price"]) != price:
        return
    t = ledger.ticket(
        {
            "side": "Sell",
            "method": "shares",
            "amount": r["qty"],
            "symbol": ticker,
            "research": origin["research"],
            "provenance": origin["provenance"],
            "notes": "Confirmed buy exit · "
            + ("stop-limit" if stopped else "percentage target"),
        }
    )
    t["entry_signal"] = entry
    x = ledger.size(t, v)
    with ledger.connection(True) as db:
        p = ledger.account(db, owner, pid, True)
        current = db.execute(
            "SELECT status,last_stamp FROM paper_exits WHERE entry=?", (entry,)
        ).fetchone()
        if (
            current["status"] not in ("Armed", "Stop triggered")
            or current["last_stamp"] != quote["stamp"]
            or p["version"] != v["portfolio"]["version"]
        ):
            return
        fresh({"stamp": x["oldest_stamp"]}, ledger.now())
        sid = uid()
        db.execute(
            "INSERT INTO paper_signals(id,pid,request_key,ticket,preview,version,status,created) VALUES(?,?,?,?,?,?,'Processing',?)",
            (
                sid,
                pid,
                "exit:" + entry,
                json.dumps(t),
                json.dumps(x),
                p["version"],
                ledger.now(),
            ),
        )
        ledger.audit(
            db, pid, sid, "Processing", {"authorized_by": entry, "quote": quote}
        )
        ledger.fill(db, pid, sid, t, x)
        db.execute(
            "UPDATE paper_exits SET status='Filled',sell=? WHERE entry=?", (sid, entry)
        )
        ledger.audit(db, pid, entry, "Exit filled", {"sell": sid})
