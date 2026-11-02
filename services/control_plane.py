"""User-scoped reports and owner-approved registration requests.

The store contains operational metadata only.  It never stores provider
credentials, OAuth material, unrelated chat history, or complete Gmail data.
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_REGISTRATION_STAGE_ORDER = {
    "COMPLETE": 0,
    "API_KEY_PENDING": 1,
    "OTP_PENDING": 2,
    "APPROVED": 3,
    "PENDING_APPROVAL": 4,
    "DECLINED": 5,
}


class ControlPlaneStore:
    REPORT_STATUSES = {"Submitted", "Under Review", "Resolved", "Dismissed"}

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir).resolve() / "jupiter_control_plane.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_reports (
                    id TEXT PRIMARY KEY,
                    user_email TEXT NOT NULL COLLATE NOCASE,
                    message_id TEXT NOT NULL,
                    reported_message TEXT,
                    chat_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    what_went_wrong TEXT NOT NULL,
                    suggested_improvement TEXT,
                    provider TEXT,
                    model_id TEXT,
                    route TEXT,
                    fallback_stage TEXT,
                    generation_mode TEXT,
                    app_version TEXT,
                    error_category TEXT,
                    source TEXT NOT NULL DEFAULT 'message_report',
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'Submitted',
                    dismissed_reason TEXT,
                    public_resolution TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_user_reports_owner_queue
                    ON user_reports(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_user_reports_user
                    ON user_reports(user_email, created_at DESC);
                CREATE TABLE IF NOT EXISTS report_comments (
                    id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL,
                    parent_comment_id TEXT,
                    author_email TEXT NOT NULL COLLATE NOCASE,
                    author_role TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(report_id) REFERENCES user_reports(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_report_comments_report
                    ON report_comments(report_id, created_at);
                CREATE TABLE IF NOT EXISTS registration_requests (
                    id TEXT PRIMARY KEY,
                    status_token_hash TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL COLLATE NOCASE,
                    username TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    decided_by TEXT,
                    decision_note TEXT,
                    approved_at TEXT,
                    onboarding_state TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
                    otp_verified_at TEXT,
                    registration_grant_hash TEXT,
                    registration_grant_expires_at TEXT,
                    owner_read_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_registration_active_email
                    ON registration_requests(email)
                    WHERE status IN ('Pending', 'Approved', 'Overdue');
                CREATE TABLE IF NOT EXISTS registration_recovery_codes (
                    id TEXT PRIMARY KEY,
                    registration_id TEXT NOT NULL,
                    email TEXT NOT NULL COLLATE NOCASE,
                    code_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    revoked_at TEXT,
                    issued_by TEXT,
                    FOREIGN KEY(registration_id) REFERENCES registration_requests(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_reg_recovery_email
                    ON registration_recovery_codes(email, expires_at);
                """
            )
            report_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(user_reports)").fetchall()
            }
            if "reported_message" not in report_columns:
                connection.execute("ALTER TABLE user_reports ADD COLUMN reported_message TEXT")
            if "source" not in report_columns:
                connection.execute("ALTER TABLE user_reports ADD COLUMN source TEXT NOT NULL DEFAULT 'message_report'")
            if "attachments_json" not in report_columns:
                connection.execute("ALTER TABLE user_reports ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
            comment_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(report_comments)").fetchall()
            }
            if "parent_comment_id" not in comment_columns:
                connection.execute("ALTER TABLE report_comments ADD COLUMN parent_comment_id TEXT")
            registration_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(registration_requests)").fetchall()
            }
            for name, declaration in (
                ("onboarding_state", "TEXT NOT NULL DEFAULT 'PENDING_APPROVAL'"),
                ("otp_verified_at", "TEXT"),
                ("registration_grant_hash", "TEXT"),
                ("registration_grant_expires_at", "TEXT"),
                ("owner_read_at", "TEXT"),
            ):
                if name not in registration_columns:
                    connection.execute(f"ALTER TABLE registration_requests ADD COLUMN {name} {declaration}")
            connection.execute(
                "UPDATE registration_requests SET onboarding_state="
                "CASE WHEN status='Approved' THEN 'APPROVED' ELSE 'PENDING_APPROVAL' END "
                "WHERE onboarding_state IS NULL OR onboarding_state=''"
            )
            connection.execute(
                "UPDATE registration_requests SET onboarding_state='APPROVED' "
                "WHERE status='Approved' AND onboarding_state='PENDING_APPROVAL'"
            )

    @staticmethod
    def _rows(rows) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    @staticmethod
    def _canonical_registration(connection, email: str):
        """Select the furthest valid active registration for an email.

        Older clients could leave a pending row beside a newer approved row;
        all resume paths must use the approved/furthest record instead of row
        insertion order.
        """
        normalized = str(email or "").strip().lower()
        return connection.execute(
            "SELECT * FROM registration_requests WHERE lower(trim(email))=? "
            "AND status IN ('Pending','Approved','Overdue') "
            "ORDER BY CASE status WHEN 'Approved' THEN 0 WHEN 'Pending' THEN 1 ELSE 2 END, "
            "CASE onboarding_state WHEN 'COMPLETE' THEN 0 WHEN 'API_KEY_PENDING' THEN 1 "
            "WHEN 'OTP_PENDING' THEN 2 WHEN 'APPROVED' THEN 3 ELSE 4 END, "
            "updated_at DESC, created_at DESC LIMIT 1",
            (normalized,),
        ).fetchone()

    def _shared_approved_registration(self, email: str):
        """Read an approved production record for an isolated WT runtime.

        WT owns its Pending/test records, but an approval is durable account
        authority rather than disposable client state.  When WT is pointed at
        its isolated data directory, consult the production control-plane DB
        read-only for Approved rows only.  Pending rows are never imported and
        no production backend or write path is reused.
        """
        if os.getenv("JUPITER_ENV", "").strip().lower() != "test":
            return None
        production_path = Path(__file__).resolve().parents[1] / "data" / "jupiter_control_plane.sqlite3"
        try:
            if self.path.resolve() == production_path.resolve() or not production_path.is_file():
                return None
        except (OSError, RuntimeError):
            return None
        normalized = str(email or "").strip().lower()
        connection = None
        try:
            connection = sqlite3.connect(production_path)
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM registration_requests WHERE lower(trim(email))=? "
                "AND status='Approved' "
                "ORDER BY CASE onboarding_state WHEN 'COMPLETE' THEN 0 WHEN 'API_KEY_PENDING' THEN 1 "
                "WHEN 'OTP_PENDING' THEN 2 WHEN 'APPROVED' THEN 3 ELSE 4 END, "
                "updated_at DESC, created_at DESC LIMIT 1",
                (normalized,),
            ).fetchone()
            return dict(row) if row else None
        except (OSError, sqlite3.Error):
            return None
        finally:
            if connection is not None:
                connection.close()

    def create_report(self, *, user_email: str, metadata: dict[str, Any]) -> dict[str, Any]:
        report_id = uuid.uuid4().hex
        now = _now()
        values = {
            "id": report_id,
            "user_email": user_email.strip().lower(),
            "message_id": str(metadata.get("message_id") or "")[:160],
            "reported_message": str(metadata.get("reported_message") or "").strip()[:6000],
            "chat_id": str(metadata.get("chat_id") or "")[:160],
            "created_at": now,
            "updated_at": now,
            "what_went_wrong": str(metadata.get("what_went_wrong") or "").strip()[:2000],
            "suggested_improvement": str(metadata.get("suggested_improvement") or "").strip()[:2000],
            "provider": str(metadata.get("provider") or "")[:80],
            "model_id": str(metadata.get("model_id") or "")[:240],
            "route": str(metadata.get("route") or "")[:160],
            "fallback_stage": str(metadata.get("fallback_stage") or "")[:80],
            "generation_mode": str(metadata.get("generation_mode") or "")[:80],
            "app_version": str(metadata.get("app_version") or "")[:80],
            "error_category": str(metadata.get("error_category") or "")[:80],
            "source": str(metadata.get("source") or "message_report")[:32],
            "attachments_json": json.dumps(metadata.get("attachments") or [], ensure_ascii=False)[:12000],
        }
        if values["source"] not in {"message_report", "general_report"}:
            raise ValueError("invalid_report_source")
        if values["source"] == "message_report" and not values["message_id"]:
            raise ValueError("message_id_required")
        if not values["what_went_wrong"] and not (metadata.get("attachments") or []):
            raise ValueError("problem_or_attachment_required")
        with self._connect() as connection:
            columns = ", ".join(values)
            connection.execute(
                f"INSERT INTO user_reports ({columns}) VALUES ({', '.join('?' for _ in values)})",
                tuple(values.values()),
            )
        return self.get_report(report_id) or {}

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM user_reports WHERE id=?", (report_id,)).fetchone()
            if not row:
                return None
            report = dict(row)
            try:
                report["attachments"] = json.loads(report.pop("attachments_json", "[]") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                report["attachments"] = []
            report["comments"] = self._rows(connection.execute(
                "SELECT * FROM report_comments WHERE report_id=? ORDER BY created_at", (report_id,)
            ).fetchall())
            return report

    def reports_for_user(self, email: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            reports = self._rows(connection.execute(
                "SELECT * FROM user_reports WHERE user_email=? ORDER BY created_at DESC", (email.strip().lower(),)
            ).fetchall())
            for report in reports:
                try:
                    report["attachments"] = json.loads(report.pop("attachments_json", "[]") or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    report["attachments"] = []
                # Users see the public resolution, never private staff comments.
                report.pop("dismissed_reason", None)
            return reports

    def staff_reports(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            reports = self._rows(connection.execute(
                "SELECT * FROM user_reports ORDER BY CASE status WHEN 'Submitted' THEN 0 WHEN 'Under Review' THEN 1 ELSE 2 END, created_at DESC"
            ).fetchall())
            for report in reports:
                try:
                    report["attachments"] = json.loads(report.pop("attachments_json", "[]") or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    report["attachments"] = []
                report["comments"] = self._rows(connection.execute(
                    "SELECT * FROM report_comments WHERE report_id=? ORDER BY created_at", (report["id"],)
                ).fetchall())
            return reports

    def update_report(self, report_id: str, *, role: str, actor: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_report(report_id)
        if not current:
            return None
        status = str(changes.get("status") or current["status"])
        if status not in self.REPORT_STATUSES:
            raise ValueError("invalid_report_status")
        if role == "admin" and status == "Resolved":
            raise PermissionError("owner_resolution_required")
        if role == "admin" and status not in {"Under Review", "Dismissed"}:
            raise PermissionError("owner_reopen_required")
        if role == "admin" and changes.get("public_resolution"):
            raise PermissionError("owner_resolution_required")
        if status == "Dismissed" and not str(changes.get("dismissed_reason") or "").strip():
            raise ValueError("dismissal_reason_required")
        public_resolution = str(changes.get("public_resolution") or current.get("public_resolution") or "")[:3000]
        dismissed_reason = str(changes.get("dismissed_reason") or current.get("dismissed_reason") or "")[:2000]
        with self._connect() as connection:
            connection.execute(
                "UPDATE user_reports SET status=?, public_resolution=?, dismissed_reason=?, updated_at=? WHERE id=?",
                (status, public_resolution, dismissed_reason, _now(), report_id),
            )
        return self.get_report(report_id)

    def add_comment(
        self,
        report_id: str,
        *,
        author: str,
        role: str,
        body: str,
        parent_comment_id: str = "",
    ) -> dict[str, Any]:
        if not self.get_report(report_id):
            raise LookupError("report_not_found")
        parent_comment_id = str(parent_comment_id or "").strip()
        if parent_comment_id:
            with self._connect() as connection:
                parent = connection.execute(
                    "SELECT 1 FROM report_comments WHERE id=? AND report_id=?",
                    (parent_comment_id, report_id),
                ).fetchone()
            if not parent:
                raise LookupError("parent_comment_not_found")
        comment = {
            "id": uuid.uuid4().hex,
            "report_id": report_id,
            "parent_comment_id": parent_comment_id or None,
            "author_email": author.strip().lower(),
            "author_role": role,
            "body": str(body or "").strip()[:2000],
            "created_at": _now(),
            "updated_at": _now(),
        }
        if not comment["body"]:
            raise ValueError("comment_required")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO report_comments
                (id,report_id,parent_comment_id,author_email,author_role,body,created_at,updated_at)
                VALUES (:id,:report_id,:parent_comment_id,:author_email,:author_role,:body,:created_at,:updated_at)""",
                comment,
            )
        return comment

    def edit_comment(self, comment_id: str, *, author: str, body: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM report_comments WHERE id=?", (comment_id,)).fetchone()
            if not row:
                return None
            if str(row["author_email"]).casefold() != author.strip().casefold():
                raise PermissionError("comment_owner_required")
            normalized = str(body or "").strip()[:2000]
            if not normalized:
                raise ValueError("comment_required")
            connection.execute(
                "UPDATE report_comments SET body=?, updated_at=? WHERE id=?", (normalized, _now(), comment_id)
            )
            updated = dict(row)
            updated.update(body=normalized, updated_at=_now())
            return updated

    @staticmethod
    def _token_hash(token: str) -> str:
        import hashlib
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def request_access(
        self,
        *,
        email: str,
        username: str,
        account_exists: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Return the one canonical registration record for an identity.

        A registration form is not an authorization event.  Always resolve an
        existing active record before attempting an INSERT so a repeated
        submit (including a different display name or device) can never create
        a second Pending request.  ``account_exists`` lets the API reconcile a
        durable account record that predates, or has lost, its control-plane
        row; an existing account is already at the COMPLETE stage.
        """
        normalized_email = email.strip().lower()
        normalized_username = " ".join(username.split())[:120]
        if not normalized_email or "@" not in normalized_email or len(normalized_username.replace(" ", "")) < 3:
            raise ValueError("valid_email_and_username_required")
        token = secrets.token_urlsafe(32)
        now = _now()
        request_id = uuid.uuid4().hex
        try:
            with self._connect() as connection:
                existing = self._canonical_registration(connection, normalized_email)
                shared_approved = self._shared_approved_registration(normalized_email)
                if shared_approved is not None:
                    shared_stage = str(shared_approved.get("onboarding_state") or "APPROVED").upper()
                    if shared_stage not in _REGISTRATION_STAGE_ORDER:
                        shared_stage = "APPROVED"
                    if existing is None:
                        # Mirror only the durable approved stage into the
                        # isolated WT control plane. The production row itself
                        # remains untouched and no Pending notification is made.
                        connection.execute(
                            "INSERT INTO registration_requests "
                            "(id,status_token_hash,email,username,status,created_at,updated_at,decided_by,decision_note,approved_at,onboarding_state) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (request_id, self._token_hash(token), normalized_email,
                             str(shared_approved.get("username") or normalized_username), "Approved", now, now,
                             str(shared_approved.get("decided_by") or "production_control_plane"),
                             str(shared_approved.get("decision_note") or "Approved in canonical control plane"),
                             shared_approved.get("approved_at") or now, shared_stage),
                        )
                        existing = connection.execute(
                            "SELECT * FROM registration_requests WHERE id=?", (request_id,)
                        ).fetchone()
                    else:
                        local_stage = str(existing["onboarding_state"] or "APPROVED").upper()
                        if local_stage not in _REGISTRATION_STAGE_ORDER:
                            local_stage = "APPROVED"
                        local_rank = _REGISTRATION_STAGE_ORDER[local_stage]
                        shared_rank = _REGISTRATION_STAGE_ORDER[shared_stage]
                        if str(existing["status"] or "") != "Approved" or shared_rank < local_rank:
                            connection.execute(
                                "UPDATE registration_requests SET status='Approved', onboarding_state=?, "
                                "approved_at=COALESCE(approved_at, ?), decided_by=COALESCE(decided_by, ?), "
                                "decision_note=COALESCE(decision_note, ?), updated_at=? WHERE id=?",
                                (shared_stage, shared_approved.get("approved_at") or now,
                                 str(shared_approved.get("decided_by") or "production_control_plane"),
                                 str(shared_approved.get("decision_note") or "Approved in canonical control plane"),
                                 now, existing["id"]),
                            )
                            existing = connection.execute(
                                "SELECT * FROM registration_requests WHERE id=?", (existing["id"],)
                            ).fetchone()
                if existing is not None:
                    request_id = str(existing["id"])
                    existing_status = str(existing["status"] or "")
                    existing_stage = str(existing["onboarding_state"] or "").upper()
                    if account_exists and not (
                        existing_status == "Approved" and existing_stage == "COMPLETE"
                    ):
                        # The account registry is authoritative for an account
                        # that already exists. Reconcile a stale Pending/partial
                        # registration without creating a new owner notification.
                        connection.execute(
                            "UPDATE registration_requests SET status='Approved', "
                            "onboarding_state='COMPLETE', approved_at=COALESCE(approved_at, ?), "
                            "decided_by=COALESCE(decided_by, 'account_registry'), decision_note=COALESCE(decision_note, ?), "
                            "registration_grant_hash=NULL, registration_grant_expires_at=NULL, updated_at=? WHERE id=?",
                            (now, "Reconciled from existing account", now, request_id),
                        )
                    # Rotate only the opaque lookup token. Lifecycle state,
                    # approval timestamp, and the Pending countdown remain
                    # untouched for an already-active registration.
                    connection.execute(
                        "UPDATE registration_requests SET status_token_hash=? WHERE id=?",
                        (self._token_hash(token), request_id),
                    )
                elif account_exists:
                    # A completed account can legitimately exist without a
                    # legacy registration_requests row. Seed a terminal record
                    # so future status lookups remain durable and never create
                    # a fresh Pending request or owner notification.
                    connection.execute(
                        "INSERT INTO registration_requests "
                        "(id,status_token_hash,email,username,status,created_at,updated_at,decided_by,decision_note,approved_at,onboarding_state) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (request_id, self._token_hash(token), normalized_email, normalized_username, "Approved", now, now,
                         "account_registry", "Reconciled from existing account", now, "COMPLETE"),
                    )
                else:
                    connection.execute(
                        "INSERT INTO registration_requests "
                        "(id,status_token_hash,email,username,status,created_at,updated_at,decided_by,decision_note,approved_at,onboarding_state) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (request_id, self._token_hash(token), normalized_email, normalized_username, "Pending", now, now, None, None, None, "PENDING_APPROVAL"),
                    )
        except sqlite3.IntegrityError:
            # Do not disclose whether an account/request exists. A new opaque
            # token is attached to the existing active request instead. This is
            # only a race fallback; the canonical lookup above is the normal
            # path and therefore runs before request creation.
            with self._connect() as connection:
                row = self._canonical_registration(connection, normalized_email)
                if not row:
                    raise
                request_id = str(row["id"])
                if account_exists and not (
                    str(row["status"] or "") == "Approved"
                    and str(row["onboarding_state"] or "").upper() == "COMPLETE"
                ):
                    connection.execute(
                        "UPDATE registration_requests SET status='Approved', "
                        "onboarding_state='COMPLETE', approved_at=COALESCE(approved_at, ?), "
                        "decided_by=COALESCE(decided_by, 'account_registry'), decision_note=COALESCE(decision_note, ?), "
                        "registration_grant_hash=NULL, registration_grant_expires_at=NULL, updated_at=? WHERE id=?",
                        (now, "Reconciled from existing account", now, request_id),
                    )
                connection.execute(
                    "UPDATE registration_requests SET status_token_hash=? WHERE id=?",
                    (self._token_hash(token), request_id),
                )
        return token, self.registration_status(token) or {}

    def registration_status(self, token: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, email, username, status, created_at, updated_at, approved_at, onboarding_state, "
                "otp_verified_at, registration_grant_expires_at FROM registration_requests WHERE status_token_hash=?",
                (self._token_hash(token),),
            ).fetchone()
            if not row:
                return None
            canonical = self._canonical_registration(connection, row["email"])
            if canonical is not None:
                row = canonical
            result = dict(row)
            created = datetime.fromisoformat(result["created_at"])
            deadline = created + timedelta(hours=48)
            if result["status"] == "Pending" and datetime.now(timezone.utc) > deadline:
                result["status"] = "Overdue"
            result["decision_due_at"] = deadline.isoformat()
            return result

    def registration_requests(self) -> list[dict[str, Any]]:
        """Return only requests that still need an owner decision."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,email,username,status,created_at AS requested_at,updated_at,owner_read_at "
                "FROM registration_requests WHERE status IN ('Pending','Overdue') "
                "ORDER BY CASE status WHEN 'Pending' THEN 0 ELSE 1 END, created_at DESC"
            ).fetchall()
            requests = self._rows(rows)
            for request in requests:
                request["owner_unread"] = not bool(request.get("owner_read_at"))
            return requests

    def mark_registration_requests_read(self) -> int:
        """Mark the current canonical owner review queue as seen.

        Read state lives on the registration request itself so polling cannot
        create duplicate notifications and approval history remains the single
        authority for the request lifecycle.
        """
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE registration_requests SET owner_read_at=? "
                "WHERE status IN ('Pending','Overdue') AND owner_read_at IS NULL",
                (now,),
            )
            return max(0, int(cursor.rowcount or 0))

    def registration_history(
        self, *, decision: str = "all", limit: int = 25, offset: int = 0, all_records: bool = False
    ) -> dict[str, Any]:
        """Return a compact, persistent owner-review history page."""
        normalized_decision = str(decision or "all").strip().casefold()
        status_filter = {"approved": "Approved", "rejected": "Declined"}.get(normalized_decision)
        safe_limit = max(1, min(int(limit or 25), 100))
        safe_offset = max(0, int(offset or 0))
        with self._connect() as connection:
            if all_records and status_filter:
                rows = connection.execute(
                    "SELECT id,email,username,status,updated_at AS decision_at,approved_at "
                    "FROM registration_requests WHERE status=? ORDER BY updated_at DESC",
                    (status_filter,),
                ).fetchall()
            elif all_records:
                rows = connection.execute(
                    "SELECT id,email,username,status,updated_at AS decision_at,approved_at "
                    "FROM registration_requests WHERE status IN ('Approved','Declined') ORDER BY updated_at DESC"
                ).fetchall()
            elif status_filter:
                rows = connection.execute(
                    "SELECT id,email,username,status,updated_at AS decision_at,approved_at "
                    "FROM registration_requests WHERE status=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (status_filter, safe_limit + 1, safe_offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id,email,username,status,updated_at AS decision_at,approved_at "
                    "FROM registration_requests WHERE status IN ('Approved','Declined') "
                    "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (safe_limit + 1, safe_offset),
                ).fetchall()
        records = self._rows(rows)
        return {
            "records": records if all_records else records[:safe_limit],
            "has_more": False if all_records else len(records) > safe_limit,
        }

    def decide_registration(self, request_id: str, *, owner: str, approve: bool, note: str = "") -> dict[str, Any] | None:
        status = "Approved" if approve else "Declined"
        now = _now()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM registration_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                return None
            if str(row["status"] or "") not in {"Pending", "Overdue"}:
                return None
            connection.execute(
                "UPDATE registration_requests SET status=?,updated_at=?,decided_by=?,decision_note=?,approved_at=?,"
                "onboarding_state=? WHERE id=?",
                (status, now, owner, str(note or "")[:1000], now if approve else None,
                 "APPROVED" if approve else "DECLINED", request_id),
            )
            return dict(connection.execute("SELECT * FROM registration_requests WHERE id=?", (request_id,)).fetchone())

    def registration_is_approved(self, email: str) -> bool:
        with self._connect() as connection:
            row = self._canonical_registration(connection, email)
            return bool(row and str(row["status"] or "") == "Approved")

    def mark_registration_otp_pending(self, email: str) -> None:
        with self._connect() as connection:
            row = self._canonical_registration(connection, email)
            if not row or str(row["status"] or "") != "Approved":
                return
            connection.execute(
                "UPDATE registration_requests SET onboarding_state='OTP_PENDING',updated_at=? "
                "WHERE id=?",
                (_now(), row["id"]),
            )

    def mark_registration_otp_verified(self, email: str, grant: str, expires_at: datetime) -> None:
        with self._connect() as connection:
            row = self._canonical_registration(connection, email)
            if not row or str(row["status"] or "") != "Approved":
                return
            connection.execute(
                "UPDATE registration_requests SET onboarding_state='API_KEY_PENDING',otp_verified_at=?,"
                "registration_grant_hash=?,registration_grant_expires_at=?,updated_at=? "
                "WHERE id=?",
                (_now(), self._token_hash(grant), expires_at.astimezone(timezone.utc).isoformat(), _now(), row["id"]),
            )

    def issue_registration_resume_grant(self, status_token: str) -> str | None:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT email FROM registration_requests WHERE status_token_hash=?",
                (self._token_hash(status_token),),
            ).fetchone()
            if not row:
                return None
            canonical = self._canonical_registration(connection, row["email"])
            if not canonical or str(canonical["status"] or "") != "Approved" or str(canonical["onboarding_state"] or "") != "API_KEY_PENDING":
                return None
            grant = secrets.token_urlsafe(32)
            expires_at = now + timedelta(minutes=10)
            connection.execute(
                "UPDATE registration_requests SET registration_grant_hash=?,registration_grant_expires_at=?,updated_at=? WHERE id=?",
                (self._token_hash(grant), expires_at.isoformat(), _now(), canonical["id"]),
            )
            return grant

    def registration_grant_is_valid(self, email: str, grant: str) -> bool:
        with self._connect() as connection:
            row = self._canonical_registration(connection, email)
            if not row or str(row["onboarding_state"] or "") != "API_KEY_PENDING":
                return False
            expires = str(row["registration_grant_expires_at"] or "")
            try:
                valid_time = datetime.fromisoformat(expires) > datetime.now(timezone.utc)
            except ValueError:
                valid_time = False
            return bool(valid_time and secrets.compare_digest(
                str(row["registration_grant_hash"] or ""), self._token_hash(grant)
            ))

    def mark_registration_complete(self, email: str) -> None:
        with self._connect() as connection:
            row = self._canonical_registration(connection, email)
            if not row or str(row["status"] or "") != "Approved":
                return
            connection.execute(
                "UPDATE registration_requests SET onboarding_state='COMPLETE',registration_grant_hash=NULL,"
                "registration_grant_expires_at=NULL,updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )

    def create_registration_recovery_code(
        self,
        email: str,
        *,
        issued_by: str = "Authorized Local Operator",
        duration_minutes: int = 60,
    ) -> tuple[str, dict[str, Any]]:
        normalized_email = email.strip().lower()
        if not normalized_email or "@" not in normalized_email:
            raise ValueError("invalid_email")

        now_dt = datetime.now(timezone.utc)
        now_str = _now()
        expires_dt = now_dt + timedelta(minutes=duration_minutes)
        expires_str = expires_dt.isoformat()

        # Random recovery code (8 random numeric digits, e.g. 10000000 - 99999999)
        code = f"{secrets.randbelow(90000000) + 10000000}"
        code_hash = self._token_hash(code)
        recovery_id = uuid.uuid4().hex

        with self._connect() as connection:
            # Must be an approved and incomplete registration request
            row = connection.execute(
                "SELECT id, status, onboarding_state, username FROM registration_requests "
                "WHERE email=? AND status='Approved' "
                "ORDER BY CASE status WHEN 'Approved' THEN 0 ELSE 1 END, created_at DESC LIMIT 1",
                (normalized_email,),
            ).fetchone()

            if not row:
                raise ValueError("approved_registration_required")

            if str(row["onboarding_state"] or "").upper() == "COMPLETE":
                raise ValueError("registration_already_completed")

            reg_id = str(row["id"])

            # Invalidate/revoke any previously active recovery codes for this email
            connection.execute(
                "UPDATE registration_recovery_codes SET revoked_at=? WHERE email=? AND used_at IS NULL AND revoked_at IS NULL",
                (now_str, normalized_email),
            )

            # Insert hashed recovery code record
            connection.execute(
                "INSERT INTO registration_recovery_codes "
                "(id, registration_id, email, code_hash, created_at, expires_at, used_at, revoked_at, issued_by) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                (recovery_id, reg_id, normalized_email, code_hash, now_str, expires_str, issued_by),
            )

        audit_record = {
            "id": recovery_id,
            "registration_id": reg_id,
            "email": normalized_email,
            "created_at": now_str,
            "expires_at": expires_str,
            "status": "active",
            "issued_by": issued_by,
        }
        return code, audit_record

    def verify_and_consume_registration_recovery_code(
        self,
        email: str,
        code: str,
    ) -> tuple[bool, str | None]:
        normalized_email = email.strip().lower()
        cleaned_code = "".join(str(code or "").split())
        if not normalized_email or not cleaned_code:
            return False, None

        code_hash = self._token_hash(cleaned_code)
        now_dt = datetime.now(timezone.utc)
        now_str = _now()

        with self._connect() as connection:
            row = connection.execute(
                "SELECT rc.id AS recovery_id, rc.registration_id, rc.expires_at, "
                "rr.status AS reg_status, rr.onboarding_state "
                "FROM registration_recovery_codes rc "
                "JOIN registration_requests rr ON rc.registration_id = rr.id "
                "WHERE rc.email=? AND rc.code_hash=? AND rc.used_at IS NULL AND rc.revoked_at IS NULL "
                "ORDER BY rc.created_at DESC LIMIT 1",
                (normalized_email, code_hash),
            ).fetchone()

            if not row:
                return False, None

            expires_str = str(row["expires_at"] or "")
            try:
                if datetime.fromisoformat(expires_str) <= now_dt:
                    return False, None
            except ValueError:
                return False, None

            if str(row["reg_status"] or "") != "Approved" or str(row["onboarding_state"] or "").upper() == "COMPLETE":
                return False, None

            # Consume recovery code (single use)
            connection.execute(
                "UPDATE registration_recovery_codes SET used_at=? WHERE id=?",
                (now_str, str(row["recovery_id"])),
            )

            # Issue registration grant for API key setup
            grant = secrets.token_urlsafe(32)
            grant_expires_at = (now_dt + timedelta(minutes=10)).isoformat()

            connection.execute(
                "UPDATE registration_requests SET "
                "onboarding_state='API_KEY_PENDING', "
                "otp_verified_at=?, "
                "registration_grant_hash=?, "
                "registration_grant_expires_at=?, "
                "updated_at=? "
                "WHERE id=?",
                (now_str, self._token_hash(grant), grant_expires_at, now_str, str(row["registration_id"])),
            )
            return True, grant

    def list_recovery_audit_records(self, email: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if email:
                rows = connection.execute(
                    "SELECT id, registration_id, email, created_at, expires_at, used_at, revoked_at, issued_by "
                    "FROM registration_recovery_codes WHERE email=? ORDER BY created_at DESC",
                    (email.strip().lower(),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, registration_id, email, created_at, expires_at, used_at, revoked_at, issued_by "
                    "FROM registration_recovery_codes ORDER BY created_at DESC",
                ).fetchall()
            records = []
            now_dt = datetime.now(timezone.utc)
            for r in rows:
                rec = dict(r)
                used = bool(rec.get("used_at"))
                revoked = bool(rec.get("revoked_at"))
                expired = False
                try:
                    expired = datetime.fromisoformat(rec["expires_at"]) <= now_dt
                except Exception:
                    pass
                rec["status"] = "used" if used else ("revoked" if revoked else ("expired" if expired else "active"))
                records.append(rec)
            return records
