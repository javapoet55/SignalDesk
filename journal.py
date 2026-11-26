"""Trade journal for the FinViz agent ("Journal" tab).

Trades come from three places:
  * broker CSV exports (fills: buy/sell executions), matched into round-trip trades here;
  * the manual "Add trade" form;
  * the "Log trade" link on a strategy's BUY row (prefills the manual form).

Storage (all on the Railway volume, written atomically):
  journal/fills.json   imported executions, de-duplicated so re-importing a file adds nothing
  journal/manual.json  trades entered by hand
  journal/notes.json   setup / strategy / tags / mistakes / notes for every trade, keyed by trade id
  journal/imports.json one line per import (file, broker, rows read/added/skipped)

Numbers are computed, never estimated:
  * Imported fills are grouped per symbol into trades that start when the position leaves 0 and end
    when it returns to 0. Scaling in and out uses average cost; a fill that flips the position
    closes the trade and opens a new one in the other direction (its fee split by share count).
  * P&L = gross (price difference x shares, sign by side) - fees.

Standard library only.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MAX_FILLS = 200_000
MAX_MANUAL = 20_000
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")  # stocks/ETFs; options and futures codes are skipped
TRADE_ID_RE = re.compile(r"^[mf][0-9a-f]{16}$")
EPS = 1e-9


class JournalError(ValueError):
    """A problem the user can fix (bad file, bad form value)."""


# --------------------------------------------------------------------------
# Small parsers
# --------------------------------------------------------------------------
def parse_number(raw) -> Optional[float]:
    """'$1,234.50' -> 1234.5, '(12.00)' -> -12.0, '' / '--' -> None."""
    if raw is None:
        return None
    s = str(raw).strip().replace("$", "").replace(",", "").replace("−", "-")
    if s in ("", "-", "--", "—", "N/A", "n/a"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    return -v if neg else v


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d", "%d-%b-%Y", "%b %d, %Y", "%Y/%m/%d")
_TIME_FORMATS = ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p", "%H%M%S")


def parse_when(raw: str) -> Tuple[Optional[str], bool]:
    """Broker date/time text -> ('YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD', has_time).
    Time zone suffixes (EDT/EST/ET) are dropped: brokers report US exchange time."""
    s = str(raw or "").strip()
    if not s:
        return None, False
    s = re.sub(r"\s+as of .*$", "", s, flags=re.I)              # Schwab: '10/30/2026 as of 10/29/2026'
    s = re.sub(r"\s*\b(E[DS]T|ET|US/Eastern|America/New_York)\b\s*$", "", s, flags=re.I)
    m = re.match(r"^([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4})\s*(.*)$", s)  # 'Oct 30, 2026 9:31:05 AM'
    if m:
        try:
            day = dt.datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
        except ValueError:
            return None, False
        s = f"{day.isoformat()} {m.group(4)}".strip()
    s = s.replace("T", " ") if re.match(r"^\d{4}-\d{2}-\d{2}T", s) else s
    s = re.sub(r"(\d)[,;]\s*(\d)", r"\1 \2", s)                   # IBKR: '2026-10-30, 09:31:05' / '20261030;093105'
    s = re.sub(r"(\.\d+)?([+-]\d{2}:?\d{2}|Z)$", "", s).strip()  # ISO fractions / offsets
    parts = s.split(None, 1)
    date_text, time_text = parts[0], (parts[1] if len(parts) > 1 else "")
    day = None
    for fmt in _DATE_FORMATS:
        try:
            day = dt.datetime.strptime(date_text, fmt).date()
            break
        except ValueError:
            continue
    if day is None:
        return None, False
    time_text = time_text.strip()
    if not time_text:
        return day.isoformat(), False
    for fmt in _TIME_FORMATS:
        try:
            t = dt.datetime.strptime(time_text.upper(), fmt).time()
            return f"{day.isoformat()} {t:%H:%M:%S}", True
        except ValueError:
            continue
    return day.isoformat(), False


def clean_symbol(raw: str) -> str:
    s = str(raw or "").strip().upper()
    s = s.replace("/", ".")
    return s


# --------------------------------------------------------------------------
# Broker CSV formats
# --------------------------------------------------------------------------
def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").lower()).strip()


# Column aliases (normalized). The first match wins.
ALIASES = {
    "when": ["filled time", "date time", "execution time", "exec time", "trade date time", "datetime",
             "activity date", "run date", "trade date", "date", "time"],
    "time_only": ["time", "exec time", "execution time"],
    "symbol": ["symbol", "ticker", "instrument", "underlying", "sym"],
    "side": ["side", "action", "buy sell", "trans code", "transaction type", "type", "b s"],
    "qty": ["filled", "filled qty", "quantity", "qty", "shares", "size", "filled quantity"],
    "price": ["avg price", "average price", "fill price", "t price", "tradeprice", "trade price", "price",
              "exec price", "execution price", "price usd"],
    "fees": ["fees comm", "fees commissions", "comm fee", "commission", "commissions", "fees", "fee",
             "ibcommission", "comm", "commission usd", "fees usd", "reg fee"],
    "status": ["status", "order status"],
}

BUY_WORDS = ("buy", "bought", "buy to cover", "buy to open", "buy to close", "cover", "bto", "btc", "b", "long")
SELL_WORDS = ("sell", "sold", "sell short", "short", "sell to open", "sell to close", "sto", "stc", "s", "ss",
              "short sell")


def side_of(raw: str) -> Optional[str]:
    s = _norm(raw)
    if not s:
        return None
    if s.startswith("you bought") or s.startswith("bought"):
        return "buy"
    if s.startswith("you sold") or s.startswith("sold") or s.startswith("you shorted"):
        return "sell"
    if s in BUY_WORDS or s.startswith("buy"):
        return "buy"
    if s in SELL_WORDS or s.startswith("sell") or s.startswith("short"):
        return "sell"
    return None


def _find(headers: List[str], key: str, used: Iterable[int] = ()) -> Optional[int]:
    normed = [_norm(h) for h in headers]
    used = set(used)
    for alias in ALIASES[key]:
        for i, h in enumerate(normed):
            if i not in used and h == alias:
                return i
    return None


def _ibkr_sections(text: str) -> Optional[Tuple[List[str], List[List[str]]]]:
    """IBKR Activity Statement: rows like 'Trades,Header,...' / 'Trades,Data,Order,Stocks,USD,AAPL,...'."""
    header, rows = None, []
    for row in csv.reader(io.StringIO(text)):
        if len(row) > 2 and row[0] == "Trades":
            if row[1] == "Header" and header is None:
                header = row[2:]
            elif row[1] == "Data" and header is not None:
                cells = row[2:]
                if cells and cells[0] not in ("Order", "Trade", "") and cells[0].lower() != "execution":
                    continue  # SubTotal / Total lines
                rows.append(cells)
    return (header, rows) if header else None


def detect_broker(headers: Sequence[str]) -> str:
    n = {_norm(h) for h in headers}
    if {"trans code", "instrument"} <= n:
        return "Robinhood"
    if {"filled time", "avg price"} <= n or {"placed time", "filled"} <= n:
        return "Webull"
    if {"fees comm", "action"} <= n or {"fees commissions", "action"} <= n:
        return "Schwab"
    if "run date" in n:
        return "Fidelity"
    if {"t price"} <= n or "ibcommission" in n or "tradeprice" in n:
        return "Interactive Brokers"
    return "Generic"


def account_suffix(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-4:] if len(digits) >= 4 else digits if len(digits) == 3 else ""


def fill_account(fill: dict) -> str:
    if fill.get("account_suffix"):
        return account_suffix(fill["account_suffix"])
    match = re.search(r"account[\s_\-]*([A-Za-z]*\d{3,})", fill.get("file", ""), re.I)
    return account_suffix(match.group(1)) if match else ""


def read_fills(content: bytes, filename: str = "") -> Tuple[str, List[dict], List[str]]:
    """Parse a broker CSV. Returns (broker, fills, skipped-reasons).
    Each fill: {when, has_time, symbol, side, qty, price, fees, row}."""
    if len(content) > 12_000_000:
        raise JournalError("That file is larger than 12 MB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    if "\x00" in text:
        raise JournalError("That isn't a text CSV file. Export your trade history as CSV and upload that.")
    ib = _ibkr_sections(text)
    if ib:
        headers, data = ib
        broker = "Interactive Brokers"
        start_row = 1
    else:
        rows = [r for r in csv.reader(io.StringIO(text))]
        # Skip title lines some brokers put above the header (Schwab: '"Transactions for account ..."').
        hi = None
        for i, r in enumerate(rows[:15]):
            normed = {_norm(c) for c in r}
            if any(a in normed for a in ALIASES["symbol"]) and any(a in normed for a in ALIASES["price"]):
                hi = i
                break
        if hi is None:
            found = ", ".join(c for c in (rows[0] if rows else []) if c.strip())[:300] or "nothing"
            raise JournalError("Couldn't find the trade columns in this file. It needs a symbol, a price, a "
                               "quantity, a buy/sell side and a date. First line found: " + found)
        headers, data = rows[hi], rows[hi + 1:]
        broker = detect_broker(headers)
        start_row = hi + 2
    cols: Dict[str, Optional[int]] = {}
    used: List[int] = []
    for key in ("symbol", "price", "qty", "fees", "status", "side"):
        cols[key] = _find(headers, key, used)
        if cols[key] is not None:
            used.append(cols[key])
    cols["account"] = next((i for i,h in enumerate(headers) if _norm(h) in
                            ("account", "account number", "account no", "account id", "account #")), None)
    file_account = fill_account({"file": filename})
    cols["when"] = _find(headers, "when", used)
    if cols["when"] is not None:
        used.append(cols["when"])
    # A separate time column (e.g. 'Date' + 'Time') is joined to the date.
    cols["time_only"] = None
    if cols["when"] is not None and _norm(headers[cols["when"]]) in ("date", "trade date", "activity date", "run date"):
        cols["time_only"] = _find(headers, "time_only", used)
    missing = [k for k in ("symbol", "price", "qty", "when") if cols[k] is None]
    if missing:
        names = {"symbol": "Symbol", "price": "Price", "qty": "Quantity", "when": "Date/Time"}
        raise JournalError("This file is missing a column for: " + ", ".join(names[m] for m in missing)
                           + ". Columns found: " + ", ".join(h for h in headers if h.strip())[:400])
    # Every fee-like column counts (Fidelity has both 'Commission ($)' and 'Fees ($)').
    fee_cols = [i for i, h in enumerate(headers) if _norm(h) in ALIASES["fees"] and i not in
                (cols["symbol"], cols["price"], cols["qty"], cols["when"], cols["side"])]
    fills, skipped = [], []

    def cell(r, k):
        i = cols.get(k)
        return r[i] if i is not None and i < len(r) else ""

    for n, r in enumerate(data):
        rowno = n + start_row
        if not any(str(c).strip() for c in r):
            continue
        status = _norm(cell(r, "status"))
        if status and status not in ("filled", "executed", "complete", "completed", "partially filled", "partial"):
            continue  # cancelled / rejected orders (Webull) are not trades
        symbol = clean_symbol(cell(r, "symbol"))
        qty = parse_number(cell(r, "qty"))
        price = parse_number(cell(r, "price"))
        side = side_of(cell(r, "side")) if cols["side"] is not None else None
        if side is None and qty is not None and cols["side"] is None:
            side = "buy" if qty > 0 else "sell" if qty < 0 else None  # IBKR signed quantity
        if side is None and qty is not None and qty < 0:
            side = "sell"
        if cols["side"] is not None and side is None:
            if cell(r, "side").strip():
                skipped.append(f"Row {rowno}: not a buy/sell ({cell(r, 'side').strip()[:40]})")
            continue  # dividends, transfers, interest...
        if not symbol:
            continue
        if not SYMBOL_RE.match(symbol):
            skipped.append(f"Row {rowno}: {symbol[:24]} skipped (only stocks and ETFs are journaled)")
            continue
        if qty is None or price is None or abs(qty) < EPS or price <= 0:
            skipped.append(f"Row {rowno}: {symbol} has no quantity or price")
            continue
        when_text = cell(r, "when")
        if cols["time_only"] is not None and cell(r, "time_only").strip():
            when_text = f"{when_text} {cell(r, 'time_only')}"
        when, has_time = parse_when(when_text)
        if not when:
            skipped.append(f"Row {rowno}: {symbol} has an unreadable date ({str(when_text)[:30]})")
            continue
        fees = math.fsum(abs(parse_number(r[i]) or 0.0) for i in fee_cols if i < len(r))
        fills.append({"when": when, "has_time": has_time, "symbol": symbol, "side": side, "qty": abs(qty),
                      "price": price, "fees": round(fees, 6), "row": rowno,
                      "account_suffix": account_suffix(cell(r, "account")) or file_account})
    # Brokers that list newest first without times (Schwab) are put in file order oldest-first.
    if fills and not any(f["has_time"] for f in fills) and len({f["when"] for f in fills}) > 1:
        if fills[0]["when"] > fills[-1]["when"]:
            fills.reverse()
    return broker, fills, skipped


def fill_ids(fills: List[dict]) -> List[str]:
    """Stable ids: same fill content + its occurrence number among identical rows of the same file."""
    seen: Dict[str, int] = {}
    out = []
    for f in fills:
        base = f"{f['when']}|{f['symbol']}|{f['side']}|{f['qty']:.6f}|{f['price']:.6f}"
        k = seen.get(base, 0)
        seen[base] = k + 1
        out.append(hashlib.sha1(f"{base}|{k}".encode()).hexdigest()[:16])
    return out


# --------------------------------------------------------------------------
# Fills -> round-trip trades
# --------------------------------------------------------------------------
def match_trades(fills: List[dict]) -> List[dict]:
    """Group fills (each with 'id' and 'seq') into trades per symbol. Pure function."""
    by_symbol: Dict[str, List[dict]] = {}
    for f in fills:
        by_symbol.setdefault(f["symbol"], []).append(f)
    trades = []
    for symbol, fs in by_symbol.items():
        fs.sort(key=lambda f: (f["when"], f.get("seq", 0)))
        cur = None
        pos = 0.0          # signed shares
        avg = 0.0          # average cost of the open position
        for f in fs:
            signed = f["qty"] if f["side"] == "buy" else -f["qty"]
            remaining = signed
            fee_per_share = f["fees"] / f["qty"] if f["qty"] else 0.0
            while abs(remaining) > EPS:
                if abs(pos) < EPS:  # open a new trade
                    cur = {"id": "f" + f["id"], "symbol": symbol, "side": "long" if remaining > 0 else "short",
                           "entry_time": f["when"], "has_time": f["has_time"], "exit_time": None,
                           "opened": 0.0, "open_value": 0.0, "closed": 0.0, "close_value": 0.0,
                           "gross": 0.0, "fees": 0.0, "fill_ids": [], "source": "import"}
                    trades.append(cur)
                    pos, avg = 0.0, 0.0
                same_dir = (pos >= 0 and remaining > 0) or (pos <= 0 and remaining < 0)
                if same_dir:
                    q = abs(remaining)
                    avg = (abs(pos) * avg + q * f["price"]) / (abs(pos) + q)
                    pos += remaining
                    cur["opened"] += q
                    cur["open_value"] += q * f["price"]
                    remaining = 0.0
                else:
                    q = min(abs(remaining), abs(pos))
                    direction = 1.0 if pos > 0 else -1.0
                    cur["gross"] += q * (f["price"] - avg) * direction
                    cur["closed"] += q
                    cur["close_value"] += q * f["price"]
                    pos += q if pos < 0 else -q
                    remaining += q if remaining < 0 else -q
                    if abs(pos) < EPS:
                        pos = 0.0
                        cur["exit_time"] = f["when"]
                        cur["has_time"] = cur["has_time"] and f["has_time"]
                cur["remaining_basis"] = avg
                cur["fees"] += fee_per_share * q
                if not cur["fill_ids"] or cur["fill_ids"][-1] != f["id"]:
                    cur["fill_ids"].append(f["id"])  # a fill that flips the position is in both trades
        # trades left open keep exit_time None
    out = []
    for t in trades:
        qty = t["opened"]
        entry = t["open_value"] / qty if qty else 0.0
        exit_price = t["close_value"] / t["closed"] if t["closed"] else None
        closed = t["exit_time"] is not None
        pnl = t["gross"] - t["fees"]
        out.append({
            "id": t["id"], "symbol": t["symbol"], "side": t["side"], "qty": round(qty, 6),
            "entry_time": t["entry_time"], "entry_price": round(entry, 6),
            "exit_time": t["exit_time"], "exit_price": round(exit_price, 6) if exit_price is not None else None,
            "fees": round(t["fees"], 6), "gross": round(t["gross"], 6) if closed else None,
            "pnl": round(pnl, 6) if closed else None,
            "return_pct": round(pnl / (entry * qty) * 100, 4) if closed and entry * qty else None,
            "status": "closed" if closed else "open", "has_time": t["has_time"], "source": "import",
            "open_qty": None if closed else round(t["opened"] - t["closed"], 6),
            "fill_ids": t["fill_ids"],
            "mark_basis": t.get("remaining_basis", entry), "realized_gross": t["gross"],
        })
    return out


def manual_trade(rec: dict) -> dict:
    """A stored manual trade -> the same shape as an imported one."""
    qty, entry, exitp, fees = rec["qty"], rec["entry_price"], rec.get("exit_price"), rec.get("fees", 0.0)
    closed = exitp is not None and bool(rec.get("exit_time"))
    sign = 1.0 if rec["side"] == "long" else -1.0
    gross = (exitp - entry) * qty * sign if closed else None
    pnl = gross - fees if closed else None
    return {"id": rec["id"], "symbol": rec["symbol"], "side": rec["side"], "qty": qty,
            "entry_time": rec["entry_time"], "entry_price": entry,
            "exit_time": rec.get("exit_time") if closed else None, "exit_price": exitp if closed else None,
            "fees": fees, "gross": round(gross, 6) if closed else None, "pnl": round(pnl, 6) if closed else None,
            "return_pct": round(pnl / (entry * qty) * 100, 4) if closed and entry * qty else None,
            "status": "closed" if closed else "open",
            "has_time": len(rec["entry_time"]) > 10 and (not closed or len(rec.get("exit_time") or "") > 10),
            "source": "manual", "open_qty": None if closed else qty, "fill_ids": []}


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------
def _parse_dt(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(s) if len(s) > 10 else None
    except (TypeError, ValueError):
        return None


def exit_day(t: dict) -> str:
    return (t.get("exit_time") or "")[:10]


def stats(trades: List[dict]) -> dict:
    """Summary of closed trades (oldest-first by exit)."""
    closed = sorted((t for t in trades if t["status"] == "closed"),
                    key=lambda t: (t["exit_time"], t["entry_time"], t["id"]))
    pnls = [t["pnl"] for t in closed]
    wins = [p for p in pnls if p > 0.005]
    losses = [p for p in pnls if p < -0.005]
    even = len(pnls) - len(wins) - len(losses)
    gp, gl = math.fsum(wins), -math.fsum(losses)
    net = math.fsum(pnls)
    curve, peak, max_dd, run = [], 0.0, 0.0, 0.0
    for t in closed:
        run += t["pnl"]
        curve.append((t["exit_time"], round(run, 2)))
        peak = max(peak, run)
        max_dd = max(max_dd, peak - run)
    streak_w = streak_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0.005:
            cur_w, cur_l = cur_w + 1, 0
        elif p < -0.005:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        streak_w, streak_l = max(streak_w, cur_w), max(streak_l, cur_l)
    holds = []
    for t in closed:
        a, b = _parse_dt(t["entry_time"]), _parse_dt(t["exit_time"])
        if a and b and t.get("has_time"):
            holds.append((b - a).total_seconds())
    decided = len(wins) + len(losses)
    return {
        "trades": len(closed), "open": sum(1 for t in trades if t["status"] == "open"),
        "wins": len(wins), "losses": len(losses), "even": even,
        "win_rate": len(wins) / decided * 100 if decided else None,
        "net": net, "gross_profit": gp, "gross_loss": gl,
        "profit_factor": gp / gl if gl > 0 else None,
        "avg_win": gp / len(wins) if wins else None, "avg_loss": -gl / len(losses) if losses else None,
        "payoff": (gp / len(wins)) / (gl / len(losses)) if wins and losses else None,
        "expectancy": net / len(closed) if closed else None,
        "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None,
        "max_drawdown": max_dd, "streak_win": streak_w, "streak_loss": streak_l,
        "avg_hold_sec": sum(holds) / len(holds) if holds else None,
        "fees": math.fsum(t["fees"] for t in closed), "curve": curve,
    }


def breakdown(trades: List[dict], key) -> List[dict]:
    """Closed-trade P&L grouped by key(trade) -> [{name, trades, wins, win_rate, net, avg}] by net desc.
    key may return a list (a trade counts once in each of its tags)."""
    groups: Dict[str, List[float]] = {}
    for t in trades:
        if t["status"] != "closed":
            continue
        k = key(t)
        for name in (k if isinstance(k, list) else [k]):
            if name not in (None, ""):
                groups.setdefault(str(name), []).append(t["pnl"])
    rows = []
    for name, ps in groups.items():
        w = sum(1 for p in ps if p > 0.005)
        decided = w + sum(1 for p in ps if p < -0.005)
        rows.append({"name": name, "trades": len(ps), "wins": w, "win_rate": w / decided * 100 if decided else None,
                     "net": math.fsum(ps), "avg": math.fsum(ps) / len(ps)})
    rows.sort(key=lambda r: (-r["net"], r["name"]))
    return rows


def daily(trades: List[dict]) -> Dict[str, Tuple[float, int]]:
    """{exit day: (net P&L, trades closed)}."""
    out: Dict[str, Tuple[float, int]] = {}
    for t in trades:
        if t["status"] == "closed":
            d = exit_day(t)
            p, n = out.get(d, (0.0, 0))
            out[d] = (p + t["pnl"], n + 1)
    return out


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def weekday_of(t: dict) -> Optional[str]:
    try:
        return WEEKDAYS[dt.date.fromisoformat(t["entry_time"][:10]).weekday()]
    except (ValueError, TypeError):
        return None


def hour_of(t: dict) -> Optional[str]:
    if not t.get("has_time") or len(t["entry_time"]) <= 10:
        return None
    return f"{t['entry_time'][11:13]}:00"


def hold_bucket(t: dict) -> Optional[str]:
    a, b = _parse_dt(t["entry_time"]), _parse_dt(t.get("exit_time") or "")
    if a and b and t.get("has_time"):
        m = (b - a).total_seconds() / 60
        return ("Under 5 min" if m < 5 else "5–30 min" if m < 30 else "30 min–2 h" if m < 120
                else "Over 2 h, same day" if a.date() == b.date() else "Overnight to 1 week" if m < 7 * 1440
                else "Over 1 week")
    if t.get("exit_time"):
        days = (dt.date.fromisoformat(t["exit_time"][:10]) - dt.date.fromisoformat(t["entry_time"][:10])).days
        return "Same day (no times)" if days == 0 else "Overnight to 1 week" if days <= 7 else "Over 1 week"
    return None


HOLD_ORDER = ("Under 5 min", "5–30 min", "30 min–2 h", "Over 2 h, same day", "Same day (no times)",
              "Overnight to 1 week", "Over 1 week")


def fmt_hold(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    m = sec / 60
    if m < 60:
        return f"{m:.0f} min"
    if m < 1440:
        return f"{m / 60:.1f} h"
    return f"{m / 1440:.1f} days"


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------
def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as err:
        raise JournalError(f"Journal file {path.name} couldn't be read ({err}). Nothing was changed.") from None


def _clean_text(v, limit: int) -> str:
    s = str(v or "").replace("\x00", "").strip()
    return s[:limit]


def parse_tags(raw: str) -> List[str]:
    out = []
    for part in re.split(r"[,;\n]", str(raw or "")):
        p = re.sub(r"\s+", " ", part).strip()[:40]
        if p and p.lower() not in [o.lower() for o in out]:
            out.append(p)
    return out[:20]


class JournalStore:
    def __init__(self, root: Path, clock=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    # -- paths --------------------------------------------------------------
    @property
    def fills_path(self) -> Path:
        return self.root / "fills.json"

    @property
    def manual_path(self) -> Path:
        return self.root / "manual.json"

    @property
    def notes_path(self) -> Path:
        return self.root / "notes.json"

    @property
    def imports_path(self) -> Path:
        return self.root / "imports.json"

    # -- reading ------------------------------------------------------------
    def notes(self) -> Dict[str, dict]:
        return _read_json(self.notes_path, {})

    def imports(self) -> List[dict]:
        return _read_json(self.imports_path, [])

    def trades(self) -> List[dict]:
        """All trades (imported + manual) with their notes, newest entry first."""
        with self.lock:
            fills = _read_json(self.fills_path, [])
            manual = _read_json(self.manual_path, [])
            notes = self.notes()
        out = match_trades([dict(f) for f in fills]) + [manual_trade(m) for m in manual]
        accounts = {f["id"]:fill_account(f) for f in fills}
        for t in out:
            t["account_suffix"] = ", ".join(sorted({accounts.get(fid, "") for fid in t.get("fill_ids", [])} - {""}))
            n = notes.get(t["id"], {})
            t["setup"] = n.get("setup", "")
            t["strategy"] = n.get("strategy", "")
            t["tags"] = n.get("tags", [])
            t["mistakes"] = n.get("mistakes", [])
            t["notes"] = n.get("notes", "")
        out.sort(key=lambda t: (t["entry_time"], t["id"]), reverse=True)
        return out

    def trade(self, trade_id: str) -> Optional[dict]:
        if not TRADE_ID_RE.match(trade_id or ""):
            return None
        return next((t for t in self.trades() if t["id"] == trade_id), None)

    def fills_for(self, ids: Sequence[str]) -> List[dict]:
        want = set(ids)
        return sorted((f for f in _read_json(self.fills_path, []) if f["id"] in want),
                      key=lambda f: (f["when"], f.get("seq", 0)))

    def manual_record(self, trade_id: str) -> Optional[dict]:
        return next((m for m in _read_json(self.manual_path, []) if m["id"] == trade_id), None)

    # -- import -------------------------------------------------------------
    def import_csv(self, filename: str, content: bytes) -> dict:
        broker, fills, skipped = read_fills(content, filename)
        if not fills:
            raise JournalError("No stock or ETF trades were found in this file."
                               + (" " + "; ".join(skipped[:3]) if skipped else ""))
        ids = fill_ids(fills)
        with self.lock:
            existing = _read_json(self.fills_path, [])
            known = {f["id"] for f in existing}
            seq = max((f.get("seq", 0) for f in existing), default=0)
            added = []
            for f, fid in zip(fills, ids):
                if fid in known:
                    continue
                seq += 1
                known.add(fid)
                added.append({**f, "id": fid, "seq": seq, "file": _clean_text(filename, 120)})
            if len(existing) + len(added) > MAX_FILLS:
                raise JournalError(f"The journal holds at most {MAX_FILLS:,} imported fills.")
            if added:
                _write_json(self.fills_path, existing + added)
            record = {"when_utc": self.clock().isoformat(timespec="seconds"), "file": _clean_text(filename, 120),
                      "broker": broker, "rows": len(fills), "added": len(added),
                      "duplicates": len(fills) - len(added), "skipped": len(skipped)}
            log = self.imports()
            _write_json(self.imports_path, (log + [record])[-200:])
        record["skipped_examples"] = skipped[:5]
        record["symbols"] = sorted({f["symbol"] for f in fills})
        return record

    # -- manual -------------------------------------------------------------
    def _manual_from_form(self, form: Dict[str, str], trade_id: Optional[str] = None) -> dict:
        symbol = clean_symbol(form.get("symbol", ""))
        if not SYMBOL_RE.match(symbol):
            raise JournalError("Enter a stock symbol, e.g. AAPL.")
        side = form.get("side", "long")
        if side not in ("long", "short"):
            raise JournalError("Side must be Long or Short.")
        qty = parse_number(form.get("qty"))
        if qty is None or qty <= 0 or qty > 1e9:
            raise JournalError("Enter the number of shares (more than 0).")
        entry = parse_number(form.get("entry_price"))
        if entry is None or entry <= 0 or entry > 1e7:
            raise JournalError("Enter the entry price (more than 0).")

        def when(prefix: str, required: bool) -> Optional[str]:
            d, t = form.get(prefix + "_date", "").strip(), form.get(prefix + "_time", "").strip()
            if not d:
                if required:
                    raise JournalError("Enter the entry date.")
                return None
            try:
                day = dt.date.fromisoformat(d)
            except ValueError:
                raise JournalError(f"Dates must look like 2026-10-30 (got {d[:20]}).") from None
            if not t:
                return day.isoformat()
            m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59 or int(m.group(3) or 0) > 59:
                raise JournalError(f"Times must look like 09:31 (got {t[:20]}).")
            return f"{day.isoformat()} {int(m.group(1)):02d}:{m.group(2)}:{int(m.group(3) or 0):02d}"

        entry_time = when("entry", True)
        exit_time = when("exit", False)
        exit_price = parse_number(form.get("exit_price"))
        if (exit_time is None) != (exit_price is None):
            raise JournalError("For a closed trade enter both the exit date and the exit price; "
                               "leave both empty for an open trade.")
        if exit_price is not None and (exit_price <= 0 or exit_price > 1e7):
            raise JournalError("Enter the exit price (more than 0).")
        if exit_time is not None and exit_time < entry_time:
            raise JournalError("The exit is before the entry.")
        fees = parse_number(form.get("fees")) or 0.0
        if fees < 0 or fees > 1e7:
            raise JournalError("Fees can't be negative.")
        if trade_id is None:
            raw = f"{symbol}|{entry_time}|{qty}|{entry}|{self.clock().isoformat()}|{os.urandom(4).hex()}"
            trade_id = "m" + hashlib.sha1(raw.encode()).hexdigest()[:16]
        return {"id": trade_id, "symbol": symbol, "side": side, "qty": qty, "entry_price": entry,
                "entry_time": entry_time, "exit_price": exit_price, "exit_time": exit_time, "fees": fees,
                "created_utc": self.clock().isoformat(timespec="seconds")}

    def add_manual(self, form: Dict[str, str]) -> dict:
        rec = self._manual_from_form(form)
        with self.lock:
            manual = _read_json(self.manual_path, [])
            if len(manual) >= MAX_MANUAL:
                raise JournalError(f"The journal holds at most {MAX_MANUAL:,} manual trades.")
            _write_json(self.manual_path, manual + [rec])
            self._save_notes(rec["id"], form)
        return rec

    def update_manual(self, trade_id: str, form: Dict[str, str]) -> dict:
        with self.lock:
            manual = _read_json(self.manual_path, [])
            idx = next((i for i, m in enumerate(manual) if m["id"] == trade_id), None)
            if idx is None:
                raise JournalError("That trade no longer exists.")
            rec = self._manual_from_form(form, trade_id)
            rec["created_utc"] = manual[idx].get("created_utc", rec["created_utc"])
            manual[idx] = rec
            _write_json(self.manual_path, manual)
            self._save_notes(trade_id, form)
        return rec

    # -- notes --------------------------------------------------------------
    def _save_notes(self, trade_id: str, form: Dict[str, str]) -> None:
        notes = self.notes()
        entry = {"setup": _clean_text(form.get("setup"), 60), "strategy": _clean_text(form.get("strategy"), 80),
                 "tags": parse_tags(form.get("tags", "")), "mistakes": parse_tags(form.get("mistakes", "")),
                 "notes": _clean_text(form.get("notes"), 5000)}
        if any(entry.values()):
            notes[trade_id] = entry
        else:
            notes.pop(trade_id, None)
        _write_json(self.notes_path, notes)

    def save_notes(self, trade_id: str, form: Dict[str, str]) -> None:
        with self.lock:
            if self.trade(trade_id) is None:
                raise JournalError("That trade no longer exists.")
            self._save_notes(trade_id, form)

    # -- delete -------------------------------------------------------------
    def delete(self, trade_id: str) -> str:
        """Manual trade: removed. Imported trade: its fills are removed (re-importing the file brings it back)."""
        with self.lock:
            t = self.trade(trade_id)
            if t is None:
                raise JournalError("That trade no longer exists.")
            if t["source"] == "manual":
                manual = [m for m in _read_json(self.manual_path, []) if m["id"] != trade_id]
                _write_json(self.manual_path, manual)
            else:
                fills = _read_json(self.fills_path, [])
                mine = set(t["fill_ids"])
                _write_json(self.fills_path, [f for f in fills if f["id"] not in mine])
            notes = self.notes()
            if notes.pop(trade_id, None) is not None:
                _write_json(self.notes_path, notes)
        return f"Deleted the {t['symbol']} trade from {t['entry_time'][:10]}."

    def clear(self) -> str:
        with self.lock:
            n = len(self.trades())
            for p in (self.fills_path, self.manual_path, self.notes_path, self.imports_path):
                if p.exists():
                    _write_json(p, [] if p != self.notes_path else {})
        return f"Deleted all {n} trades and the import history."

    # -- export -------------------------------------------------------------
    def export_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Symbol", "Side", "Status", "Shares", "Entry time", "Entry price", "Exit time", "Exit price",
                    "Fees", "Gross P&L", "Net P&L", "Return %", "Setup", "Strategy", "Tags", "Mistakes", "Notes",
                    "Source"])
        for t in sorted(self.trades(), key=lambda t: (t["entry_time"], t["id"])):
            w.writerow([t["symbol"], t["side"], t["status"], _g(t["qty"]), t["entry_time"], _g(t["entry_price"]),
                        t["exit_time"] or "", _g(t["exit_price"]), _g(t["fees"]), _g(t["gross"]), _g(t["pnl"]),
                        _g(t["return_pct"]), t["setup"], t["strategy"], "; ".join(t["tags"]),
                        "; ".join(t["mistakes"]), t["notes"], t["source"]])
        return buf.getvalue()


def _g(v) -> str:
    if v is None:
        return ""
    return f"{round(v, 6):g}" if abs(v) < 1e15 else str(v)


def filter_trades(trades: List[dict], q: Dict[str, str]) -> List[dict]:
    """Filters from the query string: from, to (entry date), symbol, side, status, tag, setup, strategy."""
    out = trades
    if q.get("from"):
        out = [t for t in out if t["entry_time"][:10] >= q["from"]]
    if q.get("to"):
        out = [t for t in out if t["entry_time"][:10] <= q["to"]]
    if q.get("symbol"):
        s = clean_symbol(q["symbol"])
        out = [t for t in out if t["symbol"] == s]
    if q.get("side") in ("long", "short"):
        out = [t for t in out if t["side"] == q["side"]]
    if q.get("status") in ("open", "closed"):
        out = [t for t in out if t["status"] == q["status"]]
    if q.get("result") in ("win", "loss"):
        out = [t for t in out if t["status"] == "closed" and (t["pnl"] > 0.005 if q["result"] == "win"
                                                               else t["pnl"] < -0.005)]
    if q.get("tag"):
        tag = q["tag"].lower()
        out = [t for t in out if tag in [x.lower() for x in t["tags"] + t["mistakes"]]]
    if q.get("setup"):
        out = [t for t in out if t["setup"].lower() == q["setup"].lower()]
    if q.get("strategy"):
        out = [t for t in out if t["strategy"].lower() == q["strategy"].lower()]
    if q.get("day"):
        out = [t for t in out if exit_day(t) == q["day"]]
    return out
