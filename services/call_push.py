"""Free background ringing for Jupiter calls via Firebase Cloud Messaging HTTP v1 (marker 172).

FCM is a no-cost Google service. It is used ONLY when its server credential has been configured through Jupiter's
normal secure credential loading (Jupiter Vault -> process environment): ``JUPITER_FCM_SERVICE_ACCOUNT_JSON`` (the
service-account JSON) or ``JUPITER_FCM_SERVICE_ACCOUNT_FILE`` (a path outside the repository). Without it, push is
reported as unavailable and only a running Jupiter app can ring. The credential is never logged or returned.

Messages are data-only and high priority with a TTL equal to the remaining ring time, so a phone that comes online
after the call expired never rings for it. Delivery is never guaranteed (force-stopped apps, offline phones, battery
restrictions and missing notification permission all prevent it); callers report that honestly.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def configured() -> bool:
    return bool(os.getenv("JUPITER_FCM_SERVICE_ACCOUNT_JSON") or os.getenv("JUPITER_FCM_SERVICE_ACCOUNT_FILE"))


class FcmPushSender:
    def __init__(self, service_account: dict[str, Any], *, http=None):
        from google.oauth2 import service_account as google_service_account
        self.project_id = str(service_account.get("project_id") or "")
        if not self.project_id:
            raise ValueError("The FCM service account has no project id.")
        self._credentials = google_service_account.Credentials.from_service_account_info(service_account, scopes=[FCM_SCOPE])
        self._lock = threading.Lock()
        self._http = http

    @classmethod
    def from_environment(cls) -> "FcmPushSender | None":
        raw = os.getenv("JUPITER_FCM_SERVICE_ACCOUNT_JSON") or ""
        path = os.getenv("JUPITER_FCM_SERVICE_ACCOUNT_FILE") or ""
        try:
            if not raw and path:
                with open(path, encoding="utf-8") as handle:
                    raw = handle.read()
            return cls(json.loads(raw)) if raw else None
        except Exception:
            return None                                   # misconfigured: push unavailable (never echo the value)

    def _token(self) -> str:
        with self._lock:
            if not self._credentials.valid:
                from google.auth.transport.requests import Request
                self._credentials.refresh(Request())
            return str(self._credentials.token)

    def send(self, token: str, data: dict[str, str], *, ttl_seconds: int) -> str:
        import httpx
        body = {"message": {"token": token, "data": {k: str(v) for k, v in data.items()},
                            "android": {"priority": "HIGH", "ttl": f"{max(1, int(ttl_seconds))}s"}}}
        try:
            client = self._http or httpx
            response = client.post(f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
                                   json=body, headers={"Authorization": f"Bearer {self._token()}"}, timeout=8)
        except Exception:
            return "failed"
        if response.status_code == 200:
            return "sent"
        if response.status_code in (400, 404):
            try:
                details = json.dumps(response.json())
            except ValueError:
                details = ""
            if "UNREGISTERED" in details or response.status_code == 404:
                return "unregistered"
        return "failed"


class CallDispatcher:
    """Background pass every few seconds: expire unanswered calls (-> missed), create due reminder calls and send
    ring / stop-ringing pushes. Bounded work per pass; a stop event ends it with the process."""

    def __init__(self, interval_seconds: float = 3.0):
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._sender: FcmPushSender | None = None
        self._sender_checked = 0.0

    def sender(self) -> FcmPushSender | None:
        if time.monotonic() - self._sender_checked > 300 or (self._sender is None and configured()
                                                             and time.monotonic() - self._sender_checked > 30):
            self._sender, self._sender_checked = FcmPushSender.from_environment(), time.monotonic()
        return self._sender

    def run_once(self) -> None:
        from services.jupiter_calls import JupiterCallStore, create_reminder_calls, dispatch_pushes
        store = JupiterCallStore.default()
        create_reminder_calls(store)
        dispatch_pushes(store, self.sender())

    def poke(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="jupiter-call-dispatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                pass                                       # a bad pass never kills calling; the next pass retries
            self._wake.wait(self.interval)
            self._wake.clear()


dispatcher = CallDispatcher()
