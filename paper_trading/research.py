"""Read provenance from completed research artifacts without invoking a strategy."""

import hashlib
import json

import strategies

from .market import Problem


def provenance(app, rid):
    if not rid:
        return {}
    if (
        not isinstance(rid, str)
        or not strategies.RESULT_RE.fullmatch(rid)
        or not app.strategies
    ):
        raise Problem("Select a saved research run.")
    folder = app.strategies.results_dir / rid
    meta = strategies.read_meta(folder)
    signals = folder / "signals.csv"
    if not meta or not strategies.regular_file(signals, folder):
        raise Problem("Research results are unavailable.")
    with signals.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    return {
        "run": rid,
        "strategy": meta.get("strategy"),
        "mode": meta.get("mode"),
        "scan": meta.get("scan"),
        "started_utc": meta.get("started_utc"),
        "signals_sha256": digest,
        "strategy_sha256": meta.get("strategy_sha256"),
    }


def securities(app):
    root = app.settings.reports_dir
    if not root.is_dir():
        return {}
    import server

    for folder in sorted(
        (p for p in root.iterdir() if server.STAMP_RE.fullmatch(p.name)), reverse=True
    ):
        summary = folder / "summary.json"
        if not summary.is_file():
            continue
        try:
            report = json.loads(summary.read_text())
        except (ValueError, OSError):
            continue
        result = {}
        for screen in report.get("screens", []):
            for ranking in screen.get("rankings", {}).values():
                for item in ranking.get("top", []):
                    result[item["ticker"]] = {
                        k: item.get(k)
                        for k in ("company", "sector", "price", "change_today_pct")
                    }
        return result
    return {}


def saved_result(app, rid, ticker):
    """Read one symbol's original output without re-running the research."""
    import csv

    from .market import symbol

    ticker = symbol(ticker)
    meta = provenance(app, rid)
    if not meta:
        raise Problem("A saved research run is required.")
    path = app.strategies.results_dir / rid / "signals.csv"
    with path.open(newline="", encoding="utf-8", errors="replace") as stream:
        record = next(
            (
                row
                for row in csv.DictReader(stream)
                if (row.get("Ticker") or row.get("Symbol") or "").upper() == ticker
            ),
            {},
        )
    return {
        "provenance": meta,
        "result": {
            k: v[:500]
            for k, v in list(record.items())[:50]
            if isinstance(k, str) and isinstance(v, str)
        },
    }
