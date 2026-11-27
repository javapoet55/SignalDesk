"""Authenticated HTTP adapter. The host supplies the verified dashboard identity."""

import csv
import io
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from urllib.parse import parse_qs

from . import identity
from .ledger import Ledger
from .market import Market, Problem, symbol
from .policy import RateLimit, entitlements, require
from .research import provenance, securities

ROOT = Path(__file__).parent
PLAIN_NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def export_cell(value):
    """A CSV cell that a spreadsheet will not run as a formula.

    Text starting with = + - or @ gets a leading apostrophe. A plain number is left alone: amounts are
    stored as text, and the apostrophe used to turn every negative one (a loss, a cash debit) into text
    that a spreadsheet would not add up."""
    if (
        isinstance(value, str)
        and value.lstrip().startswith(("=", "+", "@", "-"))
        and not PLAIN_NUMBER.fullmatch(value.strip())
    ):
        return "'" + value
    return value
LOCK = threading.Lock()
LIMITER = RateLimit()


def service(app):
    with LOCK:
        if not hasattr(app, "paper"):
            market = Market(getattr(app.strategies, "finviz", None))
            app.paper = Ledger(
                app.settings.data_dir / "paper" / "ledger.sqlite3", market
            )
        return app.paper


def page(host, owner):
    import server

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Paper Trading · Signal desk</title>"
        + server._THEME_INIT
        + "<style>"
        + server._CSS
        + server._STRAT_CSS
        + server._JOURNAL_DESIGN_CSS
        + server._DASHBOARD_THEME_CSS
        + (ROOT / "paper.css").read_text()
        + "</style></head><body class='journal-app dashboard-app'><main>"
        + (
            server.render_tabs("paper")
            if owner == host.app.settings.user
            else server.studio.shell(server.TABS, "paper", "<div class='account-controls'>" + server.theme_control()
                                    + "<form action='/paper/logout' method='post'><button>Sign out</button></form></div>")
        )
        + (ROOT / "paper.html").read_text()
        + "</main><script>"
        + (ROOT / "paper.js").read_text()
        + "</script></body></html>"
    )


def authorized_dispatch(h, path, query):
    if path in ("/paper/login", "/paper/logout"):
        return identity.handle(h, path)
    principal = identity.lookup(h)
    if principal is None:
        if not h._authorized():
            return
        principal = h.app.settings.user
    return dispatch(h, path, query, principal)


def dispatch(h, path, query, owner):
    def send(data, status=200):
        h._send(
            status,
            json.dumps(data, allow_nan=False).encode(),
            "application/json; charset=utf-8",
        )

    try:
        if path == "/paper" and h.command == "GET":
            h.CSP += "; connect-src 'self'"
            body = page(h, owner)
            if owner != h.app.settings.user:
                body = body.replace("action='/logout'", "action='/paper/logout'")
            return h._html(200, body)
        db = service(h.app)
        LIMITER.check(owner)
        q = {k: v[0] for k, v in parse_qs(query).items()}
        post = h.command == "POST"
        data = {}
        if post:
            if (
                not h._same_origin()
                or h.headers.get_content_type() != "application/json"
                or h.headers.get("X-Paper-Request") != "1"
            ):
                raise Problem("Invalid request origin or content type.", 403)
            size = int(h.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                h.close_connection = True
                raise Problem("Invalid request size.", 413)
            data = json.loads(h.rfile.read(size))
            if not isinstance(data, dict):
                raise Problem("Expected a JSON object.", 400)
        if path == "/paper/api/portfolios":
            return send(
                db.create(owner, data) if post else db.portfolios(owner),
                201 if post else 200,
            )
        if path == "/paper/api/securities" and not post:
            return send(securities(h.app) if owner == h.app.settings.user else {})
        if path == "/paper/api/entitlements" and not post:
            return send(entitlements(owner))
        if path == "/paper/api/watchlist" and not post:
            if owner != h.app.settings.user:
                return send([])
            file = h.app.settings.reports_dir / "latest_tickers.txt"
            tickers = file.read_text().splitlines()[1:] if file.is_file() else []
            tickers = [t for line in tickers for t in line.split(",")]
            rid = q.get("research", "")
            if rid:
                import server
                import strategies

                if not strategies.RESULT_RE.fullmatch(rid) or not h.app.strategies:
                    raise Problem("Invalid research run.")
                tickers = server.signal_tickers(h.app.strategies.results_dir / rid)
            return send(
                list(
                    dict.fromkeys(
                        t for t in tickers if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", t)
                    )
                )[:200]
            )
        if path == "/paper/api/research" and not post:
            if owner != h.app.settings.user:
                raise Problem("Dashboard research belongs to another account.", 403)
            from .research import saved_result

            return send(saved_result(h.app, q.get("run", ""), q.get("symbol", "")))
        if path == "/paper/api/reference" and not post:
            from .market import validate_reference

            pricing = q.get("pricing", "close")
            quote = db.reference(symbol(q.get("symbol")), pricing)
            validate_reference(quote, db.now(), pricing)
            return send(quote)
        if path == "/paper/api/bars" and not post:
            cutoff = db.now() - entitlements(owner)["history_days"] * 86400
            return send(
                [
                    b
                    for b in db.market.history(symbol(q.get("symbol")))
                    if b["stamp"] >= cutoff
                ]
            )
        match = re.fullmatch(
            r"/paper/api/portfolios/([0-9a-f]{32})/([a-z]+)(?:/([0-9a-f]{32})/(confirm|cancel))?",
            path,
        )
        if not match:
            raise Problem("Route not found.", 404)
        pid, resource, sid, action = match.groups()
        if resource == "exits":
            from .exits import cancel
            from .ledger import rows

            with db.connection(write=post) as tx:
                db.account(tx, owner, pid)
                if post and sid and action == "cancel":
                    cancel(db, tx, pid, "Canceled by user.", entry=sid)
                    return send({"ok": True})
                if not post and not sid:
                    return send(
                        rows(
                            tx,
                            "SELECT * FROM paper_exits WHERE pid=? ORDER BY rowid DESC",
                            (pid,),
                        )
                    )
            raise Problem("Route not found.", 404)
        if sid:
            if post and resource == "signals":
                return send(db.process(owner, pid, sid, action))
            raise Problem("Route not found.", 404)
        if post:
            if resource in ("preview", "draft"):
                if data.get("research") and owner != h.app.settings.user:
                    raise Problem("Dashboard research belongs to another account.", 403)
                data["provenance"] = provenance(h.app, data.get("research"))
            if resource == "draft":
                return send(db.draft(owner, pid, data))
            if resource == "preview":
                return send(db.preview(owner, pid, data))
            if resource == "action":
                from .actions import apply_action

                return send(apply_action(db, owner, pid, data))
            if resource == "explanation":
                from .reporting import explanation

                return send(explanation(db, owner, pid))
            if resource == "historical":
                from .reporting import historical_benchmark

                return send(historical_benchmark(db, owner, pid))
            if resource == "settings":
                return send(db.settings(owner, pid, data))
            if resource == "journal":
                return send(db.journal(owner, pid, data), 201)
        else:
            if resource == "settings":
                with db.connection() as tx:
                    return send(db.account(tx, owner, pid))
            if resource == "positions":
                return send(db.valuation(owner, pid)["positions"])
            if resource == "operations":
                with db.connection() as tx:
                    db.account(tx, owner, pid)
                    last = tx.execute(
                        "SELECT max(created) FROM paper_portfolio_snapshots WHERE pid=?",
                        (pid,),
                    ).fetchone()[0]
                    counts = {
                        r[0]: r[1]
                        for r in tx.execute(
                            "SELECT status,count(*) FROM paper_signals WHERE pid=? GROUP BY status",
                            (pid,),
                        )
                    }
                return send(
                    {
                        "last_snapshot": last,
                        "signals": counts,
                        "storage": "SQLite WAL",
                        "provider": "Finviz completed minute bars",
                    }
                )
            if resource == "report":
                from .reporting import pdf_report

                return h._send(
                    200,
                    pdf_report(db, owner, pid),
                    "application/pdf",
                    {
                        "Content-Disposition": 'attachment; filename="paper-research.pdf"'
                    },
                )
            if resource == "summary":
                return send(db.valuation(owner, pid))
            if resource == "analytics":
                from .analytics import analytics

                return send(analytics(db, owner, pid))
            if resource == "export":
                require(owner, "export")
                output = io.StringIO()
                writer = csv.writer(output)
                offset = 0
                with db.connection() as tx:
                    while True:
                        batch = db.records(
                            owner,
                            pid,
                            q.get("kind", "transactions"),
                            500,
                            offset,
                            q,
                            tx,
                        )
                        if not batch:
                            break
                        if not offset:
                            writer.writerow(batch[0])
                        for row in batch:
                            writer.writerow([export_cell(v) for v in row.values()])
                        offset += len(batch)
                return h._send(
                    200,
                    output.getvalue().encode(),
                    "text/csv; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="paper-records.csv"'},
                )
            limit = int(q.get("limit", 100))
            offset = int(q.get("offset", 0))
            if not 1 <= limit <= 500 or offset < 0:
                raise Problem("Invalid pagination.", 400)
            return send(db.records(owner, pid, resource, limit, offset, q))
        raise Problem("Route not found.", 404)
    except Problem as err:
        send({"error": str(err)}, err.status)
    except (ValueError, UnicodeError, OverflowError):
        send({"error": "Invalid request value."}, 400)
    except sqlite3.OperationalError:
        logging.getLogger(__name__).exception("Paper ledger unavailable")
        send({"error": "Paper ledger unavailable. Retry shortly."}, 503)
