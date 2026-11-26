"""Owner-confirmed corporate actions; append-only provenance and cash/quantity changes."""

import re
from decimal import Decimal as D

from .ledger import SHARE, money, number, short
from .market import Problem, symbol


def apply_action(ledger, owner, pid, data):
    ticker = symbol(data.get("symbol"))
    kind = data.get("kind")
    reference = short(data.get("reference", ""), 250)
    key = short(data.get("request_key", ""), 80)
    value = number(data.get("value"), "split multiplier or dividend per share", SHARE)
    if (
        kind not in ("split", "dividend")
        or not reference
        or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key)
        or data.get("confirm_symbol") != ticker
    ):
        raise Problem("Provide source reference, action type and symbol confirmation.")
    with ledger.connection(True) as db:
        p = ledger.account(db, owner, pid, True)
        prior = db.execute(
            "SELECT * FROM paper_corporate_actions WHERE pid=? AND request_key=?",
            (pid, key),
        ).fetchone()
        if prior:
            if (ticker, kind, str(value), reference) != (
                prior["symbol"],
                prior["kind"],
                prior["value"],
                prior["reference"],
            ):
                raise Problem("Request key already used.", 409)
            return dict(prior)
        pos = db.execute(
            "SELECT * FROM paper_positions WHERE pid=? AND symbol=?", (pid, ticker)
        ).fetchone()
        if not pos:
            raise Problem("There is no open position in this symbol.")
        qty = D(pos["qty"])
        new_qty = qty * value if kind == "split" else qty
        if new_qty != new_qty.quantize(SHARE):
            raise Problem("Split requires an unsupported fractional-share precision.")
        cash_delta = money(qty * value) if kind == "dividend" else D(0)
        balance = D(p["cash"]) + cash_delta
        now = ledger.now()
        if kind == "split":
            from .exits import cancel

            cancel(ledger, db, pid, "Stock split changed the holding.", symbol=ticker)
            db.execute(
                "UPDATE paper_positions SET qty=? WHERE pid=? AND symbol=?",
                (str(new_qty), pid, ticker),
            )
        else:
            db.execute(
                "INSERT INTO paper_cash_ledger(pid,delta,balance,created) VALUES(?,?,?,?)",
                (pid, str(cash_delta), str(balance), now),
            )
        db.execute(
            "UPDATE paper_portfolios SET cash=?,version=version+1 WHERE id=?",
            (str(balance), pid),
        )
        db.execute(
            "INSERT INTO paper_corporate_actions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                key,
                ticker,
                kind,
                str(value),
                str(qty),
                str(new_qty),
                str(cash_delta),
                reference,
                now,
            ),
        )
        ledger.audit(
            db,
            pid,
            key,
            kind,
            {
                "symbol": ticker,
                "before": str(qty),
                "after": str(new_qty),
                "cash": str(cash_delta),
                "reference": reference,
            },
        )
        return dict(
            db.execute(
                "SELECT * FROM paper_corporate_actions WHERE pid=? AND request_key=?",
                (pid, key),
            ).fetchone()
        )
