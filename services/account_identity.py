"""Verified login addresses, separate from immutable legacy storage principals.

The original registry key remains the account ID. Never rename user folders or
re-key credentials, permissions, memories, reports or sessions on email change.
Old addresses remain reserved, but cannot authenticate after replacement.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def normalize_address(value: str) -> str:
    value = str(value or "").strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value):
        raise ValueError("Enter a valid email address.")
    return value


class AccountIdentityStore:
    def __init__(self, directory):
        self.path = Path(directory) / "account_identity.sqlite3"

    def lookup(self, address):
        """Read only; discovering an unchanged account creates no database."""
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)) as db:
            row = db.execute(
                "SELECT a.account_id, i.login_email FROM addresses a JOIN identities i USING(account_id) WHERE a.address=?",
                (str(address).strip().lower(),),
            ).fetchone()
        return {"account_id": row[0], "login_email": row[1]} if row else None

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS identities(account_id TEXT PRIMARY KEY, login_email TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS addresses(address TEXT PRIMARY KEY, account_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS email_changes(
                account_id TEXT PRIMARY KEY, session_hash TEXT NOT NULL, flow TEXT NOT NULL,
                old_email TEXT NOT NULL, new_email TEXT, stage TEXT NOT NULL,
                code_hash TEXT NOT NULL, expires REAL NOT NULL, sent REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
        """)
        db.execute("BEGIN IMMEDIATE")
        return db

    @staticmethod
    def _ensure(db, account_id):
        reserved = db.execute("SELECT account_id FROM addresses WHERE address=?", (account_id,)).fetchone()
        if reserved and reserved[0] != account_id:
            raise ValueError("That email address is already associated with an account.")
        db.execute("INSERT OR IGNORE INTO identities VALUES(?,?)", (account_id, account_id))
        db.execute("INSERT OR IGNORE INTO addresses VALUES(?,?)", (account_id, account_id))

    def reserve_registration(self, account_id):
        db = self._connect()
        try:
            self._ensure(db, account_id)
            db.commit()
        finally:
            db.close()

    def change(self, account_id, token, action, *, flow="", otp="", new_email="", send, registered):
        """Session-bound, single-use two-mailbox proof; commit precedes success."""
        session_hash = hashlib.sha256(token.encode()).hexdigest()
        def deliver(address, code):
            try:
                send(address, code)
            except Exception:
                raise RuntimeError("Verification delivery failed.") from None
        now = time.time()
        db = self._connect()
        try:
            self._ensure(db, account_id)
            current = db.execute("SELECT login_email FROM identities WHERE account_id=?", (account_id,)).fetchone()[0]
            state = db.execute("SELECT * FROM email_changes WHERE account_id=?", (account_id,)).fetchone()
            if action == "start":
                if state and now - state["sent"] < 30:
                    raise ValueError("Wait 30 seconds before requesting another code.")
                flow = secrets.token_urlsafe(24)
                code = str(secrets.randbelow(900000) + 100000)
                digest = hashlib.sha256((flow + code).encode()).hexdigest()
                # Send before recording success; no secret values are logged.
                deliver(current, code)
                db.execute("INSERT OR REPLACE INTO email_changes VALUES(?,?,?,?,NULL,'old',?,?,?,0)",
                           (account_id, session_hash, flow, current, digest, now + 600, now))
                result = {"status": "old_code_sent", "flow": flow, "login_email": current}
            else:
                if not state or state["session_hash"] != session_hash or state["flow"] != flow or state["expires"] < now or state["old_email"] != current:
                    raise ValueError("Email change expired. Start again from this signed-in account.")
                if action == "send_new":
                    if state["stage"] != "verified_old":
                        raise ValueError("Verify your current email first.")
                    target = normalize_address(new_email)
                    if target == current or registered(target) or db.execute("SELECT 1 FROM addresses WHERE address=?", (target,)).fetchone():
                        raise ValueError("That email address is already associated with an account.")
                    code = str(secrets.randbelow(900000) + 100000)
                    deliver(target, code)
                    db.execute("UPDATE email_changes SET new_email=?, stage='new', code_hash=?, attempts=0, sent=? WHERE account_id=?",
                               (target, hashlib.sha256((flow + code).encode()).hexdigest(), now, account_id))
                    result = {"status": "new_code_sent", "flow": flow, "login_email": target}
                elif action in {"verify_old", "confirm"}:
                    expected = "old" if action == "verify_old" else "new"
                    if state["stage"] != expected:
                        raise ValueError("This verification step is no longer active.")
                    valid = secrets.compare_digest(state["code_hash"], hashlib.sha256((flow + str(otp).strip()).encode()).hexdigest())
                    if state["attempts"] >= 3 or not valid:
                        db.execute("UPDATE email_changes SET attempts=attempts+1 WHERE account_id=?", (account_id,))
                        db.commit()
                        raise ValueError("Invalid or exhausted verification code. Start again if necessary.")
                    if action == "verify_old":
                        db.execute("UPDATE email_changes SET stage='verified_old', code_hash='' WHERE account_id=?", (account_id,))
                        result = {"status": "old_verified", "flow": flow}
                    else:
                        target = state["new_email"]
                        # Recheck under the write lock: another request may have claimed it.
                        if registered(target) or db.execute("SELECT 1 FROM addresses WHERE address=?", (target,)).fetchone():
                            raise ValueError("That email address is already associated with an account.")
                        db.execute("INSERT INTO addresses VALUES(?,?)", (target, account_id))
                        db.execute("UPDATE identities SET login_email=? WHERE account_id=?", (target, account_id))
                        db.execute("DELETE FROM email_changes WHERE account_id=?", (account_id,))
                        result = {"status": "success", "account_id": account_id, "email": account_id, "login_email": target}
                else:
                    raise ValueError("Invalid email-change action.")
            db.commit()
            return result
        finally:
            db.close()
