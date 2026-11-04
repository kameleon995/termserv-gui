"""Server-side web sessions.

The browser only holds a random session id in a cookie with no expiry date, so
the cookie is dropped when the browser closes. Everything else stays in this
process: logging out (or an expired session) really ends the session, and a
service restart logs everyone out.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

ANON_IDLE_SECONDS = 15 * 60  # a login page left open
MAX_SESSIONS = 2000


@dataclass
class Session:
    id: str
    data: dict = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    expired_notice: bool = False  # set when this replaced a session that timed out

    @property
    def user(self) -> str | None:
        return self.data.get("user")


class SessionStore:
    def __init__(self, idle_seconds: float = 3600, max_age_seconds: float = 8 * 3600, clock=time.monotonic):
        self.idle_seconds = idle_seconds
        self.max_age_seconds = max_age_seconds
        self.clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _expired(self, s: Session, now: float) -> bool:
        if s.user:
            return now - s.last_seen > self.idle_seconds or now - s.created > self.max_age_seconds
        return now - s.last_seen > ANON_IDLE_SECONDS

    def _purge(self, now: float) -> None:
        for sid in [sid for sid, s in self._sessions.items() if self._expired(s, now)]:
            del self._sessions[sid]
        if len(self._sessions) > MAX_SESSIONS:  # flood of anonymous visitors: drop the oldest
            anon = sorted((s for s in self._sessions.values() if not s.user), key=lambda s: s.last_seen)
            for s in anon[: len(self._sessions) - MAX_SESSIONS]:
                del self._sessions[s.id]

    def new(self) -> Session:
        now = self.clock()
        s = Session(id=secrets.token_urlsafe(32), created=now, last_seen=now)
        with self._lock:
            self._purge(now)
            self._sessions[s.id] = s
        return s

    def get(self, sid: str | None, touch: bool = True) -> tuple[Session | None, bool]:
        """Return (session, timed_out). touch=False for background polling, which must
        not keep an idle session alive."""
        if not sid:
            return None, False
        now = self.clock()
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None, False
            if self._expired(s, now):
                del self._sessions[sid]
                return None, bool(s.user)
            if touch:
                s.last_seen = now
            return s, False

    def login(self, old: Session, user: str) -> Session:
        """Start an authenticated session under a fresh id (no session fixation)."""
        self.delete(old)
        s = self.new()
        s.data["user"] = user
        return s

    def delete(self, s: Session) -> None:
        with self._lock:
            self._sessions.pop(s.id, None)

    def __len__(self) -> int:
        return len(self._sessions)


class LoginThrottle:
    """Lock out a client address after too many failed logins."""

    def __init__(self, max_failures: int = 5, window_seconds: float = 600, clock=time.monotonic):
        self.max_failures = max_failures
        self.window = window_seconds
        self.clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def locked_for(self, key: str) -> int:
        """Seconds until this address may try again (0 = not locked)."""
        now = self.clock()
        with self._lock:
            recent = self._recent(key, now)
            if len(recent) < self.max_failures:
                return 0
            return int(self.window - (now - recent[-self.max_failures])) + 1

    def failed(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(self.clock())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
