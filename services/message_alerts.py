"""Push alerts for directed messages between Jupiter users (marker 186).

A directed message is stored in the recipient's Jupiter inbox (services/initiative_engine). This module adds the
OPTIONAL device alert, reusing the free FCM sender and the device registry that Jupiter calls already use:

- Content-free: the push carries only an opaque message id, the recipient account hash and the sender's display name;
  never the message text (nothing sensitive reaches the lock screen or Google's transport).
- Only to the recipient's own registered Android devices with a push token, only when FCM is configured, and only
  when the recipient has not switched message alerts off.
- Deduplicated by message id, so a retried request never alerts twice.
- The result is exactly what FCM said: "sent" means FCM ACCEPTED the push for at least one device — not that the phone
  received it, showed it, or that the message was read. Callers word it that way.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ALERT_TTL_SECONDS = 24 * 3600
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MessageAlertLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS message_alerts (
                message_id TEXT PRIMARY KEY, recipient TEXT NOT NULL, state TEXT NOT NULL, devices INTEGER NOT NULL,
                created_at TEXT NOT NULL)""")

    def claim(self, message_id: str, recipient: str) -> bool:
        """First caller for this message id wins; every retry after that is a duplicate."""
        with _lock, closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            try:
                connection.execute("INSERT INTO message_alerts (message_id, recipient, state, devices, created_at) "
                                   "VALUES (?, ?, 'claimed', 0, ?)", (message_id, recipient, _now()))
                return True
            except sqlite3.IntegrityError:
                return False

    def record(self, message_id: str, state: str, devices: int) -> None:
        with _lock, closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            connection.execute("UPDATE message_alerts SET state=?, devices=? WHERE message_id=?",
                               (state[:16], int(devices), message_id))

    def state(self, message_id: str) -> str:
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            row = connection.execute("SELECT state FROM message_alerts WHERE message_id=?", (message_id,)).fetchone()
        return str(row[0]) if row else ""


def alert_payload(message_id: str, recipient: str, sender_name: str) -> dict[str, str]:
    from services.jupiter_calls import account_hash
    return {"type": "jupiter_message", "message_id": message_id, "acct": account_hash(recipient),
            "sender_name": str(sender_name or "")[:60]}   # "from" is reserved by FCM (HTTP 400 INVALID_ARGUMENT)


def send_message_alert(*, message_id: str, recipient: str, sender_name: str, alerts_enabled: bool,
                       log: MessageAlertLog, call_store, sender) -> dict:
    """One alert attempt. Returns {"state": ..., "devices": n}; state is one of
    sent | disabled | push_unavailable | no_device | failed | duplicate."""
    if not log.claim(message_id, recipient):
        return {"state": "duplicate", "devices": 0}
    if not alerts_enabled:
        log.record(message_id, "disabled", 0)
        return {"state": "disabled", "devices": 0}
    if sender is None:
        log.record(message_id, "push_unavailable", 0)
        return {"state": "push_unavailable", "devices": 0}
    with closing(call_store._connect()) as connection:
        targets = connection.execute(
            "SELECT device_id, push_token FROM call_devices WHERE owner=? AND platform='android' AND push_token<>''",
            (str(recipient).strip().casefold(),)).fetchall()
    if not targets:
        log.record(message_id, "no_device", 0)
        return {"state": "no_device", "devices": 0}
    payload = alert_payload(message_id, recipient, sender_name)
    sent = 0
    for device_id, token in targets:
        outcome = sender.send(token, payload, ttl_seconds=ALERT_TTL_SECONDS)
        if outcome == "unregistered":
            call_store.drop_push_token(device_id)
        sent += outcome == "sent"
    state = "sent" if sent else "failed"
    log.record(message_id, state, sent)
    return {"state": state, "devices": sent}
