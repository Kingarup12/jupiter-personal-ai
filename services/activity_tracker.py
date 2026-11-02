"""One activity counter for genuine Jupiter work (marker 174).

The Server Load level is the number of DISTINCT accounts with work in progress right now:

    0 -> Low   1 -> Medium   2 -> High   3 or more -> Extreme

Work = an AI request while it is running (chat and streamed replies, Live sessions, dictation/transcription, speech
generation, laptop-control planning, research), plus background tasks while they are running. Several requests or
jobs from one account count once. It is NOT signed-in users, request totals or CPU.

Requests hold a lease for their whole lifetime: the ASGI middleware below wraps streamed responses and WebSocket
sessions, and releases in ``finally``, so errors, cancellations and client disconnects always clean up. A stale-lease
sweep is a second safety net (a lease can never outlive ``max_age``). Background tasks count while their tracker is
RUNNING or REVIEWING and was updated recently; they stop counting the moment they finish, fail or are cancelled.
Nothing here exposes who is working: snapshots carry counts only.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Callable, Iterable

LEVELS = ("Low", "Medium", "High", "Extreme")
HTTP_MAX_AGE = 30 * 60          # an HTTP request never legitimately holds work longer than this
SESSION_MAX_AGE = 6 * 3600      # a Live / dictation WebSocket session
TASK_CACHE_SECONDS = 5.0


def activity_level(active_users: int) -> str:
    return LEVELS[max(0, min(3, int(active_users)))]


class ActivityTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: dict[int, tuple[str, str, float, float]] = {}   # id -> (user, kind, started, max_age)
        self._ids = itertools.count(1)
        self._task_source: Callable[[], Iterable[str]] | None = None
        self._task_cache: tuple[float, frozenset[str]] = (-1e9, frozenset())

    @staticmethod
    def _user(value: str) -> str:
        return str(value or "").strip().casefold()

    def begin(self, user: str, kind: str, *, max_age: float = HTTP_MAX_AGE) -> int | None:
        user = self._user(user)
        if not user:
            return None
        with self._lock:
            lease = next(self._ids)
            self._leases[lease] = (user, str(kind or "work")[:24], self._clock(), float(max_age))
        return lease

    def end(self, lease: int | None) -> None:
        if lease is None:
            return
        with self._lock:
            self._leases.pop(lease, None)

    def set_task_source(self, source: Callable[[], Iterable[str]] | None) -> None:
        self._task_source = source
        self._task_cache = (-1e9, frozenset())

    def _task_users(self) -> frozenset[str]:
        if self._task_source is None:
            return frozenset()
        now = self._clock()
        cached_at, users = self._task_cache
        if now - cached_at < TASK_CACHE_SECONDS:
            return users
        try:
            users = frozenset(self._user(owner) for owner in self._task_source() if self._user(owner))
        except Exception:
            users = frozenset()                       # an unreadable task store never inflates (or breaks) the level
        self._task_cache = (now, users)
        return users

    def active_users(self) -> set[str]:
        now = self._clock()
        with self._lock:
            for lease, (_, _, started, max_age) in list(self._leases.items()):
                if now - started > max_age:
                    self._leases.pop(lease, None)    # leaked lease: never counts forever
            users = {user for user, _, _, _ in self._leases.values()}
        return users | set(self._task_users())

    def snapshot(self) -> dict:
        count = len(self.active_users())
        return {"active_work_users": count, "activity_level": activity_level(count),
                "activity_scope": "all_work", "activity_measured_at": time.time()}

    def lease_count(self) -> int:
        with self._lock:
            return len(self._leases)


tracker = ActivityTracker()

# Paths whose requests are genuine AI work (prefix match on the path after the /api mount).
HTTP_WORK = {
    "/api/chat": "chat", "/api/chat/stream": "chat", "/api/voice/transcribe": "transcribe",
    "/api/voice-cleanup": "transcribe", "/api/tts": "speech", "/api/tts/custom": "speech", "/api/tts/download": "speech",
    "/api/computer-control/plan": "control", "/api/owner/api-health/research": "research",
}
WS_WORK = {"/api/live/native": "live", "/api/voice/stream": "dictation"}


class ActivityMiddleware:
    """Pure ASGI middleware: the lease spans the WHOLE request, including a streamed body or a WebSocket session."""

    def __init__(self, app, resolve_user: Callable[[dict], str | None] | None = None,
                 activity: ActivityTracker | None = None):
        self.app = app
        self.resolve_user = resolve_user
        self.activity = activity or tracker

    async def __call__(self, scope, receive, send):
        kind = None
        if scope.get("type") == "http" and scope.get("method") == "POST":
            kind = HTTP_WORK.get(scope.get("path", "").rstrip("/"))
        elif scope.get("type") == "websocket":
            kind = WS_WORK.get(scope.get("path", "").rstrip("/"))
        if kind is None or self.resolve_user is None:
            await self.app(scope, receive, send)
            return
        try:
            user = self.resolve_user(scope)
        except Exception:
            user = None
        lease = self.activity.begin(user, kind, max_age=SESSION_MAX_AGE if scope["type"] == "websocket" else HTTP_MAX_AGE)
        try:
            await self.app(scope, receive, send)
        finally:
            self.activity.end(lease)
