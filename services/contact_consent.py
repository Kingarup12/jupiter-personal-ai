"""Mutual-consent sharing of a user's own contact details with one named Jupiter user (marker 185).

A requester asks for another account's email and/or verified mobile number. Nothing is revealed until the account that
OWNS the data approves the exact request in its own authenticated Jupiter app. Approval is one-time and field-specific:
only the approved fields, only to that requester, only once, only within a short window.

Privacy rules this store enforces:
- It never stores contact VALUES. On reveal the router reads the owner's current login email / verified phone from the
  authoritative account record at that moment, returns it once, and nothing is written to chat, memory, prompts or logs.
- Every state change is an atomic, owner- or requester-scoped UPDATE guarded by the current status and expiry, so an
  account can't act for another, an expired or already-used approval can't be replayed, and the recipient and fields
  are fixed at creation (there is no update path for them).
- Rows hold only routing metadata (who, which fields, status, timestamps).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

FIELDS = ("email", "phone")
REQUEST_TTL = timedelta(hours=48)       # an unanswered request expires
REVEAL_TTL = timedelta(hours=24)        # an approval must be used (viewed once) within this window
OPEN_STATES = ("pending", "approved")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _ref(account: str) -> str:
    """Opaque per-account reference for UI actions (unblock) that never exposes the other account's email."""
    import hashlib
    return hashlib.sha256(f"jupiter-contact-ref:{account}".encode("utf-8")).hexdigest()[:20]


def normalize_fields(fields) -> tuple[str, ...]:
    values = {str(item or "").strip().casefold() for item in (fields or [])}
    values = {"phone" if item in {"phone", "mobile", "number", "verified_phone"} else item for item in values}
    chosen = tuple(field for field in FIELDS if field in values)
    if not chosen:
        raise ValueError("Choose email, phone or both.")
    return chosen


class ContactConsentStore:
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self._locks.setdefault(str(self.path.resolve()), threading.Lock())
        with closing(self._connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS contact_requests (
                request_id TEXT PRIMARY KEY,
                requester TEXT NOT NULL,
                owner TEXT NOT NULL,
                fields TEXT NOT NULL,
                requester_name TEXT NOT NULL,
                owner_name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                decided_at TEXT,
                reveal_expires_at TEXT,
                revealed_at TEXT)""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_contact_owner ON contact_requests(owner, status)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_contact_requester ON contact_requests(requester, status)")
            # Marker 186: mutually accepted trusted connections, the owner's own sharing switches, blocks, and the
            # recipient's message-alert switch. Connection = one invite accepted by the OTHER account; never inferred.
            connection.execute("""CREATE TABLE IF NOT EXISTS trusted_connections (
                connection_id TEXT PRIMARY KEY, inviter TEXT NOT NULL, invitee TEXT NOT NULL,
                inviter_name TEXT NOT NULL, invitee_name TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, decided_at TEXT)""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trusted_pair ON trusted_connections(inviter, invitee, status)")
            connection.execute("""CREATE TABLE IF NOT EXISTS sharing_preferences (
                owner TEXT PRIMARY KEY, share_email INTEGER NOT NULL DEFAULT 0, share_phone INTEGER NOT NULL DEFAULT 0,
                message_alerts INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS blocked_accounts (
                owner TEXT NOT NULL, blocked TEXT NOT NULL, blocked_name TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (owner, blocked))""")
            existing = {row[1] for row in connection.execute("PRAGMA table_info(contact_requests)")}
            if "decided_by" not in existing:
                connection.execute("ALTER TABLE contact_requests ADD COLUMN decided_by TEXT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    # ── housekeeping ──────────────────────────────────────────────────────────────────────────────────────────
    def _expire(self, connection: sqlite3.Connection, now: datetime) -> None:
        stamp = _iso(now)
        connection.execute("UPDATE contact_requests SET status='expired' WHERE status='pending' AND expires_at<=?",
                           (stamp,))
        connection.execute("UPDATE contact_requests SET status='expired' WHERE status='approved' AND reveal_expires_at<=?",
                           (stamp,))

    @staticmethod
    def _public(row: sqlite3.Row, viewer: str) -> dict:
        data = {key: row[key] for key in ("request_id", "status", "created_at", "expires_at", "decided_at",
                                          "reveal_expires_at", "revealed_at")}
        data["fields"] = row["fields"].split(",")
        if row["owner"] == viewer:
            data.update(direction="incoming", requester_name=row["requester_name"])
        else:
            data.update(direction="outgoing", owner_name=row["owner_name"])
        return data                       # never the other party's email, and never any contact value

    # ── requester ─────────────────────────────────────────────────────────────────────────────────────────────
    def create(self, requester: str, owner: str, fields, *, requester_name: str, owner_name: str,
               now: datetime | None = None) -> dict:
        requester, owner = str(requester or "").strip().casefold(), str(owner or "").strip().casefold()
        if not requester or not owner:
            raise ValueError("Both accounts are required.")
        if requester == owner:
            raise ValueError("self")
        chosen = ",".join(normalize_fields(fields))
        now = now or _utcnow()
        with self._lock, closing(self._connect()) as connection, connection:
            self._expire(connection, now)
            existing = connection.execute(
                "SELECT * FROM contact_requests WHERE requester=? AND owner=? AND fields=? AND status IN ('pending','approved') "
                "ORDER BY created_at DESC LIMIT 1", (requester, owner, chosen)).fetchone()
            if existing is not None:
                return {**self._public(existing, requester), "duplicate": True}
            request_id = uuid4().hex
            connection.execute(
                "INSERT INTO contact_requests (request_id, requester, owner, fields, requester_name, owner_name, status, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (request_id, requester, owner, chosen, str(requester_name or "A Jupiter user")[:120],
                 str(owner_name or "this person")[:120], _iso(now), _iso(now + REQUEST_TTL)))
            row = connection.execute("SELECT * FROM contact_requests WHERE request_id=?", (request_id,)).fetchone()
        return {**self._public(row, requester), "duplicate": False}

    def cancel(self, requester: str, request_id: str, *, now: datetime | None = None) -> bool:
        with self._lock, closing(self._connect()) as connection, connection:
            self._expire(connection, now or _utcnow())
            return bool(connection.execute(
                "UPDATE contact_requests SET status='cancelled', decided_at=? WHERE request_id=? AND requester=? "
                "AND status='pending'", (_iso(now or _utcnow()), request_id, str(requester).casefold())).rowcount)

    def claim_reveal(self, requester: str, request_id: str, *, now: datetime | None = None) -> dict | None:
        """One-time: atomically moves an approved, unexpired request of THIS requester to 'revealed' and returns which
        owner and fields may be read now. Any other state (pending, declined, revoked, expired, already revealed, or a
        different requester) returns None."""
        now = now or _utcnow()
        requester = str(requester or "").casefold()
        with self._lock, closing(self._connect()) as connection, connection:
            self._expire(connection, now)
            changed = connection.execute(
                "UPDATE contact_requests SET status='revealed', revealed_at=? WHERE request_id=? AND requester=? "
                "AND status='approved' AND reveal_expires_at>?", (_iso(now), request_id, requester, _iso(now))).rowcount
            if not changed:
                return None
            row = connection.execute("SELECT owner, owner_name, fields FROM contact_requests WHERE request_id=?",
                                     (request_id,)).fetchone()
        return {"owner": row["owner"], "owner_name": row["owner_name"], "fields": row["fields"].split(",")}

    # ── data owner ────────────────────────────────────────────────────────────────────────────────────────────
    def decide(self, owner: str, request_id: str, approve: bool, *, now: datetime | None = None) -> dict | None:
        now = now or _utcnow()
        owner = str(owner or "").casefold()
        with self._lock, closing(self._connect()) as connection, connection:
            self._expire(connection, now)
            if approve:
                changed = connection.execute(
                    "UPDATE contact_requests SET status='approved', decided_at=?, reveal_expires_at=? WHERE request_id=? "
                    "AND owner=? AND status='pending' AND expires_at>?",
                    (_iso(now), _iso(now + REVEAL_TTL), request_id, owner, _iso(now))).rowcount
            else:
                changed = connection.execute(
                    "UPDATE contact_requests SET status='declined', decided_at=? WHERE request_id=? AND owner=? "
                    "AND status='pending'", (_iso(now), request_id, owner)).rowcount
            if not changed:
                return None
            row = connection.execute("SELECT * FROM contact_requests WHERE request_id=?", (request_id,)).fetchone()
        return self._public(row, owner)

    def revoke(self, owner: str, request_id: str, *, now: datetime | None = None) -> bool:
        """An approval not yet viewed can be withdrawn. Once viewed, the value has been seen and cannot be recalled."""
        with self._lock, closing(self._connect()) as connection, connection:
            self._expire(connection, now or _utcnow())
            return bool(connection.execute(
                "UPDATE contact_requests SET status='revoked', decided_at=? WHERE request_id=? AND owner=? "
                "AND status='approved'", (_iso(now or _utcnow()), request_id, str(owner).casefold())).rowcount)

    # ── views ─────────────────────────────────────────────────────────────────────────────────────────────────
    def list_for(self, user: str, *, now: datetime | None = None, limit: int = 50) -> dict:
        user = str(user or "").casefold()
        with self._lock, closing(self._connect()) as connection, connection:
            self._expire(connection, now or _utcnow())
            incoming = connection.execute(
                "SELECT * FROM contact_requests WHERE owner=? ORDER BY created_at DESC LIMIT ?", (user, limit)).fetchall()
            outgoing = connection.execute(
                "SELECT * FROM contact_requests WHERE requester=? ORDER BY created_at DESC LIMIT ?",
                (user, limit)).fetchall()
        incoming_items = [self._public(row, user) for row in incoming]
        return {"incoming": incoming_items, "outgoing": [self._public(row, user) for row in outgoing],
                "pending_incoming": sum(1 for item in incoming_items if item["status"] == "pending")}

    # ── marker 186: trusted connections (mutual), sharing switches, blocks, message alerts ─────────────────────────
    def is_blocked(self, owner: str, other: str) -> bool:
        """True when ``owner`` has blocked ``other`` (owner's choice; never shown to the blocked account)."""
        with closing(self._connect()) as connection:
            return connection.execute("SELECT 1 FROM blocked_accounts WHERE owner=? AND blocked=?",
                                      (str(owner).casefold(), str(other).casefold())).fetchone() is not None

    def block(self, owner: str, other: str, other_name: str) -> None:
        owner, other = str(owner).casefold(), str(other).casefold()
        if not owner or not other or owner == other:
            raise ValueError("Choose another account to block.")
        now = _iso(_utcnow())
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("INSERT OR IGNORE INTO blocked_accounts (owner, blocked, blocked_name, created_at) "
                               "VALUES (?, ?, ?, ?)", (owner, other, str(other_name or "A Jupiter user")[:120], now))
            # A block ends any trusted connection and every open request between the two accounts.
            connection.execute("UPDATE trusted_connections SET status='removed', decided_at=? WHERE status IN "
                               "('pending','accepted') AND ((inviter=? AND invitee=?) OR (inviter=? AND invitee=?))",
                               (now, owner, other, other, owner))
            connection.execute("UPDATE contact_requests SET status='declined', decided_at=?, decided_by='block' "
                               "WHERE owner=? AND requester=? AND status IN ('pending','approved')", (now, owner, other))

    def unblock(self, owner: str, other: str) -> bool:
        with self._lock, closing(self._connect()) as connection, connection:
            return bool(connection.execute("DELETE FROM blocked_accounts WHERE owner=? AND blocked=?",
                                           (str(owner).casefold(), str(other).casefold())).rowcount)

    def blocked_list(self, owner: str) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT blocked, blocked_name, created_at FROM blocked_accounts WHERE owner=? "
                                      "ORDER BY created_at DESC", (str(owner).casefold(),)).fetchall()
        return [{"ref": _ref(row["blocked"]), "name": row["blocked_name"], "created_at": row["created_at"]} for row in rows]

    def unblock_ref(self, owner: str, ref: str) -> bool:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT blocked FROM blocked_accounts WHERE owner=?",
                                      (str(owner).casefold(),)).fetchall()
        match = next((row["blocked"] for row in rows if _ref(row["blocked"]) == ref), None)
        return self.unblock(owner, match) if match else False

    def invite(self, inviter: str, invitee: str, *, inviter_name: str, invitee_name: str,
               now: datetime | None = None) -> dict:
        inviter, invitee = str(inviter).casefold(), str(invitee).casefold()
        if not inviter or not invitee or inviter == invitee:
            raise ValueError("self")
        if self.is_blocked(invitee, inviter):
            # Neither claim it was sent nor reveal the block: a neutral "unavailable".
            return {"status": "unavailable", "connection_id": "", "other_name": invitee_name}
        now = now or _utcnow()
        with self._lock, closing(self._connect()) as connection, connection:
            existing = connection.execute(
                "SELECT * FROM trusted_connections WHERE status IN ('pending','accepted') AND "
                "((inviter=? AND invitee=?) OR (inviter=? AND invitee=?)) ORDER BY created_at DESC LIMIT 1",
                (inviter, invitee, invitee, inviter)).fetchone()
            if existing is not None:
                if existing["status"] == "pending" and existing["invitee"] == inviter:
                    # The other side had already invited this account: inviting back is not acceptance. Each side
                    # accepts only through its own decision on an invite it received.
                    return {"status": "awaiting_you", "connection_id": existing["connection_id"],
                            "other_name": existing["inviter_name"]}
                return {"status": existing["status"], "connection_id": existing["connection_id"],
                        "other_name": invitee_name}
            connection_id = uuid4().hex
            connection.execute(
                "INSERT INTO trusted_connections (connection_id, inviter, invitee, inviter_name, invitee_name, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (connection_id, inviter, invitee, str(inviter_name)[:120], str(invitee_name)[:120], _iso(now)))
        return {"status": "invited", "connection_id": connection_id, "other_name": invitee_name}

    def decide_connection(self, invitee: str, connection_id: str, accept: bool) -> bool:
        """Only the INVITED account can accept or decline; acceptance is what makes the connection mutual."""
        with self._lock, closing(self._connect()) as connection, connection:
            return bool(connection.execute(
                "UPDATE trusted_connections SET status=?, decided_at=? WHERE connection_id=? AND invitee=? "
                "AND status='pending'", ("accepted" if accept else "declined", _iso(_utcnow()), connection_id,
                                          str(invitee).casefold())).rowcount)

    def remove_connection(self, user: str, connection_id: str) -> bool:
        """Either side may end a connection (or withdraw its own pending invite)."""
        user = str(user).casefold()
        with self._lock, closing(self._connect()) as connection, connection:
            return bool(connection.execute(
                "UPDATE trusted_connections SET status='removed', decided_at=? WHERE connection_id=? AND "
                "status IN ('pending','accepted') AND (inviter=? OR invitee=?)",
                (_iso(_utcnow()), connection_id, user, user)).rowcount)

    def trusted(self, a: str, b: str) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM trusted_connections WHERE status='accepted' AND ((inviter=? AND invitee=?) OR "
                "(inviter=? AND invitee=?))", (str(a).casefold(), str(b).casefold(), str(b).casefold(),
                                               str(a).casefold())).fetchone() is not None

    def connections(self, user: str) -> list[dict]:
        user = str(user).casefold()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM trusted_connections WHERE (inviter=? OR invitee=?) AND status IN ('pending','accepted') "
                "ORDER BY created_at DESC LIMIT 100", (user, user)).fetchall()
        items = []
        for row in rows:
            mine = row["inviter"] == user
            items.append({"connection_id": row["connection_id"], "status": row["status"],
                          "other_name": row["invitee_name"] if mine else row["inviter_name"],
                          "direction": "sent" if mine else "received", "created_at": row["created_at"]})
        return items

    def preferences(self, owner: str) -> dict:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM sharing_preferences WHERE owner=?",
                                     (str(owner).casefold(),)).fetchone()
        if row is None:
            return {"share_email": False, "share_phone": False, "message_alerts": True}
        return {"share_email": bool(row["share_email"]), "share_phone": bool(row["share_phone"]),
                "message_alerts": bool(row["message_alerts"])}

    def save_preferences(self, owner: str, **changes) -> dict:
        current = {**self.preferences(owner), **{k: bool(v) for k, v in changes.items()
                                                  if k in {"share_email", "share_phone", "message_alerts"}}}
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO sharing_preferences (owner, share_email, share_phone, message_alerts, updated_at) VALUES "
                "(?, ?, ?, ?, ?) ON CONFLICT(owner) DO UPDATE SET share_email=excluded.share_email, "
                "share_phone=excluded.share_phone, message_alerts=excluded.message_alerts, updated_at=excluded.updated_at",
                (str(owner).casefold(), int(current["share_email"]), int(current["share_phone"]),
                 int(current["message_alerts"]), _iso(_utcnow())))
        return current

    def policy_allows(self, owner: str, requester: str, fields) -> bool:
        """The owner's OWN standing choice covers this request: a mutually accepted connection AND every requested field
        switched on. Anything less is a normal per-request approval."""
        prefs = self.preferences(owner)
        chosen = normalize_fields(fields)
        return (self.trusted(owner, requester) and not self.is_blocked(owner, requester)
                and all(prefs.get(f"share_{field}") for field in chosen))

    def approve_by_policy(self, owner: str, request_id: str, *, now: datetime | None = None) -> dict | None:
        now = now or _utcnow()
        with self._lock, closing(self._connect()) as connection, connection:
            changed = connection.execute(
                "UPDATE contact_requests SET status='approved', decided_at=?, reveal_expires_at=?, decided_by='policy' "
                "WHERE request_id=? AND owner=? AND status='pending'",
                (_iso(now), _iso(now + REVEAL_TTL), request_id, str(owner).casefold())).rowcount
            row = connection.execute("SELECT * FROM contact_requests WHERE request_id=?", (request_id,)).fetchone()
        return self._public(row, str(row["requester"])) if changed and row else None

    def latest_between(self, requester: str, owner: str, *, now: datetime | None = None) -> dict | None:
        with self._lock, closing(self._connect()) as connection, connection:
            self._expire(connection, now or _utcnow())
            row = connection.execute(
                "SELECT * FROM contact_requests WHERE requester=? AND owner=? ORDER BY created_at DESC LIMIT 1",
                (str(requester).casefold(), str(owner).casefold())).fetchone()
        return self._public(row, str(requester).casefold()) if row else None
