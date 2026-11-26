"""Authentication, account isolation and content checks using the real HTTP router."""

import hashlib
import http.client
import json
import os
import unittest
from unittest.mock import patch

from test_server import LiveServer


class PaperHTTP(unittest.TestCase):
    def setUp(self):
        self.host = LiveServer()
        self.addCleanup(self.host.close)
        salt = bytes(range(16))
        value = (
            salt.hex()
            + ":"
            + hashlib.scrypt(
                b"fictional password", salt=salt, n=16384, r=8, p=1, dklen=32
            ).hex()
        )
        config = patch.dict(
            os.environ, {"PAPER_ACCOUNTS": json.dumps({"alice": value, "bob": value})}
        )
        config.start()
        self.addCleanup(config.stop)

    def login(self, user):
        resp, _ = self.host.request(
            "POST",
            "/paper/login",
            auth=None,
            form={"username": user, "password": "fictional password"},
        )
        self.assertEqual(resp.status, 303)
        return resp.getheader("Set-Cookie").split(";")[0]

    def call(self, cookie, path, body=None, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.host.port)
        headers = {
            "Cookie": cookie,
            "Content-Type": "application/json",
            "X-Paper-Request": "1",
            **(extra or {}),
        }
        conn.request(
            "GET" if body is None else "POST",
            "/paper/api/" + path,
            json.dumps(body) if body is not None else None,
            headers,
        )
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_two_real_logins_and_csrf(self):
        a = self.login("alice")
        b = self.login("bob")
        status, data = self.call(
            a, "portfolios", {"name": "Private", "capital": "1234"}
        )
        self.assertEqual(status, 201)
        pid = json.loads(data)["id"]
        self.assertEqual(json.loads(self.call(b, "portfolios")[1]), [])
        for kind in (
            "summary",
            "signals",
            "journal",
            "orders",
            "positions",
            "audit",
            "export",
            "report",
            "analytics",
            "operations",
        ):
            with self.subTest(kind=kind):
                self.assertEqual(self.call(b, "portfolios/" + pid + "/" + kind)[0], 404)
        self.assertEqual(
            self.call(b, "portfolios/" + pid + "/settings", {"name": "other"})[0], 404
        )
        self.assertEqual(
            self.call(
                a,
                "portfolios",
                {"name": "cross", "capital": "1"},
                {"Origin": "https://example.invalid"},
            )[0],
            403,
        )
        self.assertEqual(
            self.call(a, "portfolios", {}, {"X-Paper-Request": "0"})[0], 403
        )
        self.assertEqual(
            self.host.request("GET", "/strategies", auth=None, headers={"Cookie": a})[
                0
            ].status,
            303,
        )
        self.assertEqual(self.call(a, "watchlist")[1], b"[]")
        self.assertEqual(
            self.host.request(
                "POST", "/paper/logout", auth=None, headers={"Cookie": a}
            )[0].status,
            303,
        )
        self.assertEqual(self.call(a, "portfolios")[0], 303)
