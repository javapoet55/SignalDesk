"""Tests for tv_sync.py: a fake TradingView (sign-in + MCP server) runs on localhost.

The fake copies behaviour seen on the real server (Oct 2026): unknown symbols are accepted
into a watchlist silently, duplicated symbols in one call fail with 422, symbol lookups
answer in batches with a 'missing' list, and tools are named like 'mcp-watchlist-...'.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tv_sync as tvs  # noqa: E402

KNOWN = {"NASDAQ:AAPL", "NASDAQ:NVDA", "NYSE:BRK.B", "AMEX:GLL", "CBOE:ASTX", "NYSE:GPN", "NASDAQ:SLGB",
         "NYSE:HGV", "NASDAQ:ZJYL"}
TOOLS = ["mcp-watchlist-list-watchlists", "mcp-watchlist-get-watchlist", "mcp-watchlist-create-watchlist",
         "mcp-watchlist-delete-watchlist", "mcp-watchlist-add-to-watchlist", "mcp-watchlist-remove-from-watchlist",
         "mcp-watchlist-update-watchlist", "mcp-tv-get-symbol-data-batch", "mcp-tv-get-symbol-data"]


class FakeTV:
    def __init__(self):
        self.clients = {}
        self.challenge = {}
        self.access = {"A0"}
        self.refresh = {}
        self.n = 0
        self.sessions = set()
        self.lists = {}
        self.next_id = 1000
        self.calls = []
        self.sse = False
        self.drop_sessions = False
        self.expire_access = False
        self.revoked = []
        self.partial = False
        self.lock = threading.Lock()

    def token_pair(self):
        self.n += 1
        a, r = f"A{self.n}", f"R{self.n}"
        self.access = {a}
        self.refresh = {r}
        return {"access_token": a, "refresh_token": r, "token_type": "Bearer", "expires_in": 3600}

    def tool(self, name, args):
        self.calls.append((name, args))
        if name.endswith("list-watchlists"):
            return {"watchlists": list(self.lists.values())}
        if name.endswith("create-watchlist"):
            self.next_id += 1
            wl = {"id": self.next_id, "name": args.get("name", ""), "symbols": list(args.get("symbols") or []),
                  "type": "custom"}
            self.lists[self.next_id] = wl
            return {"watchlist": wl}
        if name.endswith("symbol-data-batch"):
            syms = args["symbols"]
            assert len(syms) <= 50, "batch over 50"
            data = {s: {"name": s.split(":")[1]} for s in syms if s in KNOWN}
            return {"count": len(data), "data": data, "partial": self.partial,
                    "missing": [{"symbol": s, "reason": "Quote data not found"} for s in syms if s not in data],
                    "success": True}
        wl = self.lists.get(int(args["watchlist_id"]))
        if wl is None:
            raise ValueError("watchlists api: 404 not found")
        if name.endswith("delete-watchlist"):
            del self.lists[wl["id"]]
            return {"deleted": True, "watchlist_id": str(wl["id"])}
        if name.endswith("add-to-watchlist"):
            syms = args["symbols"]
            if len(set(syms)) != len(syms):
                raise ValueError("watchlists api: 422 duplicated_symbols: Some symbols are duplicated")
            assert len(syms) <= 100
            for s in syms:
                if s in wl["symbols"]:
                    wl["symbols"].remove(s)
                wl["symbols"].append(s)
            return {"symbols": syms, "watchlist_id": str(wl["id"])}
        if name.endswith("remove-from-watchlist"):
            wl["symbols"] = [s for s in wl["symbols"] if s not in args["symbols"]]
            return {"symbols": [], "watchlist_id": str(wl["id"])}
        if name.endswith("update-watchlist"):
            if "description" in args:
                wl["description"] = args["description"]
            if "name" in args:
                wl["name"] = args["name"]
            return {"watchlist": wl}
        raise ValueError("unknown tool " + name)


def make_handler(fake: FakeTV):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, obj=None, headers=None, raw=None, ctype="application/json"):
            body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            with fake.lock:
                return self.route(body)

        def route(self, body):
            p = self.path
            if p == "/oauth/register":
                d = json.loads(body)
                assert d["token_endpoint_auth_method"] == "none"
                cid = f"client{len(fake.clients) + 1}"
                fake.clients[cid] = d["redirect_uris"]
                return self.reply(201, {"client_id": cid})
            if p == "/oauth/token":
                f = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}
                if f["grant_type"] == "authorization_code":
                    want = fake.challenge.get(f["code"])
                    got = base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                    if want != got or f["client_id"] not in fake.clients or \
                            f["redirect_uri"] not in fake.clients[f["client_id"]]:
                        return self.reply(400, {"error": "invalid_grant", "error_description": "bad code"})
                    return self.reply(200, fake.token_pair())
                if f["grant_type"] == "refresh_token":
                    if f["refresh_token"] not in fake.refresh:
                        return self.reply(400, {"error": "invalid_grant"})
                    return self.reply(200, fake.token_pair())
                return self.reply(400, {"error": "unsupported_grant_type"})
            if p == "/oauth/revoke":
                fake.revoked.append(body.decode())
                return self.reply(200, {})
            if p == "/mcp":
                auth = self.headers.get("Authorization", "")
                if fake.expire_access:
                    fake.expire_access = False
                    fake.access = set()
                if auth[7:] not in fake.access:
                    return self.reply(401, {"error": "invalid_token"})
                msg = json.loads(body)
                sid = self.headers.get("Mcp-Session-Id")
                if msg["method"] == "initialize":
                    sid = f"s{len(fake.sessions) + 1}"
                    fake.sessions.add(sid)
                    return self.answer(msg, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                             "serverInfo": {"name": "fake"}}, {"Mcp-Session-Id": sid})
                if fake.drop_sessions:
                    fake.drop_sessions = False
                    fake.sessions = set()
                if sid not in fake.sessions:
                    return self.reply(404, {"error": "session not found"})
                if msg["method"] == "notifications/initialized":
                    return self.reply(202)
                if msg["method"] == "tools/list":
                    cur = (msg.get("params") or {}).get("cursor")
                    half = len(TOOLS) // 2
                    if not cur:
                        return self.answer(msg, {"tools": [{"name": t} for t in TOOLS[:half]], "nextCursor": "p2"})
                    return self.answer(msg, {"tools": [{"name": t} for t in TOOLS[half:]]})
                if msg["method"] == "tools/call":
                    try:
                        res = fake.tool(msg["params"]["name"], msg["params"]["arguments"])
                        out = {"content": [{"type": "text", "text": json.dumps(res)}], "isError": False}
                    except ValueError as err:
                        out = {"content": [{"type": "text", "text": str(err)}], "isError": True}
                    return self.answer(msg, out)
            return self.reply(404, {"error": "nope"})

        def answer(self, msg, result, headers=None):
            obj = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
            if fake.sse:
                raw = ("event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"})
                       + "\n\nevent: message\ndata: " + json.dumps(obj) + "\n\n").encode()
                return self.reply(200, headers=headers, raw=raw, ctype="text/event-stream")
            return self.reply(200, obj, headers)
    return H


def write_scan(reports: Path, stamp: str, tickers):
    d = reports / stamp
    d.mkdir(parents=True)
    (d / "tickers.txt").write_text(f"Run: {stamp[:10]} {stamp[11:13]}:{stamp[13:15]}\n" + ",".join(tickers) + "\n")
    return d


def write_result(results: Path, rid: str, strategy: str, status: str, buys, scan="2026-10-30_131000"):
    d = results / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": rid, "strategy": strategy, "status": status,
                                             "asof": "2026-10-30", "scan": scan}))
    (d / "buys.txt").write_text(",".join(buys))
    return d


def signal_fn(d: Path):
    t = (d / "buys.txt").read_text().strip()
    return [x for x in t.split(",") if x]


def read_meta(d: Path):
    return json.loads((d / "meta.json").read_text())


class Base(unittest.TestCase):
    MODE = "replace"

    def setUp(self):
        self.fake = FakeTV()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.fake))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.tmp = Path(tempfile.mkdtemp())
        self.reports, self.results = self.tmp / "reports", self.tmp / "strategies" / "results"
        self.reports.mkdir(parents=True)
        self.results.mkdir(parents=True)
        self.logs = []
        self.env = {"TV_OAUTH_BASE": base + "/oauth", "TV_MCP_URL": base + "/mcp", "TV_MIN_INTERVAL": "0",
                    "RAILWAY_PUBLIC_DOMAIN": "agent.example.app", "TV_LIST_MODE": self.MODE}
        self.busy = False
        self.sync = self.make()

    def make(self, **env):
        return tvs.TVSync(self.tmp / "tradingview", self.reports, self.results, signal_fn=signal_fn,
                          read_meta=read_meta, pretty_fn=lambda s: s.replace("_", " ").title(),
                          env={**self.env, **env}, log=self.logs.append, busy_fn=lambda: self.busy)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def connect(self, sync=None):
        sync = sync or self.sync
        url = sync.connect_url()
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        self.assertEqual(q["redirect_uri"], "https://agent.example.app/tradingview/callback")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["resource"], self.env["TV_MCP_URL"])
        self.fake.challenge["code1"] = q["code_challenge"]
        sync.auth.finish({"code": "code1", "state": q["state"]})  # what the callback does, without the thread
        return q

    def wl(self, name):
        return next(w for w in self.fake.lists.values() if w["name"] == name)


class SignIn(Base):
    def test_connect_registers_once_and_stores_tokens_privately(self):
        self.connect()
        self.assertTrue(self.sync.auth.connected())
        self.assertEqual(len(self.fake.clients), 1)
        auth = self.tmp / "tradingview" / "auth.json"
        self.assertEqual(stat.S_IMODE(os.stat(auth).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp / "tradingview").st_mode), 0o700)
        self.assertEqual(json.loads(auth.read_text())["refresh_token"], "R1")
        self.sync.connect_url()  # same address: the registration is reused
        self.assertEqual(len(self.fake.clients), 1)

    def test_wrong_or_reused_state_is_refused(self):
        url = self.sync.connect_url()
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        with self.assertRaises(tvs.TVError):
            self.sync.auth.finish({"code": "x", "state": "not-" + state})
        self.fake.challenge["c"] = "wrong"
        with self.assertRaises(tvs.TVError):
            self.sync.auth.finish({"code": "c", "state": state})
        with self.assertRaises(tvs.TVError):  # a state can be used only once
            self.sync.auth.finish({"code": "c", "state": state})
        self.assertFalse(self.sync.auth.connected())

    def test_valid_code_with_wrong_state_is_refused(self):
        url = self.sync.connect_url()
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        self.fake.challenge["good"] = q["code_challenge"]  # the code itself would be accepted
        with self.assertRaisesRegex(tvs.TVError, "expired or was already used"):
            self.sync.auth.finish({"code": "good", "state": "forged"})
        self.assertFalse(self.sync.auth.connected())
        self.sync.auth.finish({"code": "good", "state": q["state"]})
        self.assertTrue(self.sync.auth.connected())

    def test_user_declines_on_tradingview(self):
        with self.assertRaisesRegex(tvs.TVError, "access_denied|not completed"):
            self.sync.auth.finish({"error": "access_denied"})

    def test_refresh_rotates_and_401_retries_once(self):
        self.connect()
        d = json.loads((self.tmp / "tradingview" / "auth.json").read_text())
        d["expires_at"] = 0
        (self.tmp / "tradingview" / "auth.json").write_text(json.dumps(d))
        write_scan(self.reports, "2026-10-30_131000", ["AAPL"])
        self.sync.sync()
        self.assertEqual(json.loads((self.tmp / "tradingview" / "auth.json").read_text())["refresh_token"], "R2")
        self.fake.expire_access = True  # server drops the access token mid-way: one refresh, then retry
        write_scan(self.reports, "2026-10-31_131000", ["NVDA"])
        self.sync.sync()
        self.assertEqual(self.wl("FinViz Scan")["symbols"], ["NASDAQ:NVDA"])

    def test_revoked_sign_in_asks_to_reconnect(self):
        self.connect()
        self.fake.refresh = set()
        d = json.loads((self.tmp / "tradingview" / "auth.json").read_text())
        d["expires_at"] = 0
        (self.tmp / "tradingview" / "auth.json").write_text(json.dumps(d))
        write_scan(self.reports, "2026-10-30_131000", ["AAPL"])
        with self.assertRaises(tvs.TVError):
            self.sync.sync()
        self.assertFalse(self.sync.auth.connected())
        self.assertIn("Connect", self.sync.page_body())
        self.assertIn("ended", self.sync.page_body())

    def test_disconnect_revokes_and_forgets(self):
        self.connect()
        self.sync.disconnect()
        self.assertFalse(self.sync.auth.connected())
        self.assertTrue(self.fake.revoked)
        self.assertNotIn("refresh_token", json.loads((self.tmp / "tradingview" / "auth.json").read_text()))

    def test_redirect_address(self):
        s = self.make(RAILWAY_PUBLIC_DOMAIN="", TV_PUBLIC_URL="")
        self.assertEqual(s.redirect_uri("my.host:8080", "http"), "http://my.host:8080/tradingview/callback")
        self.assertEqual(s.redirect_uri("x.up.railway.app", "https"), "https://x.up.railway.app/tradingview/callback")
        for bad in ("evil.com/x", "a b", "", "x\r\nSet-Cookie: y"):
            with self.assertRaises(tvs.TVError):
                s.redirect_uri(bad, "https")
        self.assertEqual(self.make(TV_PUBLIC_URL="https://a.b/").redirect_uri("ignored"), "https://a.b/tradingview/callback")


class Sync(Base):
    def test_scan_and_buy_lists_with_exchange_lookup(self):
        self.connect()
        write_scan(self.reports, "2026-10-30_131000",
                   ["AAPL", "BRK-B", "GLL", "ASTX", "FAKEQ", "AAPL", "bad ticker!", "NVDA"])
        write_result(self.results, "2026-10-30_131200_fmfi_scanner", "fmfi_scanner", "ok", ["GPN", "SLGB", "NOPE1"])
        done = self.sync.sync()
        self.assertEqual(len(done), 2)
        scan = self.wl("FinViz Scan")
        self.assertEqual(scan["symbols"], ["NASDAQ:AAPL", "NYSE:BRK.B", "AMEX:GLL", "CBOE:ASTX", "NASDAQ:NVDA"])
        self.assertIn("1 not found", scan["description"])
        buy = self.wl("FinViz BUY · Fmfi Scanner")
        self.assertEqual(buy["symbols"], ["NYSE:GPN", "NASDAQ:SLGB"])
        st = self.sync.state()["lists"]
        self.assertEqual(st["scan"]["missing"], ["FAKEQ"])
        self.assertEqual(st["buy:fmfi_scanner"]["source"], "2026-10-30_131200_fmfi_scanner")
        self.assertIn("FinViz Scan", self.sync.page_body())
        self.assertIn("FAKEQ", self.sync.page_body())

    def test_nothing_new_means_no_calls_then_new_scan_replaces(self):
        self.connect()
        write_scan(self.reports, "2026-10-30_131000", ["AAPL", "NVDA", "GLL"])
        self.sync.sync()
        self.fake.calls.clear()
        self.assertEqual(self.sync.pending(), [])
        self.assertEqual(self.sync.sync(), [])
        self.assertEqual(self.fake.calls, [])
        write_scan(self.reports, "2026-10-31_131000", ["GLL", "HGV", "AAPL"])
        self.sync.sync()
        self.assertEqual(self.wl("FinViz Scan")["symbols"], ["AMEX:GLL", "NYSE:HGV", "NASDAQ:AAPL"])
        looked = [a for n, a in self.fake.calls if n.endswith("batch")]
        self.assertTrue(all("NASDAQ:AAPL" not in a["symbols"] for a in looked), "cached symbols looked up again")

    def test_scan_not_sent_while_a_scan_is_running(self):
        self.connect()
        write_scan(self.reports, "2026-10-30_131000", ["AAPL"])
        self.busy = True
        self.assertEqual(self.sync.pending(), [])
        self.busy = False
        self.assertEqual(len(self.sync.pending()), 1)

    def test_newest_result_running_or_failed_keeps_list(self):
        self.connect()
        write_result(self.results, "2026-10-30_131200_fmfi", "fmfi", "ok", ["GPN"])
        self.sync.sync()
        write_result(self.results, "2026-10-31_131200_fmfi", "fmfi", "running", [])
        self.assertEqual(self.sync.pending(), [])
        shutil.rmtree(self.results / "2026-10-31_131200_fmfi")
        write_result(self.results, "2026-10-31_131200_fmfi", "fmfi", "error", [])
        self.assertEqual(self.sync.pending(), [])
        self.assertEqual(self.wl("FinViz BUY · Fmfi")["symbols"], ["NYSE:GPN"])

    def test_no_buys_empties_the_list_and_two_strategies_get_two_lists(self):
        self.connect()
        write_result(self.results, "2026-10-30_131200_fmfi", "fmfi", "ok", ["GPN", "HGV"])
        write_result(self.results, "2026-10-30_131300_macd_v5", "macd_v5", "ok", ["AAPL"])
        self.sync.sync()
        write_result(self.results, "2026-10-31_131200_fmfi", "fmfi", "ok", [])
        self.sync.sync()
        self.assertEqual(self.wl("FinViz BUY · Fmfi")["symbols"], [])
        self.assertEqual(self.wl("FinViz BUY · Macd V5")["symbols"], ["NASDAQ:AAPL"])

    def test_existing_list_reused_by_name_and_recreated_if_deleted(self):
        self.fake.lists[5] = {"id": 5, "name": "FinViz Scan", "symbols": ["NASDAQ:OLD"], "type": "custom"}
        self.connect()
        write_scan(self.reports, "2026-10-30_131000", ["AAPL"])
        self.sync.sync()
        self.assertEqual(self.fake.lists[5]["symbols"], ["NASDAQ:AAPL"])
        del self.fake.lists[5]
        write_scan(self.reports, "2026-10-31_131000", ["NVDA"])
        self.sync.sync()
        self.assertEqual(self.wl("FinViz Scan")["symbols"], ["NASDAQ:NVDA"])

    def test_big_list_is_chunked_and_capped(self):
        global KNOWN
        many = [f"T{i:03d}" for i in range(260)]
        saved = set(KNOWN)
        KNOWN |= {f"NYSE:{t}" for t in many}
        try:
            s = self.make(TV_MAX_SYMBOLS="230")
            self.connect(s)
            write_scan(self.reports, "2026-10-30_131000", many)
            s.sync()
            wl = self.wl("FinViz Scan")
            self.assertEqual(len(wl["symbols"]), 230)
            self.assertEqual(wl["symbols"][:2], ["NYSE:T000", "NYSE:T001"])
            self.assertIn("30 over the 230-symbol limit", wl["description"])
        finally:
            KNOWN = saved

    def test_sse_replies_and_lost_session(self):
        self.fake.sse = True
        self.connect()
        write_scan(self.reports, "2026-10-30_131000", ["AAPL"])
        self.sync.sync()
        self.fake.drop_sessions = True
        write_scan(self.reports, "2026-10-31_131000", ["NVDA"])
        self.sync.sync()
        self.assertEqual(self.wl("FinViz Scan")["symbols"], ["NASDAQ:NVDA"])
        self.assertGreaterEqual(len(self.fake.sessions), 1)

    def test_partial_lookup_does_not_remember_misses(self):
        self.connect()
        self.fake.partial = True
        write_scan(self.reports, "2026-10-30_131000", ["AAPL", "ZZZZ"])
        self.sync.sync()
        cache = json.loads((self.tmp / "tradingview" / "symbols.json").read_text())
        self.assertIn("AAPL", cache)
        self.assertNotIn("ZZZZ", cache)

    def test_errors_are_shown_and_other_lists_still_sync(self):
        self.connect()
        write_scan(self.reports, "2026-10-30_131000", ["AAPL"])
        write_result(self.results, "2026-10-30_131200_fmfi", "fmfi", "ok", ["GPN"])
        orig = self.fake.tool

        def broken(name, args):
            if name.endswith("add-to-watchlist") and args["symbols"] == ["NASDAQ:AAPL"]:
                raise ValueError("watchlists api: 500 boom")
            return orig(name, args)
        self.fake.tool = broken
        with self.assertRaisesRegex(tvs.TVError, "boom"):
            self.sync.sync()
        self.assertEqual(self.wl("FinViz BUY · Fmfi")["symbols"], ["NYSE:GPN"])
        self.assertIn("boom", self.sync.page_body())
        self.fake.tool = orig
        self.sync.sync()  # the failed list is retried (its source wasn't recorded)
        self.assertEqual(self.wl("FinViz Scan")["symbols"], ["NASDAQ:AAPL"])
        self.assertNotIn("Last problem", self.sync.page_body())

    def test_not_connected_does_nothing(self):
        write_scan(self.reports, "2026-10-30_131000", ["AAPL"])
        self.assertEqual(self.sync.sync(), [])
        with self.assertRaises(tvs.TVError):
            self.sync.sync_now()
        self.assertIn("Connect TradingView", self.sync.page_body())

    def test_loop_syncs_new_work_and_stops(self):
        self.connect()
        write_scan(self.reports, "2026-10-30_131000", ["AAPL"])
        stop = threading.Event()
        old = tvs.CHECK_EVERY
        tvs.CHECK_EVERY = 0.05
        try:
            t = threading.Thread(target=self.sync.loop, args=(stop,), daemon=True)
            t.start()
            for _ in range(100):
                if any(w["name"] == "FinViz Scan" and w["symbols"] for w in self.fake.lists.values()):
                    break
                stop.wait(0.05)
            stop.set()
            t.join(5)
        finally:
            tvs.CHECK_EVERY = old
        self.assertEqual(self.wl("FinViz Scan")["symbols"], ["NASDAQ:AAPL"])
        self.assertFalse(t.is_alive())


class Dated(Base):
    """Default mode: a new list per run, old ones deleted after TV_KEEP_DAYS."""
    MODE = "dated"

    def names(self):
        return sorted(w["name"] for w in self.fake.lists.values())

    def test_new_list_per_run_named_by_date(self):
        self.connect()
        write_scan(self.reports, "2026-10-30_131000", ["AAPL", "BRK-B"])
        write_result(self.results, "2026-10-30_131200_fmfi", "fmfi", "ok", ["GPN"])
        self.sync.sync()
        write_scan(self.reports, "2026-10-31_120000", ["NVDA"])
        write_result(self.results, "2026-10-31_120500_fmfi", "fmfi", "ok", [])
        self.sync.sync()
        self.assertEqual(self.names(), ["FinViz BUY · Fmfi · Oct 30 13:12", "FinViz BUY · Fmfi · Oct 31 12:05",
                                        "FinViz Scan · Oct 30 13:10", "FinViz Scan · Oct 31 12:00"])
        self.assertEqual(self.wl("FinViz Scan · Oct 30 13:10")["symbols"], ["NASDAQ:AAPL", "NYSE:BRK.B"])
        self.assertEqual(self.wl("FinViz Scan · Oct 31 12:00")["symbols"], ["NASDAQ:NVDA"])
        self.assertEqual(self.wl("FinViz BUY · Fmfi · Oct 31 12:05")["symbols"], [])
        self.assertEqual(self.sync.pending(), [])
        page = self.sync.page_body()
        self.assertIn("FinViz Scan · Oct 31 12:00", page)
        self.assertIn("own new list", page)

    def test_same_run_never_makes_a_second_list(self):
        self.connect()
        write_scan(self.reports, "2026-10-30_131000", ["AAPL"])
        self.sync.sync()
        self.sync.sync(force=True)  # Sync now on the same run: same list, updated in place
        self.assertEqual(self.names(), ["FinViz Scan · Oct 30 13:10"])
        st = self.sync.state()
        st["history"] = []  # e.g. the record was lost after a failure: found again by its name
        (self.tmp / "tradingview" / "state.json").write_text(json.dumps(st))
        self.sync.sync()
        self.assertEqual(self.names(), ["FinViz Scan · Oct 30 13:10"])

    def test_old_lists_deleted_but_renamed_and_other_lists_kept(self):
        now = [1_000_000_000.0]
        s = tvs.TVSync(self.tmp / "tradingview", self.reports, self.results, signal_fn=signal_fn,
                       read_meta=read_meta, env={**self.env, "TV_KEEP_DAYS": "7"}, log=self.logs.append,
                       clock=lambda: now[0])
        self.fake.lists[7] = {"id": 7, "name": "My own list", "symbols": ["NASDAQ:AAPL"], "type": "custom"}
        self.connect(s)
        write_scan(self.reports, "2026-10-01_120000", ["AAPL"])
        write_result(self.results, "2026-10-01_120500_fmfi", "fmfi", "ok", ["GPN"])
        now[0] = dt_ts("2026-10-01T20:00:00")
        s.sync()
        self.wl("FinViz BUY · fmfi · Oct 01 12:05")["name"] = "Keep: fmfi Oct 1"  # you renamed it
        write_scan(self.reports, "2026-10-07_120000", ["NVDA"])
        now[0] = dt_ts("2026-10-07T20:00:00")
        s.sync()  # 6 days later: nothing old enough yet
        self.assertIn("FinViz Scan · Oct 01 12:00", self.names())
        write_scan(self.reports, "2026-10-09_120000", ["GLL"])
        now[0] = dt_ts("2026-10-09T20:00:00")
        s.sync()  # the Oct 1 runs are now over 7 days old
        self.assertEqual(self.names(), ["FinViz Scan · Oct 07 12:00", "FinViz Scan · Oct 09 12:00",
                                        "Keep: fmfi Oct 1", "My own list"])
        self.assertFalse(any(h["source"].startswith("2026-10-01") for h in s.state()["history"]))
        self.assertEqual(s.pending(), [])  # deleting an old list never makes it be sent again

    def test_keep_zero_never_deletes(self):
        now = [dt_ts("2026-10-01T20:00:00")]
        s = tvs.TVSync(self.tmp / "tradingview", self.reports, self.results, signal_fn=signal_fn,
                       read_meta=read_meta, env={**self.env, "TV_KEEP_DAYS": "0"}, log=self.logs.append,
                       clock=lambda: now[0])
        self.connect(s)
        write_scan(self.reports, "2026-10-01_120000", ["AAPL"])
        s.sync()
        write_scan(self.reports, "2026-10-30_120000", ["NVDA"])
        now[0] = dt_ts("2026-10-30T20:00:00")
        s.sync()
        self.assertEqual(self.names(), ["FinViz Scan · Oct 01 12:00", "FinViz Scan · Oct 30 12:00"])
        self.assertIn("kept until you delete", s.page_body())

    def test_switch_from_single_lists_retires_them(self):
        old = self.make(TV_LIST_MODE="replace")
        self.connect(old)
        write_scan(self.reports, "2026-10-30_131000", ["AAPL"])
        write_result(self.results, "2026-10-30_131200_fmfi", "fmfi", "ok", ["GPN"])
        old.sync()
        self.wl("FinViz BUY · Fmfi")["name"] = "Renamed by me"
        self.assertEqual(self.names(), ["FinViz Scan", "Renamed by me"])
        self.sync.sync()  # same folder, now in dated mode (the new default)
        self.assertEqual(self.names(), ["FinViz BUY · Fmfi · Oct 30 13:12", "FinViz Scan · Oct 30 13:10",
                                        "Renamed by me"])
        self.assertTrue(all(r.get("dated") for r in self.sync.state()["lists"].values()))


def dt_ts(iso):
    import datetime as _dt
    return _dt.datetime.fromisoformat(iso).replace(tzinfo=_dt.timezone.utc).timestamp()


class Units(unittest.TestCase):
    def test_variants_and_names(self):
        self.assertEqual(tvs.variants("BRK-B"), ["BRK.B", "BRK-B"])
        self.assertEqual(tvs.variants("aapl"), ["AAPL"])
        self.assertEqual(tvs.norm("mcp-watchlist-add-to-watchlist"), "mcp_watchlist_add_to_watchlist")

    def test_tool_name_matching(self):
        m = tvs.MCP("http://x", None)
        m._tools = ["mcp-watchlist-add-to-watchlist", "add_to_watchlist_v2", "mcp-tv-get-symbol-data",
                    "mcp-tv-get-symbol-data-batch"]
        self.assertEqual(m.tool_name("add_to_watchlist"), "mcp-watchlist-add-to-watchlist")
        self.assertEqual(m.tool_name("get_symbol_data_batch"), "mcp-tv-get-symbol-data-batch")
        self.assertEqual(m.tool_name("get_symbol_data"), "mcp-tv-get-symbol-data")
        m._tools = ["create_watchlist"]
        self.assertEqual(m.tool_name("create_watchlist"), "create_watchlist")
        with self.assertRaises(tvs.TVError):
            m.tool_name("delete_everything")

    def test_parse_rpc(self):
        sse = b'event: message\ndata: {"jsonrpc":"2.0","method":"x"}\n\ndata: {"jsonrpc":"2.0","id":3,"result":{"a":1}}\n\n'
        self.assertEqual(tvs.parse_rpc(sse, "text/event-stream", 3)["result"], {"a": 1})
        self.assertEqual(tvs.parse_rpc(b'{"jsonrpc":"2.0","id":4,"result":{}}', "application/json", 4)["result"], {})
        with self.assertRaises(tvs.TVError):
            tvs.parse_rpc(b'{"jsonrpc":"2.0","id":9}', "application/json", 4)
        with self.assertRaises(tvs.TVError):
            tvs.parse_rpc(b"<html>", "text/html", 1)

    def test_off_switch(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            s = tvs.TVSync(tmp / "tv", tmp, tmp, signal_fn=signal_fn, read_meta=read_meta,
                           env={"TRADINGVIEW_SYNC": "off"}, log=lambda m: None)
            stop = threading.Event()
            s.loop(stop)  # returns at once
            self.assertIn("turned off", s.page_body())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ServerIntegration(unittest.TestCase):
    """Runs only in the agent folder (where server.py lives): the page, routes and wiring."""

    def setUp(self):
        if os.environ.get("TV_SKIP_SERVER_TEST"):
            self.skipTest("server check switched off (installer self-test)")
        try:
            import server  # noqa: F401
        except Exception as err:  # not in the agent folder (e.g. while building the update)
            self.skipTest(f"server.py not importable here: {err}")

    def test_page_routes_and_wiring(self):
        import base64 as b64
        import urllib.request
        import server
        tmp = tempfile.mkdtemp()
        try:
            env = {"DATA_DIR": tmp, "DASHBOARD_PASSWORD": "pw", "DASHBOARD_USER": "admin", "PORT": "0",
                   "SCHEDULE_TIMES": "off", "FINVIZ_API_TOKEN": "", "RAILWAY_PUBLIC_DOMAIN": "agent.example.app"}
            app = server.build_app(env, log=lambda m: None)
            self.assertIsNotNone(app.tradingview)
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            httpd.app = app
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            hdr = {"Authorization": "Basic " + b64.b64encode(b"admin:pw").decode(), "Accept": "*/*"}

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            opener = urllib.request.build_opener(NoRedirect)

            def go(path, data=None):
                req = urllib.request.Request(base + path, data=data, headers=hdr, method="POST" if data is not None else "GET")
                try:
                    with opener.open(req, timeout=20) as r:
                        return r.status, r.headers, r.read().decode()
                except urllib.error.HTTPError as err:
                    return err.code, err.headers, err.read().decode()
            otp = getattr(server, "otp", None)
            if otp is not None and otp.enabled():
                self.skipTest("email sign-in is switched on in this shell; page check skipped")
            code, _, page = go("/tradingview")
            self.assertEqual(code, 200, page[:300])
            self.assertIn("Connect TradingView", page)
            code, h, _ = go("/tradingview/sync", b"")
            self.assertEqual((code, h.get("Location")), (303, "/tradingview"))
            code, _, page = go("/tradingview")
            self.assertIn("Connect TradingView first", page)
            if hasattr(server, "TABS"):
                code, _, page = go("/strategies")
                self.assertIn("/tradingview", page)
            httpd.shutdown()
            httpd.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
