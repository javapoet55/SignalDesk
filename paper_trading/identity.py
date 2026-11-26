"""Optional paper-only users for deployments with one shared host dashboard identity."""

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs

COOKIE = "paper_identity"
TTL = 12 * 3600
LOCK = threading.Lock()
SESSIONS: dict[str, tuple[str, float]] = {}
ATTEMPTS: deque[float] = deque()


def lookup(handler):
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except CookieError:
        return None
    token = cookie[COOKIE].value if COOKIE in cookie else ""
    with LOCK:
        principal, until = SESSIONS.get(token, (None, 0))
        if until <= time.time():
            SESSIONS.pop(token, None)
            return None
        return principal


def cookie_header(h, token, age):
    secure = (
        "; Secure"
        if h.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"
        else ""
    )
    return f"{COOKIE}={token}; Path=/paper; Max-Age={age}; HttpOnly; SameSite=Strict{secure}"


def handle(h, path):
    if path == "/paper/logout" and h.command == "POST":
        if not h._same_origin():
            return h._text(403, "Invalid origin.")
        c = SimpleCookie()
        try:
            c.load(h.headers.get("Cookie", ""))
        except CookieError:
            pass
        with LOCK:
            if COOKIE in c:
                SESSIONS.pop(c[COOKIE].value, None)
        return h._send(
            303,
            b"",
            "text/plain",
            {"Set-Cookie": cookie_header(h, "", 0), "Location": "/paper/login"},
        )
    config = os.getenv("PAPER_ACCOUNTS", "")
    if not config:
        return h._send(303, b"", "text/plain", {"Location": "/login"})
    message = ""
    status = 200
    if h.command == "POST":
        if not h._same_origin():
            return h._text(403, "Invalid origin.")
        try:
            size = int(h.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if not 0 < size <= 4096:
            return h._text(400, "Invalid request size.")
        form = parse_qs(h.rfile.read(size).decode(errors="replace"))
        name = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        with LOCK:
            now = time.monotonic()
            while ATTEMPTS and ATTEMPTS[0] < now - 60:
                ATTEMPTS.popleft()
            if len(ATTEMPTS) >= 12:
                return h._text(429, "Please retry in a minute.")
            ATTEMPTS.append(now)
        try:
            accounts = json.loads(config)
            valid = (
                re.fullmatch(r"[A-Za-z0-9_.@-]{1,80}", name)
                and name in accounts
                and f"paper/{name}" != h.app.settings.user
            )
            salt, expected = (
                accounts[name].split(":") if valid else ("00" * 16, "00" * 32)
            )
            computed = hashlib.scrypt(
                password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32
            )
            ok = hmac.compare_digest(computed, bytes.fromhex(expected)) and valid
        except (ValueError, TypeError, AttributeError):
            return h._text(503, "Paper account configuration is unavailable.")
        if ok:
            token = secrets.token_urlsafe(32)
            with LOCK:
                now = time.time()
                for old in [
                    key for key, (_, expires) in SESSIONS.items() if expires <= now
                ]:
                    del SESSIONS[old]
                SESSIONS[token] = (f"paper/{name}", now + TTL)
            return h._send(
                303,
                b"",
                "text/plain",
                {"Set-Cookie": cookie_header(h, token, TTL), "Location": "/paper"},
            )
        message = "Incorrect username or password."
        status = 401
    return h._html(
        status,
        "<!doctype html><html lang='en'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Paper account sign in</title><style>body{font:16px system-ui;background:#101a2a;color:#e8eff8}main{max-width:360px;margin:10vh auto;padding:28px;background:#1c2b41;border-radius:12px}label,input,button{display:block;margin:12px 0}input,button{padding:9px}a{color:#91baff}</style><main><small>FINVIZ AGENT</small><h1>Paper account sign in</h1><p>"
        + html.escape(message)
        + "</p><form method='post' action='/paper/login'><label>Username<input name='username' autocomplete='username' required></label><label>Password<input type='password' name='password' autocomplete='current-password' required></label><button>Sign in</button></form><a href='/login'>Dashboard sign in</a></main></html>",
    )


def make_hash():
    from getpass import getpass

    pw = getpass("New paper account password: ")
    if len(pw) < 12:
        raise SystemExit("Use at least 12 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    print(salt.hex() + ":" + digest.hex())


if __name__ == "__main__":
    make_hash()
