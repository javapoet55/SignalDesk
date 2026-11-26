"""Deployment defaults with optional per-principal overrides. No billing dependencies."""

import json
import os
import threading
import time
from collections import deque

from .market import Problem

DEFAULTS = {
    "portfolios": 25,
    "history_days": 3650,
    "research_days": 3650,
    "advanced": True,
    "ai": False,
    "export": True,
    "compute_credits": 0,
}


def entitlements(owner):
    overrides = json.loads(os.getenv("PAPER_ENTITLEMENTS", "{}"))
    result = {
        **DEFAULTS,
        "portfolios": int(os.getenv("PAPER_PORTFOLIO_LIMIT", "25")),
        "export": os.getenv("PAPER_EXPORT", "1") == "1",
        **overrides.get("*", {}),
        **overrides.get(owner, {}),
    }
    for k, v in DEFAULTS.items():
        if type(result[k]) is not type(v) or (type(v) is int and result[k] < 0):
            raise Problem("Invalid paper entitlement configuration.", 503)
    return result


def require(owner, key):
    if not entitlements(owner)[key]:
        raise Problem("This research feature is disabled for your account.", 403)


class RateLimit:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = {}

    def check(self, owner):
        with self.lock:
            now = time.monotonic()
            times = self.events.setdefault(owner, deque())
            while times and times[0] <= now - 60:
                times.popleft()
            if len(times) >= 180:
                raise Problem("Paper request limit reached. Retry in a minute.", 429)
            times.append(now)
