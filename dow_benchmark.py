"""Daily Dow Jones Industrial Average closes for the authenticated backtest chart."""

import csv
import io
import math
import threading
import time
import urllib.request
from datetime import date
from http.client import HTTPException

SOURCE = "https://fred.stlouisfed.org/series/DJIA"
_FEED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DJIA"
_lock = threading.Lock()
_closes = {}
_refresh_at = 0.0


def _download():
    req = urllib.request.Request(
        _FEED, headers={"Accept": "text/csv", "User-Agent": "Finviz Agent/1.0"}
    )
    with urllib.request.urlopen(req, timeout=4) as response:
        reader = csv.DictReader(io.TextIOWrapper(response, encoding="utf-8-sig"))
        closes = {}
        for row in reader:
            close = row["DJIA"]
            if close in ("", "."):
                continue  # Non-trading days have no closing index value.
            day = date.fromisoformat(row["observation_date"]).isoformat()
            value = float(close)
            if math.isfinite(value) and value > 0:
                closes[day] = value
    if not closes:
        raise ValueError("Empty DJIA series")
    return closes


def equity(days, capital):
    """Align exact session closes and rebase once, at the start of the full backtest."""
    global _closes, _refresh_at
    if not days:
        return []
    with _lock:
        if time.monotonic() >= _refresh_at:
            try:
                _closes = _download()
            except (OSError, HTTPException, ValueError, KeyError, TypeError, csv.Error):
                _refresh_at = time.monotonic() + 300
            else:
                _refresh_at = time.monotonic() + 3600
        closes = _closes
    if any(day not in closes for day in days):
        return []
    return [capital * (closes[d] / closes[days[0]]) for d in days]
