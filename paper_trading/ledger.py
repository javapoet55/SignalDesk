"""Owner-scoped paper portfolios, preview reservations and atomic average-cost fills."""

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, InvalidOperation
from decimal import Decimal as D
from pathlib import Path

from .market import Problem, fresh, symbol
from .policy import entitlements

CENT = D(".01")
SHARE = D(".000001")
METHODS = ("shares", "dollars", "equity_pct", "cash_pct", "position_pct")


def number(value, label, minimum=D(0), maximum=D(1000000000000)):
    try:
        n = D(str(value))
    except (InvalidOperation, ValueError):
        raise Problem(f"Invalid {label}.") from None
    if (
        not n.is_finite()
        or not minimum <= n <= maximum
        or int(n.as_tuple().exponent) < -12
    ):
        raise Problem(f"{label} is outside the supported range.")
    return n


def money(n):
    return n.quantize(CENT, rounding=ROUND_HALF_EVEN)


def short(value, limit=2000):
    if not isinstance(value, str) or len(value) > limit:
        raise Problem("Text field exceeds its limit.")
    return value.strip()


def uid():
    return uuid.uuid4().hex


def rows(db, query, args=()):
    return [dict(r) for r in db.execute(query, args)]


class Ledger:
    def __init__(self, path, market, clock=time.time):
        self.path = Path(path)
        self.market = market
        self.clock = clock
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.commit()
                db.execute("PRAGMA journal_mode=WAL")
                sql = Path(__file__).with_name("migrations") / "001_up.sql"
                db.executescript(sql.read_text())
            if version in (0, 1):
                db.executescript(
                    (Path(__file__).with_name("migrations") / "002_up.sql").read_text()
                )
            elif version != 2:
                raise RuntimeError("Unsupported paper database version")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        finally:
            db.close()

    def now(self):
        return int(self.clock())

    def audit(self, db, pid, subject, action, details):
        db.execute(
            "INSERT INTO paper_audit_events(pid,subject,action,details,created) VALUES(?,?,?,?,?)",
            (pid, subject, action, json.dumps(details), self.now()),
        )

    def account(self, db, owner, pid, active=False):
        row = db.execute(
            "SELECT p.*,s.benchmark,s.fractional,s.fee FROM paper_portfolios p JOIN paper_portfolio_settings s ON s.pid=p.id WHERE p.owner=? AND p.id=?",
            (owner, pid),
        ).fetchone()
        if row is None:
            raise Problem("Portfolio not found.", 404)
        if active and row["status"] != "active":
            raise Problem("This portfolio is archived.", 409)
        return dict(row)

    def portfolios(self, owner):
        with self.connection() as db:
            return rows(
                db,
                "SELECT * FROM paper_portfolios WHERE owner=? ORDER BY created DESC,rowid DESC",
                (owner,),
            )

    def create(self, owner, data, db=None):
        if db is None:
            with self.connection(True) as tx:
                return self.create(owner, data, tx)
        name = short(data.get("name", ""), 80)
        capital = money(number(data.get("capital"), "initial capital", CENT))
        fee = money(number(data.get("fee", 0), "fee", maximum=D(10000)))
        benchmark = symbol(data.get("benchmark", "SPY"))
        if not name or data.get("currency", "USD") != "USD":
            raise Problem("A portfolio name and USD base currency are required.")
        limit = entitlements(owner)["portfolios"]
        if (
            db.execute(
                "SELECT count(*) FROM paper_portfolios WHERE owner=? AND status='active'",
                (owner,),
            ).fetchone()[0]
            >= limit
        ):
            raise Problem("Active portfolio limit reached.", 403)
        pid, now = uid(), self.now()
        fraction = data.get("fractional", True)
        if fraction not in (False, True, 0, 1):
            raise Problem("Invalid fractional-share setting.")
        db.execute(
            "INSERT INTO paper_portfolios VALUES(?,?,?,?,?,?,?,'active',0,?)",
            (
                pid,
                owner,
                name,
                short(data.get("description", "")),
                str(capital),
                str(capital),
                "USD",
                now,
            ),
        )
        db.execute(
            "INSERT INTO paper_portfolio_settings VALUES(?,?,?,?)",
            (pid, benchmark, int(fraction), str(fee)),
        )
        db.execute(
            "INSERT INTO paper_cash_ledger(pid,delta,balance,created) VALUES(?,?,?,?)",
            (pid, str(capital), str(capital), now),
        )
        db.execute(
            "INSERT INTO paper_portfolio_snapshots(pid,created,equity,version) VALUES(?,?,?,0)",
            (pid, now, str(capital)),
        )
        self.audit(db, pid, pid, "created", {"capital": str(capital)})
        return self.account(db, owner, pid)

    def settings(self, owner, pid, data):
        with self.connection(True) as db:
            p = self.account(db, owner, pid, True)
            action = data.get("action", "update")
            if action in ("archive", "reset"):
                if data.get("confirm_name") != p["name"]:
                    raise Problem("Type the portfolio name to confirm.")
                db.execute(
                    "UPDATE paper_portfolios SET status='archived',version=version+1 WHERE id=?",
                    (pid,),
                )
                for r in rows(
                    db,
                    "SELECT id FROM paper_signals WHERE pid=? AND status IN ('Pending','Draft')",
                    (pid,),
                ):
                    self.finish(db, pid, r["id"], "Canceled", "Portfolio archived.")
                from .exits import cancel

                cancel(self, db, pid, "Portfolio archived.")
                self.audit(db, pid, pid, "archived", {})
                if action == "reset":
                    replacement = self.create(owner, p, db)
                    self.audit(db, pid, pid, "reset", {"successor": replacement["id"]})
                    return replacement
            elif action == "update":
                name = short(data.get("name", p["name"]), 80)
                if not name:
                    raise Problem("Enter a portfolio name.")
                fee = money(number(data.get("fee", p["fee"]), "fee", maximum=D(10000)))
                fraction = data.get("fractional", p["fractional"])
                if fraction not in (True, False, 0, 1):
                    raise Problem("Invalid fractional setting.")
                db.execute(
                    "UPDATE paper_portfolios SET name=?,description=?,version=version+1 WHERE id=?",
                    (name, short(data.get("description", p["description"])), pid),
                )
                db.execute(
                    "UPDATE paper_portfolio_settings SET fee=?,fractional=? WHERE pid=?",
                    (str(fee), int(fraction), pid),
                )
                self.audit(
                    db,
                    pid,
                    pid,
                    "settings updated",
                    {"name": name, "fee": str(fee), "fractional": fraction},
                )
            else:
                raise Problem("Unknown settings action.")
            return self.account(db, owner, pid)

    def state(self, owner, pid):
        with self.connection() as db:
            p = self.account(db, owner, pid)
            positions = rows(
                db, "SELECT * FROM paper_positions WHERE pid=? ORDER BY symbol", (pid,)
            )
            realized = sum(
                (
                    D(r[0])
                    for r in db.execute(
                        "SELECT realized FROM paper_transactions WHERE pid=?", (pid,)
                    )
                ),
                D(0),
            )
            realized += sum(
                (
                    D(r[0])
                    for r in db.execute(
                        "SELECT cash_delta FROM paper_corporate_actions WHERE pid=?",
                        (pid,),
                    )
                ),
                D(0),
            )
            pending = db.execute(
                "SELECT count(*) FROM paper_signals WHERE pid=? AND status='Pending'",
                (pid,),
            ).fetchone()[0]
        return p, positions, realized, pending

    def valuation(self, owner, pid, extra="", strict=False):
        p, positions, realized, pending = self.state(owner, pid)
        quotes = {}
        for ticker in sorted(
            {r["symbol"] for r in positions} | ({extra} if extra else set())
        ):
            try:
                q = self.market.quote(ticker)
                number(q["price"], "reference price", D(".00000001"))
                if strict:
                    fresh(q, self.now())
                quotes[ticker] = q
            except Problem:
                if strict:
                    raise
        invested, cost = D(0), D(0)
        for r in positions:
            q = quotes.get(r["symbol"])
            cost += D(r["cost"])
            r.update(price=None, value=None, unrealized=None, stamp=None)
            if q:
                value = money(D(q["price"]) * D(r["qty"]))
                invested += value
                r.update(
                    price=q["price"],
                    value=str(value),
                    unrealized=str(value - D(r["cost"])),
                    stamp=q["stamp"],
                )
            r["average"] = str((D(r["cost"]) / D(r["qty"])).quantize(SHARE))
        complete = all(r["value"] is not None for r in positions)
        equity = D(p["cash"]) + invested
        for r in positions:
            r["allocation"] = (
                str(money(100 * D(r["value"]) / equity))
                if complete and equity
                else None
            )
        return {
            "portfolio": p,
            "positions": positions,
            "quotes": quotes,
            "realized": str(realized),
            "pending": pending,
            "invested": str(invested) if complete else None,
            "equity": str(equity) if complete else None,
            "unrealized": str(invested - cost) if complete else None,
            "pnl": str(equity - D(p["capital"])) if complete else None,
            "return_pct": str(money(100 * (equity / D(p["capital"]) - 1)))
            if complete
            else None,
        }

    def ticket(self, data):
        method, side = data.get("method"), data.get("side")
        if (
            method not in METHODS
            or side not in ("Buy", "Sell")
            or (method == "position_pct" and side != "Sell")
        ):
            raise Problem("Select a valid side and sizing method.")
        amount = number(
            data.get("amount"),
            "size",
            SHARE,
            D(100) if method.endswith("_pct") else D(1000000000000),
        )
        from .exits import terms

        conditions = terms(data)
        return {
            **({"exit": conditions} if conditions else {}),
            "symbol": symbol(data.get("symbol")),
            "side": side,
            "method": method,
            "amount": str(amount),
            "notes": short(data.get("notes", "")),
            "provenance": data.get("provenance", {}),
            "research": short(data.get("research", ""), 200),
        }

    def size(self, t, v):
        p = v["portfolio"]
        q = v["quotes"][t["symbol"]]
        price, fee, cash, equity = map(
            D, (q["price"], p["fee"], p["cash"], v["equity"])
        )
        held = next(
            (D(r["qty"]) for r in v["positions"] if r["symbol"] == t["symbol"]), D(0)
        )
        amount, method = D(t["amount"]), t["method"]
        basis = {
            "shares": D(1),
            "dollars": D(1),
            "cash_pct": cash,
            "equity_pct": equity,
            "position_pct": held,
        }[method]
        qty = amount if method == "shares" else amount / price
        if method.endswith("_pct"):
            qty = amount * basis / 100
            if method == "cash_pct" and t["side"] == "Buy":
                qty -= fee
            if method != "position_pct":
                qty /= price
        full_exit = t["side"] == "Sell" and qty == held
        step = SHARE if p["fractional"] or full_exit or t.get("entry_signal") else D(1)
        rounded = qty.quantize(step, rounding=ROUND_DOWN)
        if rounded <= 0 or (method == "shares" and rounded != qty):
            raise Problem(
                "Quantity cannot be represented with this portfolio's share increment."
            )
        notional = money(rounded * price)
        if notional <= 0:
            raise Problem("Transaction value rounds to zero.")
        buy = t["side"] == "Buy"
        after = cash + (-notional if buy else notional) - fee
        remaining = held + (rounded if buy else -rounded)
        if after < 0 or remaining < 0:
            raise Problem(
                "Insufficient virtual cash."
                if after < 0
                else "Insufficient simulated holdings."
            )
        resulting_equity = equity - fee
        from .exits import levels

        return {
            **({"exit": levels(t, price)} if t.get("exit") else {}),
            "qty": str(rounded),
            "price": str(price),
            "fee": str(fee),
            "notional": str(notional),
            "cash_after": str(after),
            "held_after": str(remaining),
            "held_before": str(held),
            "cash_before": str(cash),
            "basis": str(basis),
            "allocation": str(money(100 * remaining * price / resulting_equity))
            if resulting_equity
            else "0",
            "stamp": q["stamp"],
            "source": q["source"],
            "oldest_stamp": min(b["stamp"] for b in v["quotes"].values()),
        }

    def draft(self, owner, pid, data):
        ticket = json.dumps(self.ticket(data), sort_keys=True)
        key = data.get("request_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
            raise Problem("Invalid request key.")
        with self.connection(True) as db:
            p = self.account(db, owner, pid, True)
            old = db.execute(
                "SELECT * FROM paper_signals WHERE pid=? AND request_key=?", (pid, key)
            ).fetchone()
            if old:
                if ticket != old["ticket"]:
                    raise Problem("Request key already used.", 409)
                return dict(old)
            sid = uid()
            db.execute(
                "INSERT INTO paper_signals(id,pid,request_key,ticket,preview,version,status,created) VALUES(?,?,?,?,'{}',?,'Draft',?)",
                (sid, pid, key, ticket, p["version"], self.now()),
            )
            self.audit(db, pid, sid, "Draft", json.loads(ticket))
            return dict(
                db.execute("SELECT * FROM paper_signals WHERE id=?", (sid,)).fetchone()
            )

    def preview(self, owner, pid, data):
        t = self.ticket(data)
        key = data.get("request_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
            raise Problem("A valid request key is required.")
        ticket = json.dumps(t, sort_keys=True)

        def existing(db):
            row = db.execute(
                "SELECT * FROM paper_signals WHERE pid=? AND request_key=?", (pid, key)
            ).fetchone()
            if row and row["ticket"] != ticket:
                raise Problem("Request key already identifies another ticket.", 409)
            return dict(row) if row else None

        with self.connection() as db:
            self.account(db, owner, pid, True)
            old = existing(db)
            if old:
                return old
        v = self.valuation(owner, pid, t["symbol"], True)
        preview = self.size(t, v)
        with self.connection(True) as db:
            p = self.account(db, owner, pid, True)
            old = existing(db)
            if old:
                return old
            if p["version"] != v["portfolio"]["version"]:
                raise Problem("Portfolio changed. Preview again.", 409)
            previous = short(data.get("replaces", ""), 32)
            if previous:
                old = db.execute(
                    "SELECT status FROM paper_signals WHERE pid=? AND id=?",
                    (pid, previous),
                ).fetchone()
                if not old or old[0] not in ("Pending", "Draft"):
                    raise Problem("Only an unprocessed signal can be edited.", 409)
                self.finish(
                    db, pid, previous, "Canceled", "Replaced by edited preview."
                )
            sid = uid()
            if previous:
                self.audit(db, pid, sid, "supersedes", previous)
            db.execute(
                "INSERT INTO paper_signals(id,pid,request_key,ticket,preview,version,status,created) VALUES(?,?,?,?,?,?,'Pending',?)",
                (sid, pid, key, ticket, json.dumps(preview), p["version"], self.now()),
            )
            self.audit(db, pid, sid, "Pending", {"ticket": t, "preview": preview})
            return dict(
                db.execute("SELECT * FROM paper_signals WHERE id=?", (sid,)).fetchone()
            )

    def finish(self, db, pid, sid, status, reason=""):
        now = self.now()
        db.execute(
            "UPDATE paper_signals SET status=?,reason=?,processed=? WHERE id=?",
            (status, reason, now, sid),
        )
        db.execute(
            "INSERT INTO paper_orders VALUES(?,?,?,?,?)",
            (sid, pid, status, reason, now),
        )
        self.audit(db, pid, sid, status, reason)

    def expire_pending(self):
        with self.connection(True) as db:
            for r in rows(
                db,
                "SELECT id,pid FROM paper_signals WHERE status='Pending' AND CAST(json_extract(preview,'$.oldest_stamp') AS INTEGER)<? LIMIT 500",
                (self.now() - 120,),
            ):
                self.finish(
                    db,
                    r["pid"],
                    r["id"],
                    "Expired",
                    "Reference price expired. Preview again.",
                )

    def process(self, owner, pid, sid, action):
        current = None
        if action == "confirm":
            with self.connection() as read:
                self.account(read, owner, pid)
                signal = read.execute(
                    "SELECT ticket,status FROM paper_signals WHERE pid=? AND id=?",
                    (pid, sid),
                ).fetchone()
            if signal and signal["status"] == "Pending":
                current = self.market.quote(json.loads(signal["ticket"])["symbol"])
        with self.connection(True) as db:
            p = self.account(db, owner, pid)
            r = db.execute(
                "SELECT * FROM paper_signals WHERE pid=? AND id=?", (pid, sid)
            ).fetchone()
            if r is None:
                raise Problem("Signal not found.", 404)
            if action not in ("confirm", "cancel"):
                raise Problem("Unknown signal action.")
            if r["status"] == "Draft" and action == "cancel":
                self.finish(db, pid, sid, "Canceled")
            elif r["status"] == "Draft":
                raise Problem("Preview this draft before confirming.", 409)
            elif r["status"] == "Pending":
                x, t = json.loads(r["preview"]), json.loads(r["ticket"])
                if action == "cancel":
                    self.finish(db, pid, sid, "Canceled")
                elif p["status"] != "active" or p["version"] != r["version"]:
                    self.finish(
                        db, pid, sid, "Rejected", "Portfolio changed. Preview again."
                    )
                else:
                    try:
                        fresh({"stamp": x["oldest_stamp"]}, self.now())
                        if current is None:
                            raise Problem("Fresh quote unavailable.", 409)
                        fresh(current, self.now())
                        if D(current["price"]) != D(x["price"]):
                            raise Problem(
                                "Reference price changed. Preview again.", 409
                            )
                    except Problem as err:
                        self.finish(db, pid, sid, "Expired", str(err))
                    else:
                        db.execute(
                            "UPDATE paper_signals SET status='Processing' WHERE id=?",
                            (sid,),
                        )
                        self.audit(db, pid, sid, "Processing", {})
                        self.fill(db, pid, sid, t, x)
            return dict(
                db.execute("SELECT * FROM paper_signals WHERE id=?", (sid,)).fetchone()
            )

    def fill(self, db, pid, sid, t, x):
        now = self.now()
        old = db.execute(
            "SELECT * FROM paper_positions WHERE pid=? AND symbol=?", (pid, t["symbol"])
        ).fetchone()
        cost = D(old["cost"]) if old else D(0)
        qty, value, fee, remaining = map(
            D, (x["qty"], x["notional"], x["fee"], x["held_after"])
        )
        realized = D(0)
        if t["side"] == "Buy":
            delta = -value - fee
            cost -= delta
        else:
            delta = value - fee
            released = (
                cost if remaining == 0 else money(cost * qty / D(x["held_before"]))
            )
            realized = delta - released
            cost -= released
        if t.get("exit"):
            levels = x["exit"]
            db.execute(
                "INSERT INTO paper_exits VALUES(?,?,?,?,?,?,?,?,'Armed',?,NULL)",
                (
                    sid,
                    pid,
                    t["symbol"],
                    x["qty"],
                    x["price"],
                    levels["target"],
                    levels["stop"],
                    levels["floor"],
                    now,
                ),
            )
            self.audit(db, pid, sid, "Exit armed", levels)
        elif t["side"] == "Sell" and not t.get("entry_signal"):
            from .exits import cancel

            cancel(
                self, db, pid, "Manual sale changed the holding.", symbol=t["symbol"]
            )
        self.finish(db, pid, sid, "Simulated")
        db.execute(
            "INSERT INTO paper_transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid(),
                pid,
                sid,
                t["symbol"],
                t["side"],
                str(qty),
                x["price"],
                str(value),
                str(fee),
                str(realized),
                x["source"],
                x["stamp"],
                now,
            ),
        )
        db.execute(
            "INSERT INTO paper_cash_ledger(pid,signal,delta,balance,created) VALUES(?,?,?,?,?)",
            (pid, sid, str(delta), x["cash_after"], now),
        )
        db.execute(
            "UPDATE paper_portfolios SET cash=?,version=version+1 WHERE id=?",
            (x["cash_after"], pid),
        )
        if remaining:
            db.execute(
                "INSERT INTO paper_positions VALUES(?,?,?,?,?) ON CONFLICT(pid,symbol) DO UPDATE SET qty=excluded.qty,cost=excluded.cost",
                (
                    pid,
                    t["symbol"],
                    str(remaining),
                    str(cost),
                    old["opened"] if old else now,
                ),
            )
        else:
            db.execute(
                "DELETE FROM paper_positions WHERE pid=? AND symbol=?",
                (pid, t["symbol"]),
            )

    def records(self, owner, pid, kind, limit=100, offset=0, filters=None, db=None):
        if db is None:
            with self.connection() as tx:
                return self.records(owner, pid, kind, limit, offset, filters, tx)
        tables = {
            "signals": "signals",
            "orders": "orders",
            "transactions": "transactions",
            "journal": "journal_entries",
            "ledger": "cash_ledger",
            "audit": "audit_events",
            "snapshots": "portfolio_snapshots",
            "actions": "corporate_actions",
        }
        if kind not in tables:
            raise Problem("Unknown record type.", 404)
        self.account(db, owner, pid)
        filters = filters or {}
        policy = entitlements(owner)
        cutoff = (
            self.now()
            - policy[
                "research_days" if kind in ("signals", "journal") else "history_days"
            ]
            * 86400
        )
        clauses = ["r.pid=?", "r.created>=?"]
        params = [pid, cutoff]
        for param, op in (("start", ">="), ("end", "<")):
            if filters.get(param):
                clauses.append("r.created" + op + "?")
                params.append(int(filters[param]))
        table = "paper_" + tables[kind] + " r"
        select = "r.*"
        ticker = None
        if kind in ("signals", "orders", "transactions"):
            if kind != "signals":
                table += " JOIN paper_signals s ON s.id=" + (
                    "r.id" if kind == "orders" else "r.signal"
                )
            alias = "r" if kind == "signals" else "s"
            ticker = f"json_extract({alias}.ticket,'$.symbol')"
            if kind != "signals":
                select += f", {alias}.ticket, {alias}.preview, {alias}.status AS signal_status"
            if filters.get("symbol"):
                clauses.append(ticker + "=?")
                params.append(symbol(filters["symbol"]))
            if filters.get("query"):
                clauses.append(
                    f"(instr(lower({alias}.ticket),lower(?))>0 OR instr(lower({alias}.status),lower(?))>0 OR instr({alias}.id,?)>0)"
                )
                params.extend([short(filters["query"], 200)] * 3)
        sort_by = {
            "created": "r.created",
            "symbol": ticker,
            "status": ("r.status" if kind in ("signals", "orders") else None),
            "qty": (
                "CAST(json_extract(" + alias + ".preview,'$.qty') AS REAL)"
                if ticker
                else None
            ),
        }.get(filters.get("sort"))
        order = (sort_by or "r.created") + (
            " ASC" if filters.get("direction") == "asc" else " DESC"
        )
        return rows(
            db,
            f"SELECT {select} FROM {table} WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY {order},r.rowid DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )

    def journal(self, owner, pid, data):
        fields = {
            k: short(data.get(k, ""))
            for k in (
                "hypothesis",
                "rationale",
                "strategy",
                "entry",
                "exit",
                "tags",
                "observations",
                "review",
                "lessons",
            )
        }
        if not any(fields.values()):
            raise Problem("Enter a journal observation.")
        sid = short(data.get("signal", ""), 32) or None
        with self.connection(True) as db:
            self.account(db, owner, pid, True)
            if (
                sid
                and not db.execute(
                    "SELECT id FROM paper_signals WHERE pid=? AND id=?", (pid, sid)
                ).fetchone()
            ):
                raise Problem("Linked signal not found.", 404)
            jid = uid()
            db.execute(
                "INSERT INTO paper_journal_entries VALUES(?,?,?,?,?)",
                (jid, pid, sid, json.dumps(fields), self.now()),
            )
            self.audit(db, pid, jid, "journal entry", {})
            return {"id": jid}
