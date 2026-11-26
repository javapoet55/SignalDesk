"""Daily price data from Massive.com (formerly Polygon.io) for strategy runs.

* Daily bars come from the grouped-daily endpoint (one request = every US stock
  for one day) and are stored compactly on the volume, one gzip CSV per day:
      /data/prices/daily/YYYY-MM-DD.csv.gz   (ticker,o,h,l,c,v)
* For a strategy run, only the needed tickers (+ SPY for the regime filter) are
  assembled into a history_cache.json in exactly the format the V5 scripts use:
      {"tickers": [...], "history": {"YYYY-MM-DD": {"SYM": {"o","h","l","c","v"}}}}
* Optional: today's bar-so-far from the snapshot endpoint (intraday preview).

The API key is sent only in an Authorization header and is redacted from every
error and log line.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = dt.timezone(dt.timedelta(hours=-5))

DEFAULT_BASE = "https://api.massive.com"
GROUPED_PATH = "/v2/aggs/grouped/locale/us/market/stocks/{date}"
SNAPSHOT_PATH = "/v2/snapshot/locale/us/markets/stocks/tickers"
USER_AGENT = "finviz-gainers-agent-prices/1.0"


class PriceError(Exception):
    pass


class PriceAuthError(PriceError):
    pass


def _redactor(secret: str) -> Callable[[object], str]:
    def redact(text: object) -> str:
        out = str(text)
        if secret:
            out = out.replace(secret, "***").replace(urllib.parse.quote(secret, safe=""), "***")
        return out
    return redact


def clean_api_key(raw: str) -> str:
    """Undo common paste mistakes: surrounding spaces/quotes, a 'Bearer ' or 'apiKey=' prefix."""
    key = (raw or "").strip().strip("'\"").strip()
    for prefix in ("Bearer ", "bearer ", "apiKey=", "apikey=", "MASSIVE_API_KEY="):
        if key.startswith(prefix):
            key = key[len(prefix):].strip().strip("'\"").strip()
    return key


def key_hint(key: str) -> str:
    """Non-secret description so the user can compare with their dashboard."""
    if not key:
        return "not set"
    tail = key[-4:] if len(key) >= 12 else ""
    return f"{len(key)} characters" + (f", ending …{tail}" if tail else "") + (
        "; contains a space (probably a paste error)" if " " in key else "")


class MassiveClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE, min_interval: float = 0.25,
                 max_retries: int = 8, timeout: float = 60.0,
                 urlopen: Optional[Callable] = None, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda m: None):
        api_key = clean_api_key(api_key)
        if not api_key:
            raise PriceAuthError("MASSIVE_API_KEY is not set. Add it in Railway → Variables.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self._urlopen = urlopen or urllib.request.urlopen
        self._sleep = sleep
        self.redact = _redactor(api_key)
        self._log = lambda m: log(self.redact(m))
        self._last = 0.0
        self.requests_made = 0

    def _get_json(self, path: str, params: Optional[Dict[str, str]] = None) -> dict:
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params, safe=",") if params else "")
        attempt = 0
        while True:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            self.requests_made += 1
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.api_key}",
                                                       "User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with self._urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                return json.loads(body.decode("utf-8"))
            except urllib.error.HTTPError as err:
                if err.code in (401, 403):
                    detail = ""
                    try:
                        body = json.loads(err.read().decode("utf-8"))
                        detail = body.get("error") or body.get("message") or ""  # Massive uses "error"
                    except Exception:
                        pass
                    hint = ("the key itself was not recognised: check MASSIVE_API_KEY is your REST API key "
                            "from the Massive dashboard (not a Flat Files/S3 key), copied in full"
                            if err.code == 401 else "your Massive plan may not include this data")
                    raise PriceAuthError(self.redact(
                        f"Massive refused the request (HTTP {err.code}" + (f": {detail}" if detail else "")
                        + f") — {hint}.")) from None
                if err.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    retry_after = None
                    try:
                        retry_after = float(err.headers.get("Retry-After")) if err.headers else None
                    except (TypeError, ValueError):
                        pass
                    # free plans allow ~5 requests/minute: wait long enough for the window to reset
                    delay = (retry_after if retry_after is not None
                             else (13.0 if err.code == 429 else 2.0 * (2 ** attempt)))
                    self._log(f"Massive HTTP {err.code}; waiting {delay:.0f}s before retrying")
                    self._sleep(delay)
                    attempt += 1
                    continue
                raise PriceError(self.redact(f"Massive HTTP {err.code} for {path}")) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as err:
                if attempt < 3:
                    self._sleep(2.0 * (2 ** attempt))
                    attempt += 1
                    continue
                raise PriceError(self.redact(f"Could not reach Massive: {err}")) from None

    def test(self, now_utc: Optional[dt.datetime] = None) -> str:
        """One request for the last completed session; returns a plain-English verdict."""
        day = last_completed_session(now_utc or dt.datetime.now(dt.timezone.utc)).isoformat()
        try:
            rows = self.grouped_daily(day)
        except PriceError as err:
            return f"❌ Key {key_hint(self.api_key)}: {err}"
        return (f"✅ Key works ({key_hint(self.api_key)}): Massive returned {len(rows):,} stocks for {day}."
                if rows else f"✅ Key accepted ({key_hint(self.api_key)}), but no prices for {day} yet.")

    def grouped_daily(self, day: str) -> List[dict]:
        data = self._get_json(GROUPED_PATH.format(date=day), {"adjusted": "true"})
        return data.get("results") or []

    def snapshot(self, tickers: Iterable[str]) -> List[dict]:
        tickers = sorted(set(tickers))
        out: List[dict] = []
        for i in range(0, len(tickers), 250):  # keep URLs short
            data = self._get_json(SNAPSHOT_PATH, {"tickers": ",".join(tickers[i:i + 250])})
            out.extend(data.get("tickers") or [])
        return out


def to_massive_symbol(sym: str) -> str:
    """FinViz writes class shares as BRK-B; Massive uses BRK.B."""
    return sym.replace("-", ".")


def last_completed_session(now_utc: dt.datetime) -> dt.date:
    """Latest weekday whose daily bar should exist (after 17:00 New York time)."""
    et = now_utc.astimezone(ET)
    d = et.date() if et.hour >= 17 else et.date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def market_is_open(now_utc: dt.datetime) -> bool:
    et = now_utc.astimezone(ET)
    return et.weekday() < 5 and (dt.time(9, 30) <= et.time() < dt.time(16, 0))


class PriceStore:
    def __init__(self, root: Path, history_days: int = 150, log: Callable[[str], None] = lambda m: None):
        self.root = root
        self.daily_dir = root / "daily"
        self.index_path = root / "fetched.json"
        self.history_days = max(40, history_days)
        self.log = log
        self._lock = threading.Lock()
        self.status: Dict[str, object] = {"running": False, "done": 0, "total": 0, "error": None,
                                          "finished_utc": None}
        self.daily_dir.mkdir(parents=True, exist_ok=True)

    # -- index of fetched days (holidays are stored with count 0 so they are not refetched)
    def _index(self) -> Dict[str, int]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_index(self, idx: Dict[str, int]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, indent=0, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def trading_days(self) -> List[str]:
        return sorted(d for d, n in self._index().items() if n > 0 and (self.daily_dir / f"{d}.csv.gz").is_file())

    def wanted_days(self, now_utc: dt.datetime) -> List[str]:
        end = last_completed_session(now_utc)
        days: List[str] = []
        d = end
        # history_days trading days ~ history_days weekdays (holidays just come back empty)
        while len(days) < self.history_days:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d -= dt.timedelta(days=1)
        return sorted(days)

    def update(self, client: MassiveClient, now_utc: Optional[dt.datetime] = None) -> Dict[str, object]:
        """Fetch every wanted day that is not stored yet. Safe to call repeatedly."""
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        if not self._lock.acquire(blocking=False):
            return {"skipped": "another price update is running"}
        try:
            idx = self._index()
            todo = [d for d in self.wanted_days(now_utc) if d not in idx]
            self.status.update(running=True, done=0, total=len(todo), error=None)
            fetched = 0
            for i, day in enumerate(todo, 1):
                rows = client.grouped_daily(day)
                if rows:
                    self._write_day(day, rows)
                    idx[day] = len(rows)
                elif day < last_completed_session(now_utc).isoformat():
                    idx[day] = 0  # market holiday: remember, don't refetch
                # an empty result for the most recent session means "not published yet": retry next time
                self._save_index(idx)
                fetched += 1
                self.status["done"] = i
            self.status.update(running=False, finished_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
            return {"fetched": fetched, "days_stored": len(self.trading_days())}
        except PriceError as err:
            self.status.update(error=str(err))
            raise
        except OSError as err:
            self.status.update(error=f"Could not save price data: {err}")
            raise PriceError(f"Could not save price data: {err}") from None
        finally:
            self.status["running"] = False
            self._lock.release()

    def _write_day(self, day: str, rows: List[dict]) -> None:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ticker", "o", "h", "l", "c", "v"])
        for r in rows:
            t = r.get("T")
            if not t or r.get("c") is None:
                continue
            w.writerow([t, r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v", 0)])
        path = self.daily_dir / f"{day}.csv.gz"
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
        os.replace(tmp, path)

    def _read_day(self, day: str, wanted: set) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        with gzip.open(self.daily_dir / f"{day}.csv.gz", "rt", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r["ticker"] in wanted:
                    out[r["ticker"]] = {"o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
                                        "c": float(r["c"]), "v": float(r["v"] or 0)}
        return out

    def build_cache(self, finviz_tickers: Iterable[str], include_intraday: bool = False,
                    client: Optional[MassiveClient] = None, now_utc: Optional[dt.datetime] = None,
                    extra: Iterable[str] = ("SPY",)) -> Tuple[dict, Dict[str, object]]:
        """history_cache.json-format dict for the given tickers (+ SPY). Returns (cache, info)."""
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        symbols = {to_massive_symbol(t) for t in finviz_tickers} | set(extra)
        history: Dict[str, Dict[str, dict]] = {}
        for day in self.trading_days():
            bars = self._read_day(day, symbols)
            if bars:
                history[day] = bars
        info: Dict[str, object] = {"days": len(history), "first_day": min(history) if history else None,
                                   "last_day": max(history) if history else None, "intraday": None}
        if include_intraday:
            today = now_utc.astimezone(ET).date()
            if today.weekday() >= 5:
                info["intraday"] = "weekend: no intraday bar"
            elif today.isoformat() in history:
                info["intraday"] = "today's full daily bar is already stored"
            elif client is None:
                info["intraday"] = "MASSIVE_API_KEY not set"
            else:
                snap = client.snapshot(symbols)
                bars: Dict[str, dict] = {}
                for s in snap:
                    day_bar = s.get("day") or {}
                    c = day_bar.get("c")
                    if s.get("ticker") and c:  # 0/None before the open
                        bars[s["ticker"]] = {"o": float(day_bar.get("o") or c), "h": float(day_bar.get("h") or c),
                                             "l": float(day_bar.get("l") or c), "c": float(c),
                                             "v": float(day_bar.get("v") or 0)}
                if bars:
                    history[today.isoformat()] = bars
                    info["intraday"] = f"today's bar so far, as of {now_utc.astimezone(ET):%H:%M} ET"
                    info["last_day"] = today.isoformat()
                else:
                    info["intraday"] = "no intraday prices yet (before the open?)"
        present = sorted({s for bars in history.values() for s in bars})
        return {"tickers": present, "history": history}, info
