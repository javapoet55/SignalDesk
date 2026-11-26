#!/usr/bin/env python3
"""TradingView watchlist sync for the FinViz agent (standard library only).

After every scan, the scan's tickers go into a new TradingView watchlist named after the
run ("FinViz Scan · Oct 31 13:04"). After every strategy run, that strategy's BUY tickers go
into their own new list ("FinViz BUY · <strategy> · Oct 31 13:05"). Lists from runs older than
TV_KEEP_DAYS are deleted. (TV_LIST_MODE=replace keeps one list per scan/strategy instead.)

It talks to TradingView's official MCP server (https://mcp.tradingview.com/mcp) with
your own TradingView sign-in (OAuth 2.1 + PKCE; the agent registers itself once).
Tokens are kept in DATA_DIR/tradingview/auth.json (owner-only file); nothing is
stored in Railway variables, and uploaded strategies can't read it.

Why each ticker is checked first: TradingView accepts made-up symbols into a watchlist
without complaint, and FinViz tickers carry no exchange. Every ticker is looked up as
NASDAQ:/NYSE:/AMEX:/CBOE: (class shares as BRK.B) and only symbols TradingView knows
are added. Results are remembered for 30 days.

Environment variables (all optional)
  TRADINGVIEW_SYNC         on (default) / off
  TV_SCAN_WATCHLIST        name of the scan list (default: FinViz Scan)
  TV_BUY_WATCHLIST_PREFIX  prefix of the BUY lists (default: "FinViz BUY · ")
  TV_SYNC_SCANS / TV_SYNC_BUYS   on (default) / off
  TV_LIST_MODE             dated (default): a new list for every run, e.g. "FinViz Scan · Oct 31 13:04";
                           replace: one list per scan/strategy, overwritten each run
  TV_KEEP_DAYS             dated lists from runs older than this are deleted (default 7; 0 = keep all).
                           Only lists this agent made and you haven't renamed are ever deleted.
  TV_MAX_SYMBOLS           most symbols put in one list (default 1000)
  TV_PUBLIC_URL            this page's address, if RAILWAY_PUBLIC_DOMAIN isn't set
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

VERSION = "1.0.0"
MCP_URL = "https://mcp.tradingview.com/mcp"
OAUTH_BASE = "https://www.tradingview.com/mcp/oauth"
SCOPES = "mcp:read mcp:tools"
PROTOCOL_VERSION = "2025-06-18"
EXCHANGES = ("NASDAQ", "NYSE", "AMEX", "CBOE")
BATCH = 50           # get_symbol_data_batch takes up to 50 symbols
CHUNK = 100          # symbols per add/remove call
MIN_INTERVAL = 0.65  # seconds between MCP calls (TradingView allows ~100 a minute)
RESOLVED_TTL = 30 * 86400
UNRESOLVED_TTL = 86400
CHECK_EVERY = 30
BACKOFF = 300
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
RESULT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}_[A-Za-z0-9_.-]{1,80}$")


class TVError(Exception):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _on(value: Optional[str], default: bool = True) -> bool:
    v = (value or "").strip().lower()
    return default if not v else v not in ("0", "off", "false", "no")


def _write_private(path: Path, data: dict) -> None:
    """Atomic write, readable by the server's user only."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _dedupe(items: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(i for i in items if i))


def _chunks(items: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def clean_tickers(items: Iterable[str]) -> List[str]:
    """Upper-case tickers, once each, in order; anything that isn't a ticker is dropped."""
    return _dedupe(t for t in (str(x).strip().upper() for x in items) if TICKER_RE.match(t))


BUY_STATUSES = {"BUY", "BUY SIGNAL", "SIGNAL"}  # fmfi-style, V5-style, V5 dashboard


def signal_tickers(d: Path) -> List[str]:
    """A strategy result's BUY tickers (same rule as the agent's TXT link): with a Status column, only
    BUY rows ('BLOCKED BY REGIME' is not a buy); without one, every row's ticker."""
    f = Path(d) / "signals.csv"
    if f.is_symlink() or not f.is_file():
        return []
    with f.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        has_status = "Status" in (reader.fieldnames or [])
        return _dedupe((r.get("Ticker") or "").strip() for r in reader
                       if not has_status or (r.get("Status") or "").strip().upper() in BUY_STATUSES)


def read_meta(d: Path) -> dict:
    f = Path(d) / "meta.json"
    m = {} if f.is_symlink() else _read_json(f)
    m.setdefault("id", Path(d).name)
    return m


def variants(ticker: str) -> List[str]:
    """FinViz writes class shares as BRK-B; TradingView uses BRK.B."""
    t = ticker.strip().upper()
    return _dedupe([t.replace("-", ".").replace("/", "."), t])


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP
# ─────────────────────────────────────────────────────────────────────────────
def http(method: str, url: str, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
         timeout: float = 30) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": f"finviz-agent-tv-sync/{VERSION}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        try:
            data = err.read()
        except OSError:
            data = b""
        return err.code, {k.lower(): v for k, v in (err.headers or {}).items()}, data
    except (urllib.error.URLError, OSError) as err:
        raise TVError(f"Could not reach TradingView ({getattr(err, 'reason', err)}).") from None


def _error_text(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            return str(data.get("error_description") or data.get("error") or data.get("message") or data)[:300]
    except ValueError:
        pass
    return body.decode("utf-8", errors="replace").strip()[:300] or "no details"


# ─────────────────────────────────────────────────────────────────────────────
#  Sign-in (OAuth 2.1, dynamic client registration, PKCE)
# ─────────────────────────────────────────────────────────────────────────────
class Auth:
    def __init__(self, path: Path, oauth_base: str = OAUTH_BASE, resource: str = MCP_URL,
                 clock: Callable[[], float] = time.time):
        self.path = path
        self.base = oauth_base.rstrip("/")
        self.resource = resource
        self.clock = clock
        self._lock = threading.Lock()
        self._pending: Dict[str, Tuple[str, str, float]] = {}

    def _load(self) -> dict:
        return _read_json(self.path)

    def _save(self, data: dict) -> None:
        _write_private(self.path, data)

    def connected(self) -> bool:
        d = self._load()
        return bool(d.get("refresh_token") or d.get("access_token")) and not d.get("needs_reconnect")

    def info(self) -> dict:
        d = self._load()
        return {"connected": self.connected(), "connected_utc": d.get("connected_utc"),
                "needs_reconnect": bool(d.get("needs_reconnect")), "reason": d.get("reconnect_reason", "")}

    def _register(self, redirect_uri: str) -> str:
        body = json.dumps({"client_name": "FinViz agent (watchlist sync)", "redirect_uris": [redirect_uri],
                           "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                           "token_endpoint_auth_method": "none", "scope": SCOPES}).encode()
        status, _, raw = http("POST", self.base + "/register", body, {"Content-Type": "application/json",
                                                                    "Accept": "application/json"})
        if status not in (200, 201):
            raise TVError(f"TradingView refused to register this agent (HTTP {status}): {_error_text(raw)}")
        try:
            client_id = json.loads(raw)["client_id"]
        except (ValueError, KeyError, TypeError):
            raise TVError("TradingView's registration reply had no client_id.") from None
        d = self._load()
        d.update(client_id=client_id, redirect_uri=redirect_uri)
        self._save(d)
        return client_id

    def begin(self, redirect_uri: str) -> str:
        """The TradingView sign-in address to send the browser to."""
        with self._lock:
            d = self._load()
            client_id = d.get("client_id") if d.get("redirect_uri") == redirect_uri else None
            client_id = client_id or self._register(redirect_uri)
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if now - v[2] < 900}
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            state = secrets.token_urlsafe(24)
            self._pending[state] = (verifier, redirect_uri, now)
        q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPES,
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": self.resource}
        return self.base + "/authorize?" + urllib.parse.urlencode(q)

    def finish(self, query: Dict[str, str]) -> None:
        if query.get("error"):
            raise TVError("TradingView sign-in was not completed: "
                          + (query.get("error_description") or query["error"])[:200])
        with self._lock:
            pending = self._pending.pop(query.get("state", ""), None)
        if pending is None:
            raise TVError("This sign-in link expired or was already used. Click Connect TradingView again.")
        if not query.get("code"):
            raise TVError("TradingView did not send a sign-in code. Click Connect TradingView again.")
        verifier, redirect_uri, _ = pending
        d = self._load()
        self._token({"grant_type": "authorization_code", "code": query["code"], "redirect_uri": redirect_uri,
                     "client_id": d.get("client_id", ""), "code_verifier": verifier, "resource": self.resource},
                    first=True)

    def _token(self, form: dict, first: bool = False) -> dict:
        status, _, raw = http("POST", self.base + "/token", urllib.parse.urlencode(form).encode(),
                              {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        if status != 200:
            text = _error_text(raw)
            if not first and status in (400, 401) and "invalid_grant" in raw.decode("utf-8", "replace"):
                d = self._load()
                d.update(needs_reconnect=True, reconnect_reason="TradingView ended the sign-in. Connect again.")
                d.pop("access_token", None)
                self._save(d)
                raise TVError("TradingView ended this sign-in. Open the TradingView page and click Connect again.")
            raise TVError(f"TradingView sign-in failed (HTTP {status}): {text}")
        try:
            tok = json.loads(raw)
            access = tok["access_token"]
        except (ValueError, KeyError, TypeError):
            raise TVError("TradingView's sign-in reply had no access token.") from None
        d = self._load()
        d.update(access_token=access, expires_at=self.clock() + float(tok.get("expires_in") or 3600) - 60,
                 needs_reconnect=False, reconnect_reason="")
        if tok.get("refresh_token"):
            d["refresh_token"] = tok["refresh_token"]  # TradingView may rotate it on every refresh
        if first:
            d["connected_utc"] = utc_now().isoformat(timespec="seconds")
        self._save(d)
        return d

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            d = self._load()
            if d.get("needs_reconnect"):
                raise TVError("TradingView needs to be connected again (see the TradingView page).")
            if d.get("access_token") and not force_refresh and self.clock() < float(d.get("expires_at") or 0):
                return d["access_token"]
            if not d.get("refresh_token"):
                if d.get("access_token") and not force_refresh:
                    return d["access_token"]
                raise TVError("TradingView is not connected. Open the TradingView page and click Connect.")
            d = self._token({"grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                             "client_id": d.get("client_id", ""), "resource": self.resource})
            return d["access_token"]

    def disconnect(self) -> None:
        with self._lock:
            d = self._load()
            token = d.get("refresh_token") or d.get("access_token")
            if token:
                try:  # best effort: tell TradingView to forget it
                    http("POST", self.base + "/revoke", urllib.parse.urlencode(
                        {"token": token, "client_id": d.get("client_id", "")}).encode(),
                        {"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
                except TVError:
                    pass
            for k in ("access_token", "refresh_token", "expires_at", "connected_utc", "needs_reconnect",
                      "reconnect_reason"):
                d.pop(k, None)
            self._save(d)


# ─────────────────────────────────────────────────────────────────────────────
#  MCP client (streamable HTTP, JSON-RPC)
# ─────────────────────────────────────────────────────────────────────────────
def parse_rpc(body: bytes, ctype: str, want_id: int) -> dict:
    """The JSON-RPC reply with id `want_id`, from a JSON body or an SSE stream."""
    text = body.decode("utf-8", errors="replace")
    messages: List[dict] = []
    if "text/event-stream" in (ctype or "").lower():
        for event in re.split(r"\r?\n\r?\n", text):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            if data.strip():
                try:
                    messages.append(json.loads(data))
                except ValueError:
                    continue
    else:
        try:
            parsed = json.loads(text)
        except ValueError:
            raise TVError(f"TradingView sent an unreadable reply: {text[:200]}") from None
        messages = parsed if isinstance(parsed, list) else [parsed]
    for m in messages:
        if isinstance(m, dict) and m.get("id") == want_id:
            return m
    raise TVError("TradingView's reply did not answer the request.")


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class MCP:
    def __init__(self, url: str, auth: Auth, min_interval: float = MIN_INTERVAL,
                 sleep: Callable[[float], None] = time.sleep):
        self.url = url
        self.auth = auth
        self.min_interval = min_interval
        self.sleep = sleep
        self._lock = threading.Lock()
        self._session: Optional[str] = None
        self._version = PROTOCOL_VERSION
        self._tools: Optional[List[str]] = None
        self._next_id = 0
        self._last = 0.0

    def reset(self) -> None:
        self._session, self._tools = None, None

    def _post(self, payload: dict, token: str) -> Tuple[int, Dict[str, str], bytes]:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {token}"}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
            headers["MCP-Protocol-Version"] = self._version
        try:
            return http("POST", self.url, json.dumps(payload).encode(), headers, timeout=60)
        finally:
            self._last = time.monotonic()

    def _rpc(self, method: str, params: Optional[dict] = None, notify: bool = False) -> dict:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self._next_id += 1
            payload["id"] = self._next_id
        status, headers, body = 0, {}, b""
        for attempt in range(2):
            status, headers, body = self._post(payload, self.auth.access_token(force_refresh=attempt > 0))
            if status != 401:
                break
        if status == 401:
            raise TVError("TradingView rejected the sign-in. Connect again on the TradingView page.")
        if status == 404 and self._session and method != "initialize":
            raise _SessionGone()
        if notify:
            if status >= 400:
                raise TVError(f"TradingView error (HTTP {status}): {_error_text(body)}")
            return {}
        if status >= 400:
            raise TVError(f"TradingView error (HTTP {status}): {_error_text(body)}")
        if method == "initialize" and headers.get("mcp-session-id"):
            self._session = headers["mcp-session-id"]
        msg = parse_rpc(body, headers.get("content-type", ""), payload["id"])
        if msg.get("error"):
            err = msg["error"]
            raise TVError(f"TradingView error: {err.get('message') if isinstance(err, dict) else err}")
        return msg.get("result") or {}

    def _ensure(self) -> None:
        if self._tools is not None:
            return
        self._session = None
        init = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                        "clientInfo": {"name": "finviz-agent", "version": VERSION}})
        self._version = str(init.get("protocolVersion") or PROTOCOL_VERSION)
        self._rpc("notifications/initialized", notify=True)
        tools: List[str] = []
        cursor = None
        for _ in range(20):
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t.get("name", "") for t in res.get("tools") or [] if isinstance(t, dict)]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self._tools = tools

    def tool_name(self, short: str) -> str:
        """Server tool for e.g. 'add_to_watchlist' (TradingView names them like 'mcp-watchlist-add-to-watchlist')."""
        exact = [t for t in self._tools or [] if norm(t) == short]
        ends = [t for t in self._tools or [] if norm(t).endswith("_" + short)]
        found = exact or sorted(ends, key=len)
        if not found:
            raise TVError(f"TradingView's MCP server has no '{short}' tool (it may have changed; it's in beta).")
        return found[0]

    def call(self, short: str, args: dict):
        with self._lock:
            for attempt in range(2):
                try:
                    self._ensure()
                    res = self._rpc("tools/call", {"name": self.tool_name(short), "arguments": args})
                    break
                except _SessionGone:
                    self.reset()
                    if attempt:
                        raise TVError("TradingView keeps ending the session. Try again later.") from None
            texts = [c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict)
                     and c.get("type") == "text"]
            if res.get("isError"):
                raise TVError("TradingView: " + (" ".join(texts).strip()[:300] or "the request failed"))
            if isinstance(res.get("structuredContent"), dict):
                return res["structuredContent"]
            joined = "".join(texts).strip()
            try:
                return json.loads(joined) if joined else {}
            except ValueError:
                return {"text": joined}


class _SessionGone(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  Sync
# ─────────────────────────────────────────────────────────────────────────────
class TVSync:
    def __init__(self, data_dir: Path, reports_dir: Path, results_dir: Path,
                 signal_fn: Callable[[Path], List[str]], read_meta: Callable[[Path], dict],
                 pretty_fn: Callable[[str], str] = lambda s: s, tz=dt.timezone.utc,
                 env: Optional[Dict[str, str]] = None, log: Callable[[str], None] = print,
                 busy_fn: Callable[[], bool] = lambda: False, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        env = dict(env or {})
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)  # tokens live here; strategies run as another user
        except OSError:
            pass
        self.reports_dir, self.results_dir = Path(reports_dir), Path(results_dir)
        self.signal_fn, self.read_meta, self.pretty_fn = signal_fn, read_meta, pretty_fn
        self.tz, self.log, self.busy_fn, self.clock = tz, log, busy_fn, clock
        self.enabled = _on(env.get("TRADINGVIEW_SYNC"))
        self.sync_scans = _on(env.get("TV_SYNC_SCANS"))
        self.sync_buys = _on(env.get("TV_SYNC_BUYS"))
        self.scan_name = (env.get("TV_SCAN_WATCHLIST") or "").strip()[:200] or "FinViz Scan"
        self.buy_prefix = env.get("TV_BUY_WATCHLIST_PREFIX") or "FinViz BUY · "
        self.dated = (env.get("TV_LIST_MODE") or "dated").strip().lower() != "replace"
        try:
            self.keep_days = max(0, int(env.get("TV_KEEP_DAYS") or 7))
        except ValueError:
            self.keep_days = 7
        try:
            self.max_symbols = max(1, min(5000, int(env.get("TV_MAX_SYMBOLS") or 1000)))
        except ValueError:
            self.max_symbols = 1000
        self.public_url = (env.get("TV_PUBLIC_URL") or "").strip().rstrip("/")
        domain = (env.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
        if not self.public_url and domain:
            self.public_url = "https://" + domain
        self.auth = Auth(self.dir / "auth.json", env.get("TV_OAUTH_BASE") or OAUTH_BASE,
                         env.get("TV_MCP_URL") or MCP_URL, clock=clock)
        self.mcp = MCP(env.get("TV_MCP_URL") or MCP_URL, self.auth,
                       min_interval=float(env.get("TV_MIN_INTERVAL") or MIN_INTERVAL), sleep=sleep)
        self.state_path = self.dir / "state.json"
        self.symbols_path = self.dir / "symbols.json"
        self._lock = threading.Lock()
        self._retry_at = 0.0
        self.syncing = False
        self._flash: Optional[Tuple[str, str]] = None

    def set_flash(self, kind: str, text: str) -> None:
        self._flash = (kind, text)

    # ── state ──────────────────────────────────────────────────────────────
    def state(self) -> dict:
        s = _read_json(self.state_path)
        s.setdefault("lists", {})
        s.setdefault("history", [])  # dated lists this agent made: key, source, id, name, ...
        return s

    def _save_state(self, s: dict) -> None:
        _write_private(self.state_path, s)

    def _note(self, **values) -> None:
        s = self.state()
        s.update(values)
        self._save_state(s)

    # ── sign-in ────────────────────────────────────────────────────────────
    def redirect_uri(self, host: str = "", proto: str = "") -> str:
        base = self.public_url
        if not base:
            host = (host or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d{1,5})?", host):
                raise TVError("Can't tell this page's address. Set TV_PUBLIC_URL in Railway → Variables.")
            proto = (proto or "").split(",")[0].strip().lower()
            base = f"{'http' if proto == 'http' else 'https'}://{host}"
        return base + "/tradingview/callback"

    def connect_url(self, host: str = "", proto: str = "") -> str:
        return self.auth.begin(self.redirect_uri(host, proto))

    def finish_connect(self, query: Dict[str, str]) -> str:
        self.auth.finish(query)
        self.mcp.reset()
        self._retry_at = 0.0
        self._note(last_error="", last_error_utc="")
        self.log("TradingView connected")
        threading.Thread(target=self._sync_quietly, args=(True,), daemon=True).start()
        return ("TradingView is connected. Your latest scan and BUY signals are being copied to your "
                "watchlists now; reload this page in a minute.")

    def disconnect(self) -> str:
        self.auth.disconnect()
        self.mcp.reset()
        self.log("TradingView disconnected")
        return "TradingView disconnected. Your watchlists stay in TradingView but won't be updated."

    # ── what to sync ───────────────────────────────────────────────────────
    def latest_scan(self) -> Optional[Path]:
        if not self.reports_dir.is_dir():
            return None
        runs = sorted((p for p in self.reports_dir.iterdir() if p.is_dir() and STAMP_RE.match(p.name)
                       and (p / "tickers.txt").is_file()), key=lambda p: p.name)
        return runs[-1] if runs else None

    @staticmethod
    def scan_tickers(scan: Path) -> List[str]:
        lines = (scan / "tickers.txt").read_text(encoding="utf-8").splitlines()
        line = lines[1] if len(lines) > 1 else ""
        return clean_tickers(line.split(","))

    def latest_results(self) -> Dict[str, Tuple[str, Path, dict]]:
        """Each strategy's newest run, if it finished OK. A newest run that is still going or
        failed leaves that strategy's list as it is (an older run is never sent over a newer one)."""
        newest: Dict[str, Tuple[str, Path, dict]] = {}
        if not self.results_dir.is_dir():
            return {}
        for p in sorted(self.results_dir.iterdir(), key=lambda p: p.name, reverse=True):
            if not (p.is_dir() and RESULT_RE.match(p.name)):
                continue
            m = self.read_meta(p)
            name = str(m.get("strategy") or "")
            if name and name not in newest:
                newest[name] = (p.name, p, m)
        return {k: v for k, v in newest.items() if v[2].get("status") == "ok"}

    # ── TradingView side ───────────────────────────────────────────────────
    def resolve(self, tickers: List[str]) -> Tuple[Dict[str, str], List[str]]:
        """ticker -> EXCHANGE:SYMBOL for every ticker TradingView knows; plus those it doesn't."""
        cache = _read_json(self.symbols_path)
        now = self.clock()
        out: Dict[str, str] = {}
        todo: List[str] = []
        for t in _dedupe(tickers):
            if not TICKER_RE.match(t):
                continue
            c = cache.get(t)
            if isinstance(c, dict) and now - float(c.get("t") or 0) < (RESOLVED_TTL if c.get("tv") else UNRESOLVED_TTL):
                if c.get("tv"):
                    out[t] = c["tv"]
                continue
            todo.append(t)
        trustworthy = True
        for exch in EXCHANGES:
            if not todo:
                break
            cands: Dict[str, str] = {}
            for t in todo:
                for v in variants(t):
                    cands.setdefault(f"{exch}:{v}", t)
            names = list(cands)
            for chunk in _chunks(names, BATCH):
                res = self.mcp.call("get_symbol_data_batch", {"symbols": chunk, "columns": ["name"]})
                if not isinstance(res, dict) or res.get("success") is False:
                    raise TVError("TradingView couldn't look up symbols right now (try again later).")
                if res.get("partial"):
                    trustworthy = False
                data = res.get("data") or {}
                for sym in chunk:  # candidate order: BRK.B before BRK-B
                    if sym in data and cands[sym] not in out:
                        out[cands[sym]] = sym
            todo = [t for t in todo if t not in out]
        for t in out:
            cache[t] = {"tv": out[t], "t": now}
        if trustworthy:
            for t in todo:
                cache[t] = {"tv": None, "t": now}
        _write_private(self.symbols_path, cache)
        return out, [t for t in _dedupe(tickers) if t not in out]

    def _watchlists(self) -> List[dict]:
        res = self.mcp.call("list_watchlists", {})
        lists = res.get("watchlists") if isinstance(res, dict) else res
        return [w for w in lists or [] if isinstance(w, dict)]

    def _known(self, key: str, source: str) -> dict:
        s = self.state()
        if self.dated:
            return next((h for h in s["history"] if h.get("key") == key and h.get("source") == source), {})
        return s["lists"].get(key, {})

    def _ensure_list(self, key: str, name: str, source: str = "") -> Tuple[str, List[str]]:
        known = self._known(key, source)
        lists = self._watchlists()
        own = [w for w in lists if w.get("type", "custom") == "custom" and not w.get("read_only")]
        hit = next((w for w in own if str(w.get("id")) == str(known.get("id") or "")), None)
        hit = hit or next((w for w in own if str(w.get("name", "")) == name), None)
        if hit is None:
            res = self.mcp.call("create_watchlist", {"name": name, "symbols": []})
            hit = res.get("watchlist", res) if isinstance(res, dict) else {}
            if not hit.get("id"):
                raise TVError("TradingView didn't return the new watchlist.")
            self.log(f"TradingView: created watchlist '{name}'")
        return str(hit["id"]), [str(s) for s in hit.get("symbols") or []]

    def push(self, key: str, name: str, tickers: List[str], description: str, source: str) -> dict:
        mapping, missing = self.resolve(tickers)
        symbols = _dedupe(mapping[t] for t in tickers if t in mapping)
        cut = max(0, len(symbols) - self.max_symbols)
        symbols = symbols[:self.max_symbols]
        wl_id, current = self._ensure_list(key, name, source)
        if current != symbols:
            for chunk in _chunks(_dedupe(current), CHUNK):
                self.mcp.call("remove_from_watchlist", {"watchlist_id": wl_id, "symbols": chunk})
            for chunk in _chunks(symbols, CHUNK):
                self.mcp.call("add_to_watchlist", {"watchlist_id": wl_id, "symbols": chunk})
        note = description + (f" · {len(missing)} not found on TradingView" if missing else "") + \
            (f" · {cut} over the {self.max_symbols}-symbol limit left out" if cut else "")
        self.mcp.call("update_watchlist", {"watchlist_id": wl_id, "description": note[:900]})
        rec = {"id": wl_id, "name": name, "source": source, "count": len(symbols), "tickers": len(_dedupe(tickers)),
               "missing": missing[:200], "cut": cut, "synced_utc": utc_now().isoformat(timespec="seconds")}
        s = self.state()
        if self.dated:
            rec.update(key=key, dated=True)
            s["history"] = [h for h in s["history"] if not (h.get("key") == key and h.get("source") == source)]
            s["history"].append(rec)
        s["lists"][key] = rec
        self._save_state(s)
        self.log(f"TradingView: '{name}' now has {len(symbols)} symbols ({source})"
                 + (f"; not found: {', '.join(missing[:15])}" if missing else ""))
        return rec

    def _stamp_dt(self, stamp: str) -> Optional[dt.datetime]:
        try:
            return dt.datetime.strptime(str(stamp)[:17], "%Y-%m-%d_%H%M%S").replace(tzinfo=self.tz)
        except ValueError:
            return None

    def list_name(self, base: str, source: str) -> str:
        """'FinViz Scan · Oct 31 13:04' in dated mode; just the base name otherwise."""
        w = self._stamp_dt(source) if self.dated else None
        return (f"{base} · {w:%b %d %H:%M}" if w else base)[:200]

    def _done(self, s: dict, key: str, source: str) -> bool:
        if self.dated:
            return (f"{key}|{source}" in s.get("expired", []) or
                    any(h.get("key") == key and h.get("source") == source for h in s["history"]))
        return s["lists"].get(key, {}).get("source") == source

    def cleanup(self, legacy: Optional[List[dict]] = None) -> List[str]:
        """Dated mode: delete this agent's lists from runs older than TV_KEEP_DAYS, and the old
        single lists from before dated mode. A list is deleted only if it still has the name the agent
        gave it, so a list you renamed (or copied) is always kept."""
        if not self.dated:
            return []
        s = self.state()
        now = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
        old = []
        if self.keep_days:
            cutoff = now - dt.timedelta(days=self.keep_days)
            old = [h for h in s["history"] if (self._stamp_dt(h.get("source", "")) or now) < cutoff]
        targets = old + list(legacy or [])
        if not targets:
            return []
        live = {str(w.get("id")): w for w in self._watchlists()}
        gone = []
        for h in targets:
            w = live.get(str(h.get("id")))
            if w is not None and str(w.get("name", "")) == h.get("name"):
                self.mcp.call("delete_watchlist", {"watchlist_id": str(h["id"])})
                self.log(f"TradingView: deleted old watchlist '{h.get('name')}'")
                gone.append(str(h.get("name")))
        drop = {(str(h.get("id")), h.get("source")) for h in old}
        s = self.state()
        s["history"] = [h for h in s["history"] if (str(h.get("id")), h.get("source")) not in drop]
        # remember runs whose list was removed, so an old run is never sent (and deleted) again
        s["expired"] = (s.get("expired", []) + [f"{h.get('key')}|{h.get('source')}" for h in old])[-1000:]
        s["lists"] = {k: v for k, v in s["lists"].items() if v.get("dated")}
        self._save_state(s)
        return gone

    def _when(self, stamp: str) -> str:
        try:
            w = dt.datetime.strptime(stamp[:17], "%Y-%m-%d_%H%M%S").replace(tzinfo=self.tz)
            return f"{w:%b %d %H:%M %Z}"
        except ValueError:
            return stamp

    def pending(self, force: bool = False) -> List[Tuple[str, str, List[str], str, str]]:
        """(key, watchlist name, tickers, description, source) for each list that is out of date."""
        s = self.state()
        jobs = []
        if self.sync_scans and not self.busy_fn():
            scan = self.latest_scan()
            if scan is not None and (force or not self._done(s, "scan", scan.name)):
                t = self.scan_tickers(scan)
                jobs.append(("scan", self.list_name(self.scan_name, scan.name), t,
                             f"FinViz agent · scan {self._when(scan.name)} · {len(t)} tickers", scan.name))
        if self.sync_buys:
            for strat, (rid, d, meta) in sorted(self.latest_results().items()):
                key = "buy:" + strat
                if force or not self._done(s, key, rid):
                    t = clean_tickers(self.signal_fn(d))
                    jobs.append((key, self.list_name((self.buy_prefix + self.pretty_fn(strat))[:170], rid), t,
                                 f"FinViz agent · {strat} BUY signals · as of {meta.get('asof', '?')} · "
                                 f"scan {self._when(str(meta.get('scan', '')))} · {len(t)} tickers", rid))
        return jobs

    def sync(self, force: bool = False) -> List[str]:
        """Bring every out-of-date list up to date. Returns one line per list."""
        if not self.auth.connected():
            return []
        with self._lock:
            self.syncing = True
            try:
                done, errors = [], []
                legacy = [r for r in self.state()["lists"].values() if not r.get("dated")] if self.dated else []
                for key, name, tickers, desc, source in self.pending(force):
                    try:
                        r = self.push(key, name, tickers, desc, source)
                        done.append(f"{name}: {r['count']} symbols")
                    except TVError as err:
                        errors.append(f"{name}: {err}")
                        self.log(f"TradingView sync failed for '{name}': {err}")
                if not errors:
                    try:
                        self.cleanup(legacy)
                    except TVError as err:
                        errors.append(f"Removing old lists: {err}")
                if errors:
                    self._note(last_error=" | ".join(errors)[:1000], last_error_utc=utc_now().isoformat(timespec="seconds"))
                    raise TVError(" | ".join(errors)[:1000])
                if done:
                    self._note(last_error="", last_error_utc="", last_sync_utc=utc_now().isoformat(timespec="seconds"))
                return done
            finally:
                self.syncing = False

    def _sync_quietly(self, force: bool = False) -> None:
        try:
            self.sync(force)
            self._retry_at = 0.0
        except TVError:
            self._retry_at = self.clock() + BACKOFF
        except Exception as err:  # never let a surprise stop the agent
            self._note(last_error=f"{type(err).__name__}: {err}"[:500], last_error_utc=utc_now().isoformat(timespec="seconds"))
            self.log(f"TradingView sync error: {type(err).__name__}: {err}")
            self._retry_at = self.clock() + BACKOFF

    def sync_now(self) -> str:
        if not self.auth.connected():
            raise TVError("Connect TradingView first.")
        if self.syncing:
            return "A TradingView sync is already running."
        threading.Thread(target=self._sync_quietly, args=(True,), daemon=True).start()
        return "Copying your latest scan and BUY signals to TradingView now. Reload in a minute to see the counts."

    def loop(self, stop: threading.Event) -> None:
        """Background thread: copy each new scan and strategy result as soon as it's finished."""
        if not self.enabled:
            self.log("TradingView sync is off (TRADINGVIEW_SYNC=off)")
            return
        while not stop.is_set():
            if self.auth.connected() and self.clock() >= self._retry_at:
                try:
                    if self.pending():
                        self._sync_quietly()
                except Exception as err:  # keep the loop alive
                    self.log(f"TradingView check failed: {type(err).__name__}: {err}")
            stop.wait(CHECK_EVERY)

    # ── page ───────────────────────────────────────────────────────────────
    def _local(self, iso: Optional[str]) -> str:
        try:
            return f"{dt.datetime.fromisoformat(iso).astimezone(self.tz):%a %b %d, %H:%M %Z}"
        except (TypeError, ValueError):
            return "—"

    def page_body(self, flash: str = "") -> str:
        e = html.escape
        info, s = self.auth.info(), self.state()
        if self._flash:
            kind, text = self._flash
            self._flash = None
            flash += f"<div class='{'okbox' if kind == 'ok' else 'warn'}' role='status'>{e(text)}</div>"
        out = ["<h1>TradingView watchlists</h1>",
               "<div class='muted'>After every scan and every strategy run, the agent copies the tickers into your "
               "TradingView watchlists, so they're ready when you open TradingView.</div>", flash]
        if not self.enabled:
            out.append("<div class='warn'>Sync is turned off (TRADINGVIEW_SYNC=off in Railway → Variables).</div>")
        card = ["<div class='card' id='tv-connection'>"]
        if info["connected"]:
            card.append(f"<div class='v ok'>Connected</div><div class='muted'>Since {e(self._local(info['connected_utc']))}"
                        "</div><div class='forms'>"
                        "<form method='post' action='/tradingview/sync'><button type='submit'>Sync now</button></form>"
                        "<form method='post' action='/tradingview/disconnect'><button type='submit' class='ghost'>"
                        "Disconnect</button></form></div>")
        else:
            if info["needs_reconnect"]:
                card.append(f"<div class='warn'>{e(info['reason'] or 'TradingView needs to be connected again.')}</div>")
            card.append("<div class='v'>Not connected</div><p class='muted'>You'll sign in to TradingView and approve "
                        "access for this agent. It only reads and updates watchlists; your password is never seen "
                        "here.</p><p><a class='tvlink' href='/tradingview/connect'>Connect TradingView ↗</a></p>")
        card.append("</div>")
        out += card
        if s.get("last_error"):
            out.append(f"<div class='warn'>Last problem ({e(self._local(s.get('last_error_utc')))}): "
                       f"{e(s['last_error'])}</div>")
        if self.dated:
            rows = sorted(((h.get("key", ""), h) for h in s["history"]),
                          key=lambda kv: (str(kv[1].get("source", ""))[:17], kv[0] != "scan"), reverse=True)[:60]
            out.append("<h2 id='tv-lists'>Your FinViz lists in TradingView</h2>")
        else:
            rows = sorted(s["lists"].items(), key=lambda kv: (kv[0] != "scan", kv[1].get("name", "")))
            out.append("<h2 id='tv-lists'>Watchlists kept up to date</h2>")
        if rows:
            out.append("<div class='card wrap'><table><thead><tr><th>Watchlist</th><th class='n'>Symbols</th>"
                       "<th>From</th><th>Updated</th></tr></thead><tbody>")
            for key, r in rows:
                miss = r.get("missing") or []
                extra = (f"<div class='muted' style='font-size:12.5px'>Not on TradingView: {e(', '.join(miss[:20]))}"
                         f"{' …' if len(miss) > 20 else ''}</div>" if miss else "")
                if r.get("cut"):
                    extra += f"<div class='muted' style='font-size:12.5px'>{r['cut']} left out (limit)</div>"
                link = (f"<a href='https://www.tradingview.com/watchlists/{e(str(r.get('id', '')), quote=True)}/' "
                        f"target='_blank' rel='noopener noreferrer'>{e(r.get('name', ''))}</a>")
                out.append(f"<tr><td class='name'>{link}{extra}</td><td class='n'>{r.get('count', 0)}</td>"
                           f"<td>{'scan' if key == 'scan' else 'BUY signals'} · {e(self._when(str(r.get('source', ''))))}</td>"
                           f"<td>{e(self._local(r.get('synced_utc')))}</td></tr>")
            out.append("</tbody></table></div>")
        else:
            out.append("<p class='muted'>None yet. They appear after the first sync.</p>")
        out.append(
            "<div class='card' id='tv-sync'><div class='v'>How it works</div><ul class='muted' style='margin:6px 0 0 18px;padding:0'>"
            f"<li><b>{e(self.scan_name)}</b> gets every ticker from a scan.</li>"
            f"<li><b>{e(self.buy_prefix)}&lt;strategy&gt;</b> gets that strategy's BUY tickers from a run "
            "(an empty list means no BUY signals that run).</li>"
            + ((f"<li>Every run gets its <b>own new list</b>, named with the run's date and time. Lists from runs "
                f"more than <b>{self.keep_days} day{'s' if self.keep_days != 1 else ''}</b> old are deleted "
                "automatically; to keep one, rename it in TradingView (renamed lists are never deleted).</li>"
                if self.keep_days else "<li>Every run gets its <b>own new list</b>, named with the run's date and "
                "time. Old lists are kept until you delete them.</li>") if self.dated else
               "<li>Each list is <b>replaced</b> each time, so it always matches the latest run. Don't add your own "
               "symbols to these lists; use another watchlist for that.</li>") +
            "<li>Tickers TradingView doesn't know are skipped and listed above. Checks run every 30 seconds.</li>"
            "</ul></div>")
        return "".join(out)
