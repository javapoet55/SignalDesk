"""Session-bound, single-use pending changes; request bodies never go into browser fields."""
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

import login_otp as otp


@dataclass
class Change:
    id: str
    session: str
    path: str
    body: bytes
    content_type: str
    label: str
    expires: float
    challenge: str = ''


class PendingChanges:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}

    def _remove(self, id):
        change = self.items.pop(id, None)
        if change:
            otp.cancel(change.challenge)

    def _cleanup(self):
        for id in list(self.items):
            if self.items[id].expires <= time.time():
                self._remove(id)

    def reserve(self, session, path, body, content_type, label):
        with self.lock:
            self._cleanup()
            for id in list(self.items):
                if hmac.compare_digest(self.items[id].session, session):
                    self._remove(id)
            if len(self.items) >= 8 or sum(len(c.body) for c in self.items.values()) + len(body) > 24_000_000:
                raise ValueError('Too many pending changes. Try again later.')
            id = secrets.token_urlsafe(24)
            change = Change(id, session, path, body, content_type, label, time.time() + otp.TTL)
            self.items[id] = change
            return change

    def get(self, id, session):
        with self.lock:
            self._cleanup()
            change = self.items.get(id)
            return change if change and hmac.compare_digest(change.session, session) else None

    def attach(self, id, session, challenge):
        with self.lock:
            change = self.items.get(id)
            if not change or not hmac.compare_digest(change.session, session):
                otp.cancel(challenge)
                return False
            change.challenge = challenge
            change.expires = time.time() + otp.TTL
            return True

    def take(self, id, session):
        with self.lock:
            self._cleanup()
            change = self.items.get(id)
            if not change or not hmac.compare_digest(change.session, session):
                return None
            del self.items[id]
            otp.cancel(change.challenge)
            return change

    def cancel_session(self, session):
        with self.lock:
            for id in list(self.items):
                if hmac.compare_digest(self.items[id].session, session):
                    self._remove(id)
