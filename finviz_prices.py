"""Daily price history from FinViz Elite (the same token the scans use) for strategy runs.

For each ticker (+ SPY for the V5 regime filter) the server calls FinViz Elite's
price export  /quote_export?t=TICKER&p=d&auth=TOKEN  which returns CSV:
    Date, Open, High, Low, Close, Volume
The bars are assembled into a history_cache.json-format dict, exactly what the
V5 scripts read:  {"tickers": [...], "history": {"YYYY-MM-DD": {"SYM": {"o","h","l","c","v"}}}}

* Runs on the server side only; the token never reaches uploaded strategy code.
* An on-disk cache (/data/prices/finviz/<SYM>.csv) avoids re-downloading:
  a file downloaded after the most recent market close (16:05 ET, i.e. 1:05 pm
  Pacific, once the closing prices have settled) is reused until the next close,
  because no new daily bar can appear before then. So a 1:10 pm download serves
  every later run that evening, overnight, on weekends, and "latest close" runs
  the next trading day. Intraday runs while the market is open still download
  fresh prices (reused for at most 10 minutes). A file downloaded before the
  latest close is never reused after it.
* "Latest close" mode drops a bar dated today while the US market is still open;
  "intraday" mode keeps it (FinViz Elite quotes are real-time).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import finviz_agent as fa

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = dt.timezone(dt.timedelta(hours=-5))

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
MARKET_CLOSE_ET = dt.time(16, 0)
CLOSE_SETTLE = dt.timedelta(minutes=5)  # closing auction prints settle in the first minutes after 16:00 ET
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d", "%d-%b-%Y", "%b %d, %Y", "%Y/%m/%d")


class FinvizPriceError(Exception):
    pass


def _norm(h: str) -> str:
    return re.sub(r"[^a-z]", "", h.lower())


def parse_date(raw: str) -> Optional[str]:
    """'10/23/2026', '2026-10-23', '2026-10-23 00:00:00', '10/23/2026 4:00 PM' ... -> '2026-10-23'."""
    s = (raw or "").strip().strip('"')
    if not s:
        return None
    candidates = [s, s.split("T")[0], s.split(" ")[0]]
    for c in candidates:
        for fmt in DATE_FORMATS:
            try:
                return dt.datetime.strptime(c, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _num(raw: str) -> Optional[float]:
    v, _ = fa.parse_number(raw)
    return v


def parse_quote_csv(text: str) -> Tuple[Dict[str, dict], Dict[str, object]]:
    """Return ({date: {o,h,l,c,v}}, info). Header names and date formats are matched flexibly."""
    text = (text or "").lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return {}, {"rows": 0, "bad_rows": 0, "header": []}
    header = [_norm(h) for h in rows[0]]
    want = {"date": ("date", "datetime", "time", "timestamp"), "o": ("open",), "h": ("high",),
            "l": ("low",), "c": ("close", "adjclose", "last"), "v": ("volume", "vol")}
    idx: Dict[str, int] = {}
    for key, names in want.items():
        for i, h in enumerate(header):
            if h in names:
                idx[key] = i
                break
    if not all(k in idx for k in ("date", "o", "h", "l", "c")):
        raise FinvizPriceError(f"Unexpected FinViz price columns: {rows[0]}")
    bars: Dict[str, dict] = {}
    bad = 0
    for r in rows[1:]:
        try:
            day = parse_date(r[idx["date"]])
            o, h, l, c = (_num(r[idx[k]]) for k in ("o", "h", "l", "c"))
            v = _num(r[idx["v"]]) if "v" in idx and idx["v"] < len(r) else 0.0
        except IndexError:
            bad += 1
            continue
        if not day or None in (o, h, l, c) or c <= 0:
            bad += 1
            continue
        bars[day] = {"o": o, "h": h, "l": l, "c": c, "v": v or 0.0}  # later duplicate rows win
    return bars, {"rows": len(rows) - 1, "bad_rows": bad, "header": rows[0]}


# ── Intraday bars and multi-day / multi-minute candles ────────────────────────
# FinViz Elite quote_export intraday periods (checked on a live account, Oct 2026):
#   i1, i3, i5   ~2-3 weeks, include pre/after-market (04:00-20:00 ET)
#   i15          ~3 weeks, regular session only
#   i30          ~9 months, regular session only
#   h            uneven (9:30-10:00, then on the clock hour), so not used: hours are built from i30.
# Rows look like "10/30/2026 15:00 PM,open,high,low,close,volume": New York time, bar START, 24-hour
# clock with an AM/PM suffix (a true 12-hour clock is handled too).
INTRADAY_BASES = (30, 15, 5, 3, 1)
SESSION_OPEN_MIN, SESSION_CLOSE_MIN = 9 * 60 + 30, 16 * 60
MAX_CANDLES = 500  # per ticker; plenty for indicators, keeps the price file small
_INTRADAY_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?\s*$")


def parse_intraday_stamp(raw: str) -> Optional[Tuple[str, int]]:
    """'10/30/2026 15:00 PM' -> ('2026-10-30', minutes after midnight New York time)."""
    m = _INTRADAY_RE.match((raw or "").strip().strip('"'))
    if not m:
        return None
    mo, da, yr, hh, mi, ap = int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), (m[6] or "").upper()
    if ap == "PM" and hh < 12:
        hh += 12
    elif ap == "AM" and hh == 12:
        hh = 0
    if hh > 23 or mi > 59:
        return None
    try:
        day = dt.date(yr, mo, da).isoformat()
    except ValueError:
        return None
    return day, hh * 60 + mi


def parse_intraday_csv(text: str) -> Dict[Tuple[str, int], dict]:
    """{(day, minute): {o,h,l,c,v}} for intraday rows; unreadable rows are skipped."""
    rows = [r for r in csv.reader(io.StringIO((text or "").lstrip("\ufeff"))) if any(c.strip() for c in r)]
    if not rows:
        return {}
    header = [_norm(h) for h in rows[0]]
    idx = {}
    for key, names in {"t": ("date", "datetime", "time", "timestamp"), "o": ("open",), "h": ("high",), "l": ("low",),
                       "c": ("close", "last"), "v": ("volume", "vol")}.items():
        for i, h in enumerate(header):
            if h in names:
                idx[key] = i
                break
    if not all(k in idx for k in ("t", "o", "h", "l", "c")):
        raise FinvizPriceError(f"Unexpected FinViz intraday columns: {rows[0]}")
    out = {}
    for r in rows[1:]:
        try:
            when = parse_intraday_stamp(r[idx["t"]])
            o, h, l, c = (_num(r[idx[k]]) for k in ("o", "h", "l", "c"))
            v = _num(r[idx["v"]]) if "v" in idx and idx["v"] < len(r) else 0.0
        except IndexError:
            continue
        if when and None not in (o, h, l, c) and c > 0:
            out[when] = {"o": o, "h": h, "l": l, "c": c, "v": v or 0.0}
    return out


def intraday_base(minutes: int) -> int:
    """The FinViz bar size the candles are built from: the largest one that divides the candle size."""
    return next(b for b in INTRADAY_BASES if minutes % b == 0)


def minute_candles(bars: Dict[Tuple[str, int], dict], minutes: int, now_utc: dt.datetime,
                   include_forming: bool) -> Dict[str, dict]:
    """Regular-session bars (9:30-16:00 New York) grouped into `minutes`-long candles that start at
    9:30 + k*minutes each day (TradingView's default session alignment). Key = candle start
    'YYYY-MM-DD HH:MM'. A candle still forming at `now` is kept only when include_forming."""
    now_et = now_utc.astimezone(ET)
    today, now_min = now_et.date().isoformat(), now_et.hour * 60 + now_et.minute
    groups: Dict[Tuple[str, int], List[Tuple[int, dict]]] = {}
    for (day, mins), bar in bars.items():
        if not SESSION_OPEN_MIN <= mins < SESSION_CLOSE_MIN:
            continue
        start = SESSION_OPEN_MIN + (mins - SESSION_OPEN_MIN) // minutes * minutes
        groups.setdefault((day, start), []).append((mins, bar))
    out = {}
    for (day, start), items in sorted(groups.items()):
        end = min(start + minutes, SESSION_CLOSE_MIN)
        if (day > today or (day == today and now_min < end)) and not include_forming:
            continue  # still forming
        items.sort(key=lambda x: x[0])
        bs = [b for _, b in items]
        out[f"{day} {start // 60:02d}:{start % 60:02d}"] = {
            "o": bs[0]["o"], "h": max(b["h"] for b in bs), "l": min(b["l"] for b in bs), "c": bs[-1]["c"],
            "v": math.fsum(b["v"] for b in bs)}
    return out


def day_candles(cache: dict, n: int, max_candles: int = MAX_CANDLES) -> dict:
    """A daily history_cache -> n-trading-day candles per ticker. Each ticker's newest candle ends on its
    last day; oldest days that don't fill a candle are left out. Key = the candle's last day.
    high = highest, low = lowest, open = first open, close = last close, volume = total."""
    if n <= 1:
        return cache
    per: Dict[str, List[Tuple[str, dict]]] = {}
    for day in sorted(cache["history"]):
        for sym, bar in cache["history"][day].items():
            per.setdefault(sym, []).append((day, bar))
    history: Dict[str, Dict[str, dict]] = {}
    for sym, bars in per.items():
        candles = []
        for i in range(len(bars) % n, len(bars), n):
            blk = [b for _, b in bars[i:i + n]]
            candles.append((bars[i + n - 1][0], {
                "o": float(blk[0].get("o", blk[0]["c"])), "h": max(float(b["h"]) for b in blk),
                "l": min(float(b["l"]) for b in blk), "c": float(blk[-1]["c"]),
                "v": math.fsum(float(b.get("v") or 0.0) for b in blk)}))
        for day, bar in candles[-max_candles:]:
            history.setdefault(day, {})[sym] = bar
    return {"tickers": sorted(per), "history": history}


def last_close(now_utc: dt.datetime) -> dt.datetime:
    """The most recent weekday 16:00 ET at or before now (UTC). Market holidays are not known here,
    so a holiday counts as a close: that only causes one extra download, never stale data."""
    et = now_utc.astimezone(ET)
    day = et.date()
    for _ in range(8):
        if day.weekday() < 5:
            close = dt.datetime.combine(day, MARKET_CLOSE_ET, tzinfo=ET)
            if close <= et:
                return close.astimezone(dt.timezone.utc)
        day -= dt.timedelta(days=1)
    raise AssertionError("no weekday in the last 8 days")  # pragma: no cover


def market_open_today(now_utc: dt.datetime) -> Tuple[str, bool]:
    et = now_utc.astimezone(ET)
    return et.date().isoformat(), et.weekday() < 5 and dt.time(9, 30) <= et.time() < dt.time(16, 0)


class FinvizPriceSource:
    def __init__(self, client: "fa.FinvizClient", cache_dir: Path, history_days: int = 150,
                 ttl_seconds: int = 600, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = time.time):
        self.client = client
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.history_days = max(40, history_days)
        self.ttl = ttl_seconds
        self.log = log
        self._clock = clock

    def reusable(self, path: Path, now_utc: dt.datetime, fresh_intraday: bool) -> bool:
        """May the saved download at `path` be used instead of a new one?

        Only if it was downloaded after the most recent close had settled (16:05 ET): it then holds
        that close's final bar, and no newer daily bar exists until the next close. When the run
        needs today's live bar (intraday, market open), it must also be under ttl seconds old."""
        try:
            fetched = path.stat().st_mtime
        except OSError:
            return False
        if fetched < (last_close(now_utc) + CLOSE_SETTLE).timestamp():
            return False
        return not fresh_intraday or now_utc.timestamp() - fetched < self.ttl

    def _fetch(self, sym: str, use_cache: bool = True, now_utc: Optional[dt.datetime] = None,
               fresh_intraday: bool = False, period: str = "d") -> Tuple[str, bool]:
        """(CSV text, True if it came from the saved download)."""
        folder = self.cache_dir if period == "d" else self.cache_dir / f"p_{period}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{sym}.csv"
        now_utc = now_utc or dt.datetime.fromtimestamp(self._clock(), dt.timezone.utc)
        if use_cache and self.reusable(path, now_utc, fresh_intraday):
            return path.read_text(encoding="utf-8"), True
        text = self.client.quote_history(sym, period)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return text, False

    def bars_for(self, sym: str, use_cache: bool = True, now_utc: Optional[dt.datetime] = None,
                 fresh_intraday: bool = False) -> Tuple[Dict[str, dict], bool]:
        text, reused = self._fetch(sym, use_cache, now_utc, fresh_intraday)
        bars, _ = parse_quote_csv(text)
        return bars, reused

    def build_cache(self, tickers: Iterable[str], include_intraday: bool = False,
                    now_utc: Optional[dt.datetime] = None, progress: Callable[[str], None] = lambda m: None,
                    history_days: Optional[int] = None, cancel_check=lambda: None) -> Tuple[dict, Dict[str, object]]:
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        today, open_now = market_open_today(now_utc)
        wanted: List[str] = []
        for t in ["SPY"] + [t.strip().upper() for t in tickers]:
            if t and SYMBOL_RE.match(t) and t not in wanted:
                wanted.append(t)
        history: Dict[str, Dict[str, dict]] = {}
        missing: Dict[str, str] = {}
        dropped_today = False
        reused = 0
        fresh_intraday = include_intraday and open_now  # today's live bar needed: no long reuse
        for i, sym in enumerate(wanted, 1):
            cancel_check()
            try:
                bars, was_reused = self.bars_for(sym, now_utc=now_utc, fresh_intraday=fresh_intraday)
                reused += was_reused
            except fa.AuthError as err:
                if sym == "SPY" or not history:
                    raise FinvizPriceError(str(err)) from None  # the token itself is the problem
                missing[sym] = "FinViz returned a web page instead of prices (unknown ticker?)"
                continue
            except (fa.FetchError, FinvizPriceError, OSError) as err:
                if sym == "SPY":
                    raise FinvizPriceError(f"Could not get SPY prices from FinViz: {err}") from None
                missing[sym] = str(err)
                continue
            if not bars:
                missing[sym] = "no price rows returned"
                continue
            days = sorted(bars)
            if not include_intraday and open_now and days[-1] == today:
                days = days[:-1]  # today's bar is still forming; "latest close" excludes it
                dropped_today = True
            for d in days[-(history_days or self.history_days):]:
                history.setdefault(d, {})[sym] = bars[d]
            if i % 50 == 0:
                progress(f"  FinViz prices: {i} of {len(wanted)} tickers")
        last = max(history) if history else None
        note = None
        if include_intraday:
            note = (f"today's bar so far from FinViz, as of {now_utc.astimezone(ET):%H:%M} ET"
                    if last == today and open_now else "no partial bar for today (market closed)")
        info: Dict[str, object] = {
            "days": len(history), "first_day": min(history) if history else None, "last_day": last,
            "tickers_requested": len(wanted), "tickers_with_prices": len(wanted) - len(missing),
            "missing": missing, "intraday": note, "dropped_today": dropped_today,
            "reused": reused, "downloaded": len(wanted) - reused,
        }
        present = sorted({s for bars in history.values() for s in bars})
        return {"tickers": present, "history": history}, info

    def build_minute_cache(self, tickers: Iterable[str], minutes: int, include_forming: bool = False,
                           now_utc: Optional[dt.datetime] = None, progress: Callable[[str], None] = lambda m: None,
                           max_candles: int = MAX_CANDLES, cancel_check=lambda: None) -> Tuple[dict, Dict[str, object]]:
        """history_cache-format dict of `minutes`-long candles (keys 'YYYY-MM-DD HH:MM', New York time)."""
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        base = intraday_base(minutes)
        _, open_now = market_open_today(now_utc)
        wanted: List[str] = []
        for t in ["SPY"] + [t.strip().upper() for t in tickers]:
            if t and SYMBOL_RE.match(t) and t not in wanted:
                wanted.append(t)
        history: Dict[str, Dict[str, dict]] = {}
        missing: Dict[str, str] = {}
        reused = 0
        saved_ttl, self.ttl = self.ttl, 60  # minute data goes stale fast while the market is open
        try:
            for i, sym in enumerate(wanted, 1):
                cancel_check()
                try:
                    text, was = self._fetch(sym, now_utc=now_utc, fresh_intraday=open_now, period=f"i{base}")
                    reused += was
                    candles = minute_candles(parse_intraday_csv(text), minutes, now_utc, include_forming)
                except fa.AuthError as err:
                    if sym == "SPY" or not history:
                        raise FinvizPriceError(str(err)) from None
                    missing[sym] = "FinViz returned a web page instead of prices (unknown ticker?)"
                    continue
                except (fa.FetchError, FinvizPriceError, OSError) as err:
                    if sym == "SPY":
                        raise FinvizPriceError(f"Could not get SPY {base}-minute prices from FinViz: {err}") from None
                    missing[sym] = str(err)
                    continue
                if not candles:
                    missing[sym] = "no regular-session minute bars returned"
                    continue
                for k in sorted(candles)[-max_candles:]:
                    history.setdefault(k, {})[sym] = candles[k]
                if i % 50 == 0:
                    progress(f"  FinViz prices: {i} of {len(wanted)} tickers")
        finally:
            self.ttl = saved_ttl
        keys = sorted(history)
        info: Dict[str, object] = {
            "days": len({k[:10] for k in keys}), "first_day": keys[0] if keys else None,
            "last_day": keys[-1] if keys else None, "tickers_requested": len(wanted),
            "tickers_with_prices": len(wanted) - len(missing), "missing": missing, "intraday": None,
            "dropped_today": False, "reused": reused, "downloaded": len(wanted) - reused, "base_minutes": base,
            "candles": len(keys)}
        return {"tickers": sorted({s for b in history.values() for s in b}), "history": history}, info

    def test(self) -> str:
        """One fresh request for SPY; returns a plain-English verdict the user can check against a chart."""
        try:
            text, _ = self._fetch("SPY", use_cache=False)
            bars, info = parse_quote_csv(text)
        except fa.AuthError as err:
            return f"❌ {err}"
        except (fa.FetchError, FinvizPriceError, OSError) as err:
            return f"❌ FinViz price export failed: {err}"
        if not bars:
            return f"❌ FinViz returned no usable price rows for SPY (columns: {info.get('header')})."
        days = sorted(bars)
        last = bars[days[-1]]
        first_raw = text.lstrip("﻿").splitlines()[1] if len(text.splitlines()) > 1 else ""
        return (f"✅ FinViz prices work: SPY {len(days):,} daily bars, {days[0]} → {days[-1]}. "
                f"Latest close {last['c']:,.2f} (compare with your FinViz chart). "
                f"Raw first row: {first_raw[:80]}"
                + (f". {info['bad_rows']} unreadable row(s) skipped." if info.get("bad_rows") else ""))
