"""Jupiter-owned task completion notifications (marker 159).

Long-running work (Claude Code / Codex / tool workers) REPORTS authenticated status to Jupiter; Jupiter alone decides
whether and how to tell the user. Workers never message or call the user themselves, and completion is never
inferred from silence, a closed window or an unchecked "done" sentence.

Lifecycle honesty: a new tracker is AWAITING_WORKER (nothing has started it; Jupiter does not launch Claude Code,
Codex or AntiGravity). It becomes RUNNING only when its worker reports so. The worker's report key is issued, or
rotated, for THAT task by its owner in Settings (older keys stop working); only a hash is stored and keys never appear
in chat, URLs, logs or list responses. COMPLETED needs proof that the task's OWN registered check passed: an
unrelated passing command (or a Stop hook merely firing) is not evidence that this task's requirements were met.

Storage extends the ACTIVE per-runtime SQLite store (the proactive-inbox database, services/initiative_engine.py):
the canonical job status (services.continuity_engine.JobStatus), each transition, and the notification event/delivery
records are written in ONE SQLite transaction, so a completion can never exist without its notification (or vice
versa). The app/inbox channel is delivered through the existing proactive inbox (quiet hours, conversation-busy and
compare-and-set delivery semantics). External channels (email, WhatsApp, SMS, phone) are modelled with the same
durable outbox but stay DISABLED until an authorized, verified, recurring-free provider is configured: nothing here
places calls, sends messages or spends money.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from services.continuity_engine import JobStatus

CHANNELS = ("app", "windows", "android", "call", "email", "whatsapp", "sms", "phone")
# "call" is a Jupiter in-app call (services/jupiter_calls.py): free, rings the owner's own Jupiter apps and connects to
# Live Conversation. "phone" is a real telephone/SIM call, which stays off (paid telephony).
DEVICE_CHANNELS = frozenset({"app", "windows", "android"})
TRIGGERS = ("success", "failure", "needs_attention", "always")
PRIORITIES = ("normal", "important", "urgent")
OUTCOMES = ("SUCCESS", "FAILED", "NEEDS_ATTENTION", "CANCELLED")
DELIVERY_STATES = ("queued", "accepted", "sent", "delivered", "read", "failed", "unknown", "dead", "cancelled",
                   "unavailable")
TERMINAL_JOB = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.FAILED_ALL_FREE_ROUTES, JobStatus.CANCELLED})
# Canonical job status -> notification outcome. Progress states produce no notification (no spam).
OUTCOME_FOR = {
    JobStatus.COMPLETED: "SUCCESS",
    JobStatus.FAILED: "FAILED",
    JobStatus.FAILED_ALL_FREE_ROUTES: "FAILED",
    JobStatus.CANCELLED: "CANCELLED",
    JobStatus.WAITING_REQUIRED_INPUT: "NEEDS_ATTENTION",
    JobStatus.WAITING_CONFIRMATION: "NEEDS_ATTENTION",
}
OPEN_JOB = frozenset({JobStatus.AWAITING_WORKER, JobStatus.RUNNING, JobStatus.REVIEWING,
                      JobStatus.WAITING_REQUIRED_INPUT, JobStatus.WAITING_CONFIRMATION})
ALLOWED_TRANSITIONS = {
    JobStatus.AWAITING_WORKER: {JobStatus.RUNNING, JobStatus.REVIEWING, JobStatus.COMPLETED, JobStatus.FAILED,
                                JobStatus.FAILED_ALL_FREE_ROUTES, JobStatus.CANCELLED, JobStatus.WAITING_REQUIRED_INPUT,
                                JobStatus.WAITING_CONFIRMATION},
    JobStatus.RUNNING: {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.FAILED_ALL_FREE_ROUTES, JobStatus.CANCELLED,
                        JobStatus.WAITING_REQUIRED_INPUT, JobStatus.WAITING_CONFIRMATION, JobStatus.REVIEWING},
    JobStatus.REVIEWING: {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.RUNNING,
                          JobStatus.WAITING_REQUIRED_INPUT},
    # resumable; a worker whose registered check now passes may complete straight from here
    JobStatus.WAITING_REQUIRED_INPUT: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.COMPLETED},
    JobStatus.WAITING_CONFIRMATION: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED},
}
# Only these channels exist in this runtime today. External ones are listed with the precise reason they are off.
CHANNEL_AVAILABILITY = {
    "app": (True, "Jupiter chat inbox (delivered when Jupiter is open; no lock-screen content)."),
    "windows": (True, "Windows notification while Jupiter is running and you have allowed notifications."),
    "android": (True, "Android in-app delivery while the Jupiter app is running (no remote push yet)."),
    "call": (True, "A Jupiter call in the Jupiter app (not a phone call). It rings your signed-in Jupiter apps; a "
                   "closed Android app rings only when free push is configured, and calls follow your quiet hours."),
    "email": (False, "Email notifications are not configured (no verified sender for task notifications)."),
    "whatsapp": (False, "WhatsApp needs the official WhatsApp Business Platform, which is not set up; outside the "
                        "24-hour service window it is paid."),
    "sms": (False, "SMS needs a paid telecom provider; Jupiter's free-only policy keeps it off."),
    "phone": (False, "Calls to your phone number need a paid telephony provider; Jupiter's free-only policy keeps them "
                     "off. A Jupiter call inside the app is free."),
}


class NotificationError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def normalize_check(check: str) -> str:
    """The registered completion check, compared whitespace-insensitively with what the reporter actually ran."""
    return " ".join(str(check or "").split())[:200]


REPORT_ENV_NAMES = ("JUPITER_TASK_URL", "JUPITER_TASK_ID", "JUPITER_TASK_RUN", "JUPITER_TASK_TOKEN")


# ── preference parsing: task text and notification preference are separate ──────────────────────────────────────
_CHANNEL_WORDS = {
    "whatsapp": r"whats\s*app|whatsapp|wa\b",
    # A real telephone call only when the user names the phone line itself; "call me" is a Jupiter in-app call.
    "phone": r"\b(?:phone|mobile|sim|cell)\s*(?:number|line)\b|\bphone\s*call\b|\b(?:sim|pstn|cellular)\s+call\b",
    "call": r"\bcall\b|\bring\s+me\b|\bjupiter\s+call\b",
    "sms": r"\bsms\b|\btext\s+(?:me|message)\b",
    "email": r"\be-?mail\b|\bmail\s+me\b",
    "windows": r"\bwindows\s+notification\b|\bdesktop\s+notification\b|\bnotify\s+me\s+on\s+(?:my\s+)?(?:laptop|pc)\b",
    "android": r"\bon\s+my\s+phone\b(?!\s*call)|\bandroid\b",
}
_NEGATED = r"(?:don'?t|do\s+not|never|no|without)\s+(?:\w+\s+){0,2}"


@dataclass(frozen=True)
class NotificationPreference:
    channels: tuple[str, ...] = ("app",)
    excluded: tuple[str, ...] = ()
    trigger: str = "always"
    priority: str = "normal"
    exclusive: bool = False                     # "only WhatsApp"
    respect_quiet_hours: bool = True
    explicit: bool = False                      # the user actually asked to be told

    def as_dict(self) -> dict[str, Any]:
        return {"channels": list(self.channels), "excluded": list(self.excluded), "trigger": self.trigger,
                "priority": self.priority, "exclusive": self.exclusive,
                "respect_quiet_hours": self.respect_quiet_hours, "explicit": self.explicit}


def parse_preference(text: str) -> NotificationPreference:
    """Extract ONLY the notification preference from a request ("WhatsApp me when done", "only WhatsApp; don't
    call", "tell me if it fails"). The task itself is left to the normal task path."""
    value = " ".join(str(text or "").casefold().split())
    excluded = tuple(channel for channel, pattern in _CHANNEL_WORDS.items()
                     if re.search(_NEGATED + f"(?:{pattern})", value))
    if "call" in excluded and "phone" not in excluded:
        excluded += ("phone",)                       # "don't call" rules out every kind of call
    requested = tuple(channel for channel, pattern in _CHANNEL_WORDS.items()
                      if channel not in excluded and re.search(pattern, value))
    exclusive = bool(re.search(r"\bonly\b", value) and requested)
    if re.search(r"\b(?:if|when)\s+it\s+fails?\b|\bon\s+failure\b|\bif\s+(?:anything|something)\s+goes\s+wrong\b", value):
        trigger = "failure"
    elif re.search(r"\b(?:needs?|requires?)\s+(?:me|my\s+input|attention)\b", value):
        trigger = "needs_attention"
    elif re.search(r"\b(?:when|once|after)\b.*\b(?:done|finish(?:es|ed)?|complete[sd]?|ready)\b", value):
        trigger = "always"                       # done = success, failure, attention or cancellation (never progress)
    else:
        trigger = "always"
    priority = "urgent" if re.search(r"\burgent(?:ly)?\b|\basap\b|\bimmediately\b", value) else (
        "important" if re.search(r"\bimportant\b", value) else "normal")
    explicit = bool(requested or re.search(r"\b(?:tell|notify|let\s+me\s+know|ping|remind)\b", value))
    channels = requested if exclusive else tuple(dict.fromkeys(("app", *requested)))
    return NotificationPreference(channels=channels or ("app",), excluded=excluded, trigger=trigger,
                                  priority=priority, exclusive=exclusive, explicit=explicit)


def channel_plan(preference: NotificationPreference, configured: frozenset[str] = frozenset()) -> dict[str, Any]:
    """What will actually happen, said BEFORE anything is promised. Excluded channels are never used; a requested
    but unavailable channel is explained; the app inbox stays the safe default unless the user said 'only X'.
    ``configured`` names external channels that have an authorized, verified adapter in this runtime."""
    usable, unavailable = [], []
    for channel in preference.channels:
        if channel in preference.excluded:
            continue
        available, reason = CHANNEL_AVAILABILITY.get(channel, (False, "Unknown channel."))
        if channel in configured:
            available, reason = True, "Configured authorized channel."
        (usable if available else unavailable).append({"channel": channel, "reason": reason})
    fallback = None
    if not usable and not preference.exclusive:
        fallback = "app"
    elif not usable and preference.exclusive:
        fallback = None                             # "only WhatsApp": never silently switch to email or a call
    return {"usable": usable, "unavailable": unavailable, "fallback": fallback,
            "never": list(preference.excluded), "needs_choice": bool(not usable and preference.exclusive)}


def safe_summary(title: str, outcome: str, *, sensitive: bool, action_required: str = "") -> str:
    """Short, truthful, secret-free text. Sensitive jobs only say 'open Jupiter'. Never logs or raw output."""
    if sensitive:
        return {"SUCCESS": "A task finished. Open Jupiter for details.",
                "FAILED": "A task did not finish. Open Jupiter for details.",
                "NEEDS_ATTENTION": "A task needs you. Open Jupiter to continue.",
                "CANCELLED": "A task was cancelled. Open Jupiter for details."}[outcome]
    name = re.sub(r"\s+", " ", str(title or "Your task")).strip()[:80]
    text = {"SUCCESS": f"Done: {name}.", "FAILED": f"Failed: {name}.",
            "NEEDS_ATTENTION": f"Needs you: {name}.", "CANCELLED": f"Cancelled: {name}."}[outcome]
    if action_required and outcome == "NEEDS_ATTENTION":
        text += f" {re.sub(r'[^\w .,:;!?()-]', '', str(action_required))[:140]}"
    return text


class ChannelAdapter(Protocol):
    def send(self, *, recipient_ref: str, text: str, idempotency_key: str) -> dict[str, Any]: ...
    def status(self, *, provider_ref: str) -> str: ...


@dataclass
class TaskNotificationStore:
    """Durable jobs + transitions + notification deliveries in the active per-runtime SQLite store."""
    path: Path
    clock: Callable[[], datetime] = _now
    adapters: dict[str, ChannelAdapter] = field(default_factory=dict)   # external channels: empty = all disabled
    max_attempts: int = 5

    @classmethod
    def default(cls) -> "TaskNotificationStore":
        from services.initiative_engine import InitiativeEngine
        return cls(path=InitiativeEngine().path)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        from services.initiative_engine import InitiativeEngine
        InitiativeEngine(self.path)                          # the inbox schema this store writes into, same file
        with closing(self._connect()) as connection, connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                    run_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
                    sensitive INTEGER NOT NULL DEFAULT 0, preference_json TEXT NOT NULL, report_token_hash TEXT NOT NULL,
                    validation_required INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    chat_id TEXT NOT NULL DEFAULT '', request_key TEXT NOT NULL DEFAULT '',
                    completion_check TEXT NOT NULL DEFAULT '', key_version INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_owner ON agent_tasks(owner, updated_at);
                CREATE TABLE IF NOT EXISTS agent_task_transitions (
                    transition_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL, event_id TEXT NOT NULL, from_status TEXT NOT NULL, to_status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(task_id, event_id));
                CREATE TABLE IF NOT EXISTS task_notification_deliveries (
                    delivery_id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL, outcome TEXT NOT NULL, channel TEXT NOT NULL, recipient_ref TEXT NOT NULL,
                    status TEXT NOT NULL, text TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
                    provider_ref TEXT, last_error TEXT, inbox_event_id TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner, task_id, run_id, attempt, outcome, recipient_ref, channel));
            """)
            # Additive upgrade of a marker-159 staging table created before the review repair.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_tasks)")}
            for name, ddl in (("chat_id", "TEXT NOT NULL DEFAULT ''"), ("request_key", "TEXT NOT NULL DEFAULT ''"),
                              ("completion_check", "TEXT NOT NULL DEFAULT ''"),
                              ("key_version", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE agent_tasks ADD COLUMN {name} {ddl}")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_request ON agent_tasks(owner, request_key)")
            # Marker 174: the reply a Jupiter-run background job produced (owner-scoped; shown in the card/call).
            connection.execute("CREATE TABLE IF NOT EXISTS agent_task_results (task_id TEXT PRIMARY KEY, owner TEXT "
                               "NOT NULL, result_text TEXT NOT NULL, created_at TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    # ── tasks ─────────────────────────────────────────────────────────────────────────────────────────────────
    def create_task(self, owner: str, title: str, preference: NotificationPreference, *, sensitive: bool = False,
                    validation_required: bool = True, chat_id: str = "", request_key: str = "",
                    completion_check: str = "", issue_key: bool = True) -> dict[str, Any]:
        """Create a tracker in AWAITING_WORKER. ``request_key`` de-duplicates: while an open task with the same key
        exists for this owner, that task is returned (``duplicate``) and nothing new is created. ``issue_key`` returns
        a one-time report key now (Settings setup); chat-created trackers get theirs later via issue_report_key."""
        owner = str(owner or "").strip().casefold()
        if not owner:
            raise NotificationError("A signed-in owner is required.")
        task_id, run_id = uuid4().hex, uuid4().hex
        token = secrets.token_urlsafe(32) if issue_key else ""
        now = _iso(self.clock())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if request_key:
                    existing = connection.execute(
                        f"SELECT * FROM agent_tasks WHERE owner=? AND request_key=? AND status IN "
                        f"({','.join('?' * len(OPEN_JOB))}) ORDER BY created_at DESC LIMIT 1",
                        (owner, request_key, *sorted(status.value for status in OPEN_JOB))).fetchone()
                    if existing is not None:
                        connection.execute("COMMIT")
                        return {**self._public(existing), "duplicate": True}
                connection.execute(
                    "INSERT INTO agent_tasks (task_id, owner, title, status, run_id, attempt, version, sensitive, "
                    "preference_json, report_token_hash, validation_required, created_at, updated_at, chat_id, "
                    "request_key, completion_check, key_version) VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, owner, str(title or "Task")[:200], JobStatus.AWAITING_WORKER.value, run_id,
                     int(bool(sensitive)), json.dumps(preference.as_dict()), _hash_token(token) if token else "",
                     int(bool(validation_required)), now, now, str(chat_id or "")[:180], str(request_key or "")[:64],
                     normalize_check(completion_check), 1 if token else 0))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        created = {"task_id": task_id, "run_id": run_id, "attempt": 1, "title": str(title or "Task")[:200],
                   "status": JobStatus.AWAITING_WORKER.value, "duplicate": False,
                   "completion_check": normalize_check(completion_check),
                   "channels": channel_plan(preference, frozenset(self.adapters))}
        if token:
            created["report_token"] = token
        return created

    def open_tasks(self, owner: str) -> list[dict[str, Any]]:
        """This owner's unfinished trackers, newest first, with the chat each came from (for attaching a follow-up
        notification request to existing work)."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_tasks WHERE owner=? AND status IN ({','.join('?' * len(OPEN_JOB))}) "
                "ORDER BY created_at DESC LIMIT 20",
                (str(owner or "").casefold(), *sorted(status.value for status in OPEN_JOB))).fetchall()
        return [{**self._public(row), "chat_id": row["chat_id"]} for row in rows]

    def update_preference(self, owner: str, task_id: str, preference: NotificationPreference) -> dict[str, Any]:
        """Attach a later notification instruction to an existing, unfinished task of the same owner."""
        with closing(self._connect()) as connection:
            changed = connection.execute(
                f"UPDATE agent_tasks SET preference_json=?, version=version+1, updated_at=? WHERE task_id=? AND "
                f"owner=? AND status IN ({','.join('?' * len(OPEN_JOB))})",
                (json.dumps(preference.as_dict()), _iso(self.clock()), task_id, str(owner or "").casefold(),
                 *sorted(status.value for status in OPEN_JOB))).rowcount
        if not changed:
            raise NotificationError("Task not found.")
        return self.get(owner, task_id)

    def issue_report_key(self, owner: str, task_id: str, *, completion_check: str | None = None) -> dict[str, Any]:
        """Owner-only: issue (or rotate) the report key for THIS existing task and run, optionally registering the
        check that proves it is done. Any older key stops working immediately. The key is returned once."""
        owner = str(owner or "").casefold()
        token = secrets.token_urlsafe(32)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM agent_tasks WHERE task_id=? AND owner=?",
                                         (task_id, owner)).fetchone()
                if row is None:
                    raise NotificationError("Task not found.")
                status = JobStatus(row["status"])
                if status not in OPEN_JOB:
                    raise NotificationError(f"The task is already {status.value.lower()}; run it again for a new key.")
                check = normalize_check(completion_check) if completion_check is not None else row["completion_check"]
                connection.execute(
                    "UPDATE agent_tasks SET report_token_hash=?, key_version=key_version+1, completion_check=?, "
                    "version=version+1, updated_at=? WHERE task_id=? AND owner=?",
                    (_hash_token(token), check, _iso(self.clock()), task_id, owner))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"task_id": task_id, "run_id": row["run_id"], "attempt": row["attempt"], "report_token": token,
                "completion_check": check, "env_names": list(REPORT_ENV_NAMES)}

    def save_result(self, owner: str, task_id: str, text: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO agent_task_results (task_id, owner, result_text, created_at) SELECT ?, ?, ?, ? WHERE EXISTS "
                "(SELECT 1 FROM agent_tasks WHERE task_id=? AND owner=?) ON CONFLICT(task_id) DO UPDATE SET "
                "result_text=excluded.result_text, created_at=excluded.created_at",
                (task_id, str(owner).casefold(), str(text)[:8000], _iso(self.clock()), task_id, str(owner).casefold()))

    def result(self, owner: str, task_id: str) -> str:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT result_text FROM agent_task_results WHERE task_id=? AND owner=?",
                                     (task_id, str(owner).casefold())).fetchone()
        return row["result_text"] if row else ""

    def running_owners(self, *, fresh_within: timedelta = timedelta(hours=6)) -> list[str]:
        """Owners with a background task actually running (RUNNING / REVIEWING) and reported on recently: these count
        as active work until the task finishes, fails or is cancelled. A worker that vanished without reporting stops
        counting after ``fresh_within`` instead of inflating the load forever. Owners only (no titles)."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT owner FROM agent_tasks WHERE status IN (?, ?) AND updated_at>?",
                (JobStatus.RUNNING.value, JobStatus.REVIEWING.value, _iso(self.clock() - fresh_within))).fetchall()
        return [row["owner"] for row in rows]

    def get(self, owner: str, task_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM agent_tasks WHERE task_id=? AND owner=?",
                                     (task_id, str(owner or "").casefold())).fetchone()
        return self._public(row) if row else None

    def list(self, owner: str, limit: int = 30) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM agent_tasks WHERE owner=? ORDER BY updated_at DESC LIMIT ?",
                                      (str(owner or "").casefold(), max(1, min(100, limit)))).fetchall()
        return [self._public(row) for row in rows]

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        item = {k: row[k] for k in ("task_id", "title", "status", "run_id", "attempt", "version", "created_at",
                                    "updated_at", "completion_check")}
        item["worker_connected"] = bool(row["report_token_hash"])        # whether a report key was issued
        item["sensitive"] = bool(row["sensitive"])
        item["preference"] = json.loads(row["preference_json"])
        return item

    def authenticate_report(self, task_id: str, token: str) -> sqlite3.Row:
        """A worker's report token is scoped to ONE task, stored only as a hash, compared in constant time."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (str(task_id or ""),)).fetchone()
        if row is None or not token or not row["report_token_hash"] or not hmac.compare_digest(
                row["report_token_hash"], _hash_token(token)):
            raise NotificationError("Invalid task report credentials.")
        return row

    def rerun(self, owner: str, task_id: str) -> dict[str, Any]:
        """A distinct new run/attempt with a fresh token; earlier runs keep their own notifications."""
        token, run_id = secrets.token_urlsafe(32), uuid4().hex
        now = _iso(self.clock())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE agent_tasks SET status=?, run_id=?, attempt=attempt+1, version=version+1, report_token_hash=?, "
                "key_version=key_version+1, updated_at=? WHERE task_id=? AND owner=?",
                (JobStatus.AWAITING_WORKER.value, run_id, _hash_token(token), now, task_id,
                 str(owner).casefold())).rowcount
            connection.execute("COMMIT")
        if not changed:
            raise NotificationError("Task not found.")
        task = self.get(owner, task_id)
        return {**task, "report_token": token}

    # ── the atomic transition ─────────────────────────────────────────────────────────────────────────────────
    def transition(self, *, task_id: str, token: str | None = None, owner: str | None = None, run_id: str,
                   event_id: str, status: str, evidence: dict[str, Any] | None = None,
                   action_required: str = "") -> dict[str, Any]:
        """Apply one reported status for one run. Duplicate events are idempotent; reports for an old run, invalid
        transitions and 'completed' without real evidence are refused. The job update, the transition row, the
        notification delivery rows and the inbox event are committed together or not at all."""
        try:
            new_status = JobStatus(str(status or "").upper())
        except ValueError as error:
            raise NotificationError("Unknown status.") from error
        if token is not None:
            row = self.authenticate_report(task_id, token)
        else:
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT * FROM agent_tasks WHERE task_id=? AND owner=?",
                                         (task_id, str(owner or "").casefold())).fetchone()
            if row is None:
                raise NotificationError("Task not found.")
        evidence = dict(evidence or {})
        if new_status == JobStatus.COMPLETED and row["validation_required"]:
            checks = evidence.get("validation")
            if not (isinstance(checks, dict) and checks.get("passed") is True and checks.get("command_or_check")):
                raise NotificationError("Completion needs validation evidence (which check ran and that it passed).")
            if not row["completion_check"]:
                raise NotificationError("This task has no registered check yet; register the check that proves it "
                                        "is done before completion can be reported.")
            if normalize_check(checks.get("command_or_check")) != row["completion_check"]:
                raise NotificationError("The check that ran is not the registered check for this task, so it does "
                                        "not prove this task is done.")
        event_id = str(event_id or "")[:120]
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,120}", event_id):
            raise NotificationError("A unique event id (8-120 safe characters) is required.")
        now = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute("SELECT 1 FROM agent_task_transitions WHERE task_id=? AND event_id=?",
                                      (task_id, event_id)).fetchone():
                    connection.execute("COMMIT")
                    return {"duplicate": True, **self._public(connection.execute(
                        "SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone())}
                current = connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
                if str(run_id) != current["run_id"]:
                    raise NotificationError("This report belongs to an earlier run.")
                old_status = JobStatus(current["status"])
                if old_status in TERMINAL_JOB:
                    raise NotificationError(f"The task is already {old_status.value.lower()}.")
                if new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
                    raise NotificationError(f"{old_status.value} cannot become {new_status.value}.")
                changed = connection.execute(
                    "UPDATE agent_tasks SET status=?, version=version+1, updated_at=? WHERE task_id=? AND version=?",
                    (new_status.value, _iso(now), task_id, current["version"])).rowcount
                if not changed:
                    raise NotificationError("Concurrent update; report again.")
                safe_evidence = {k: evidence[k] for k in ("validation", "artifact_refs", "duration_seconds")
                                 if k in evidence}
                connection.execute(
                    "INSERT INTO agent_task_transitions (transition_id, task_id, owner, run_id, attempt, event_id, "
                    "from_status, to_status, evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (uuid4().hex, task_id, current["owner"], current["run_id"], current["attempt"], event_id,
                     old_status.value, new_status.value, json.dumps(safe_evidence)[:4000], _iso(now)))
                deliveries = self._enqueue(connection, current, new_status, now, action_required)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"duplicate": False, "status": new_status.value, "notifications": deliveries}

    def _enqueue(self, connection, task: sqlite3.Row, status: JobStatus, now: datetime,
                 action_required: str) -> list[dict[str, Any]]:
        outcome = OUTCOME_FOR.get(status)
        if outcome is None:
            return []                                         # progress: no unsolicited notification
        preference = json.loads(task["preference_json"])
        if outcome == "CANCELLED":
            # A cancelled task never calls, and a call still ringing for it stops everywhere.
            from services.jupiter_calls import ensure_schema_in_transaction
            ensure_schema_in_transaction(connection)
            connection.execute(
                "UPDATE call_invitations SET status='cancelled', end_reason='source_cancelled', ended_at=?, "
                "version=version+1, push_cancel='pending' WHERE owner=? AND source_kind='task' AND source_id=? "
                "AND status='ringing'", (_iso(now), task["owner"], task["task_id"]))
        trigger = preference.get("trigger", "always")
        wanted = (trigger == "always" or (trigger == "success" and outcome == "SUCCESS")
                  or (trigger == "failure" and outcome in {"FAILED", "CANCELLED"})
                  or (trigger == "needs_attention" and outcome == "NEEDS_ATTENTION")
                  or outcome == "NEEDS_ATTENTION")            # a blocked task always tells its owner
        if not wanted:
            return []
        text = safe_summary(task["title"], outcome, sensitive=bool(task["sensitive"]), action_required=action_required)
        plan = channel_plan(NotificationPreference(
            channels=tuple(preference.get("channels") or ("app",)), excluded=tuple(preference.get("excluded") or ()),
            exclusive=bool(preference.get("exclusive"))), frozenset(self.adapters))
        channels = [item["channel"] for item in plan["usable"]] or ([plan["fallback"]] if plan["fallback"] else [])
        records = []
        shared_inbox_event = None
        for channel in dict.fromkeys(channels + [item["channel"] for item in plan["unavailable"]]):
            available = channel in DEVICE_CHANNELS or channel == "call" or channel in self.adapters
            delivery_id = uuid4().hex
            inbox_event_id = None
            provider_ref = None
            last_error = None
            status_value = "queued" if available else "unavailable"
            if channel == "call" and available:
                status_value, provider_ref, last_error = self._call(connection, task, outcome, now)
            if channel in {"app", "windows", "android"} and available:
                # One inbox event serves the app chat card AND the device notification the running client shows for
                # it (Windows notification / Android app); they share its compare-and-set delivered state.
                if shared_inbox_event is None:
                    shared_inbox_event = self._inbox_event(connection, task, outcome, text, preference, now)
                inbox_event_id = shared_inbox_event
                status_value = "accepted"                     # delivered when the inbox hands it to the user
            cursor = connection.execute(
                "INSERT OR IGNORE INTO task_notification_deliveries (delivery_id, owner, task_id, run_id, attempt, "
                "outcome, channel, recipient_ref, status, text, next_attempt_at, inbox_event_id, provider_ref, last_error, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (delivery_id, task["owner"], task["task_id"], task["run_id"], task["attempt"], outcome, channel,
                 f"owner:{task['owner']}", status_value, text, _iso(now) if status_value == "queued" else None,
                 inbox_event_id, provider_ref, last_error, _iso(now), _iso(now)))
            if cursor.rowcount:
                records.append({"channel": channel, "status": status_value, "outcome": outcome})
        return records

    @staticmethod
    def _call(connection, task: sqlite3.Row, outcome: str, now: datetime) -> tuple[str, str | None, str | None]:
        """The Jupiter call invitation is written in the SAME transaction as the job change (ringing happens only for
        a real, committed outcome). Calls off / quiet hours / already on a call leave a missed-call entry instead."""
        from services.jupiter_calls import create_invitation_in
        if outcome == "CANCELLED":
            return "cancelled", None, "task_cancelled"
        created = create_invitation_in(
            connection, task["owner"], source_kind="task", source_id=task["task_id"],
            source_key=f"task:{task['task_id']}:{task['run_id']}:{task['attempt']}:{outcome}", outcome=outcome, now=now)
        if created is None:
            return "cancelled", None, "duplicate"
        if created["status"] == "ringing":
            return "accepted", created["call_id"], None
        return "unavailable", created["call_id"], created["end_reason"]

    @staticmethod
    def _inbox_event(connection, task: sqlite3.Row, outcome: str, text: str, preference: dict, now: datetime) -> str:
        """The app/inbox delivery IS a proactive-inbox event, written in the SAME transaction as the job change."""
        event_id = uuid4().hex
        priority = {"urgent": "urgent", "important": "important"}.get(preference.get("priority"), "normal")
        payload = {"summary": text, "title": text, "task_id": task["task_id"], "run_id": task["run_id"],
                   "outcome": outcome, "respect_quiet_hours": preference.get("respect_quiet_hours", True),
                   "kind": "task_status"}
        connection.execute(
            "INSERT OR IGNORE INTO initiative_events (event_id, user_id, event_type, due_at, expires_at, priority, "
            "interruption_policy, payload_json, source_chat_id, source_message_id, created_at, updated_at) "
            "VALUES (?, ?, 'task_status', ?, NULL, ?, 'explicit_task', ?, NULL, ?, ?, ?)",
            (event_id, task["owner"], _iso(now), priority, json.dumps(payload),
             f"task:{task['task_id']}:{task['run_id']}:{task['attempt']}:{outcome}", _iso(now), _iso(now)))
        return event_id

    # ── external channel outbox (disabled until an adapter is configured) ─────────────────────────────────────
    def deliver_due(self) -> list[dict[str, Any]]:
        """One bounded pass over queued external deliveries. A timeout AFTER provider acceptance is UNKNOWN, never a
        failure: it is reconciled by status before any resend (no duplicate WhatsApps or calls)."""
        now = self.clock()
        results = []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM task_notification_deliveries WHERE status IN ('queued','unknown') AND channel NOT IN "
                "('app','windows','android') AND (next_attempt_at IS NULL OR next_attempt_at<=?) LIMIT 20",
                (_iso(now),)).fetchall()
        for row in rows:
            adapter = self.adapters.get(row["channel"])
            if adapter is None:
                self._update(row["delivery_id"], "unavailable", error="channel_not_configured")
                continue
            key = f"{row['task_id']}:{row['run_id']}:{row['attempt']}:{row['outcome']}:{row['channel']}"
            if row["status"] == "unknown" and row["provider_ref"]:
                state = adapter.status(provider_ref=row["provider_ref"])
                self._update(row["delivery_id"], state if state in DELIVERY_STATES else "unknown")
                results.append({"delivery_id": row["delivery_id"], "status": state})
                continue
            if not self._claim(row["delivery_id"]):
                continue                                       # another worker owns this attempt
            try:
                response = adapter.send(recipient_ref=row["recipient_ref"], text=row["text"], idempotency_key=key)
                state = str(response.get("status") or "accepted")
                self._update(row["delivery_id"], state if state in DELIVERY_STATES else "accepted",
                             provider_ref=str(response.get("provider_ref") or "")[:120])
            except TimeoutError:
                self._update(row["delivery_id"], "unknown", error="timeout_after_send")
            except Exception as error:
                tries = int(row["tries"]) + 1
                state = "dead" if tries >= self.max_attempts else "queued"
                delay = timedelta(seconds=min(3600, 30 * (2 ** tries)))
                self._update(row["delivery_id"], state, error=type(error).__name__[:60], next_at=now + delay)
            results.append({"delivery_id": row["delivery_id"]})
        return results

    def _claim(self, delivery_id: str) -> bool:
        with closing(self._connect()) as connection:
            return bool(connection.execute(
                "UPDATE task_notification_deliveries SET status='sending', tries=tries+1, updated_at=? "
                "WHERE delivery_id=? AND status='queued'", (_iso(self.clock()), delivery_id)).rowcount)

    def _update(self, delivery_id: str, status: str, *, error: str = "", provider_ref: str = "",
                next_at: datetime | None = None) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE task_notification_deliveries SET status=?, last_error=COALESCE(NULLIF(?, ''), last_error), "
                "provider_ref=COALESCE(NULLIF(?, ''), provider_ref), next_attempt_at=?, updated_at=? WHERE delivery_id=?",
                (status, error, provider_ref, _iso(next_at) if next_at else None, _iso(self.clock()), delivery_id))

    def deliveries(self, owner: str, task_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT channel, outcome, status, run_id, attempt, created_at, updated_at FROM "
                "task_notification_deliveries WHERE owner=? AND task_id=? ORDER BY created_at",
                (str(owner).casefold(), task_id)).fetchall()
        return [dict(row) for row in rows]

    def cancel_pending_for_owner(self, owner: str) -> int:
        """Logout/account switch: undelivered task notifications for this owner are cancelled on this device."""
        with closing(self._connect()) as connection:
            changed = connection.execute(
                "UPDATE task_notification_deliveries SET status='cancelled', updated_at=? WHERE owner=? AND "
                "status IN ('queued','unknown')", (_iso(self.clock()), str(owner).casefold())).rowcount
        return changed
