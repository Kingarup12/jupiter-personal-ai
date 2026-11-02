"""Deterministic, user-scoped proactive events and consented sharing.

Deterministic rules run first; bounded semantic fallback handles ambiguity. It persists
small scheduling records, directed messages, and facts that a user explicitly
marked shareable.  Private memory databases remain private and are never read
across accounts here.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PRIORITY_ORDER = {"urgent": 0, "important": 1, "normal": 2, "low": 3}
EVENT_TYPES = frozenset({
    "reminder", "upcoming_event", "follow_up", "weather_warning",
    "memory_check_in", "directed_message", "deadline", "birthday",
    "meeting", "meeting_follow_up", "trip", "appointment", "outdoor_plan",
    "task_status",                                  # marker 159: Jupiter-owned task completion notifications
})
VISIBILITY_SCOPES = frozenset({"PRIVATE", "SHARE_WITH_USERS", "SHARED_PROFILE"})
SENSITIVE_SOURCES = frozenset({
    "gmail", "email", "oauth", "api_key", "credential", "face", "face++",
    "health", "medical", "financial", "password", "private_chat", "no_persist",
})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


class InitiativeEngine:
    """SQLite-backed scheduler with explicit user and visibility boundaries."""

    DEFAULTS = {
        "enabled": True,
        "reminders": True,
        "followups": True,
        "weather_warnings": False,
        "directed_messages": True,
        "memory_followups": True,
        "allow_nonurgent_interruptions": False,
        "quiet_hours_configured": False,
        "quiet_start": "",
        "quiet_end": "",
        "timezone": "Asia/Calcutta",
    }

    def __init__(self, path: str | Path | None = None, *, semantic=None):
        root = Path(__file__).resolve().parents[1]
        data_root = Path(os.getenv("JUPITER_DATA_DIR", "").strip() or root / "data")
        # Marker 222: Jupiter WT shares production's scheduler (reminders, inbox, Jupiter calls and the device list), so a
        # "call me" made on the laptop rings the phone and vice versa - like JUPITER_USERS_DIR for chats (marker 216).
        shared = os.getenv("JUPITER_INITIATIVE_DB", "").strip()
        self.path = Path(path) if path else (Path(shared) if shared else data_root / "initiative.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.semantic = semantic
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS initiative_reasoning (
                    user_id TEXT NOT NULL, cache_key TEXT NOT NULL,
                    created_at TEXT NOT NULL, result_json TEXT,
                    PRIMARY KEY(user_id, cache_key)
                );
                CREATE TABLE IF NOT EXISTS proactive_settings (
                    user_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    reminders INTEGER NOT NULL DEFAULT 1,
                    followups INTEGER NOT NULL DEFAULT 1,
                    weather_warnings INTEGER NOT NULL DEFAULT 0,
                    directed_messages INTEGER NOT NULL DEFAULT 1,
                    memory_followups INTEGER NOT NULL DEFAULT 1,
                    allow_nonurgent_interruptions INTEGER NOT NULL DEFAULT 0,
                    quiet_start TEXT NOT NULL DEFAULT '',
                    quiet_end TEXT NOT NULL DEFAULT '',
                    quiet_hours_configured INTEGER NOT NULL DEFAULT 0,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Calcutta',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS initiative_events (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    expires_at TEXT,
                    priority TEXT NOT NULL,
                    interruption_policy TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    snoozed_until TEXT,
                    source_chat_id TEXT,
                    source_message_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, event_type, source_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_initiative_due
                    ON initiative_events(user_id, status, due_at);
                CREATE TABLE IF NOT EXISTS directed_messages (
                    message_id TEXT PRIMARY KEY,
                    sender_user_id TEXT NOT NULL,
                    recipient_user_id TEXT NOT NULL,
                    sender_display_name TEXT NOT NULL,
                    message_body TEXT NOT NULL,
                    deliver_after TEXT NOT NULL,
                    expires_at TEXT,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    source_message_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(sender_user_id, source_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_directed_delivery
                    ON directed_messages(recipient_user_id, status, deliver_after);
                CREATE TABLE IF NOT EXISTS shared_people_facts (
                    fact_id TEXT PRIMARY KEY,
                    source_user_id TEXT NOT NULL,
                    subject_label TEXT NOT NULL,
                    fact_text TEXT NOT NULL,
                    visibility_scope TEXT NOT NULL DEFAULT 'PRIVATE',
                    allowed_user_ids_json TEXT NOT NULL DEFAULT '[]',
                    allow_source_attribution INTEGER NOT NULL DEFAULT 0,
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shared_fact_source
                    ON shared_people_facts(source_user_id, visibility_scope);
                """
            )
            directed_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(directed_messages)")
            }
            for table in ("directed_messages", "initiative_events"):
                columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
                for column in ("delivery_decision", "delivery_reason"):
                    if column not in columns:
                        try:
                            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
                        except sqlite3.OperationalError as err:
                            if "duplicate column" not in str(err).lower():
                                raise
            if "source_message_id" not in directed_columns:
                try:
                    connection.execute("ALTER TABLE directed_messages ADD COLUMN source_message_id TEXT")
                except sqlite3.OperationalError as err:
                    if "duplicate column" not in str(err).lower():
                        raise
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_directed_source "
                "ON directed_messages(sender_user_id, source_message_id) "
                "WHERE source_message_id IS NOT NULL"
            )
            # Marker 157: a shared fact may be about a registered account (stable identity, never a name match), so
            # "I've heard about you from X" can only ever reach the person it is actually about.
            shared_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(shared_people_facts)")}
            if "subject_user_id" not in shared_columns:
                try:
                    connection.execute("ALTER TABLE shared_people_facts ADD COLUMN subject_user_id TEXT")
                except sqlite3.OperationalError as err:
                    if "duplicate column" not in str(err).lower():
                        raise
            # Per-conversation "that / it" references for explicit personal-assistant commands ("forget that",
            # "move that reminder", "share that with ...") and one pending reminder clarification.
            connection.execute(
                """CREATE TABLE IF NOT EXISTS assistant_last_actions (
                    user_id TEXT NOT NULL, chat_id TEXT NOT NULL, kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, chat_id, kind)
                )"""
            )
            proactive_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(proactive_settings)")
            }
            if "quiet_hours_configured" not in proactive_columns:
                connection.execute(
                    "ALTER TABLE proactive_settings ADD COLUMN quiet_hours_configured "
                    "INTEGER NOT NULL DEFAULT 0"
                )
                # Values different from Jupiter's former implicit 22:00–07:00
                # defaults can only have come from an explicit user edit.
                connection.execute(
                    "UPDATE proactive_settings SET quiet_hours_configured=1 "
                    "WHERE quiet_start<>'' AND quiet_end<>'' "
                    "AND NOT (quiet_start='22:00' AND quiet_end='07:00')"
                )

    @staticmethod
    def normalize_user(user_id: str) -> str:
        return str(user_id or "").strip().casefold()

    def remember_action(self, user_id: str, chat_id: str | None, kind: str, payload: dict[str, Any]) -> None:
        if not chat_id:
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO assistant_last_actions (user_id, chat_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, chat_id, kind) DO UPDATE SET payload_json=excluded.payload_json, created_at=excluded.created_at",
                (self.normalize_user(user_id), str(chat_id)[:120], kind, json.dumps(payload, ensure_ascii=False)[:8000], _iso(_utcnow())),
            )

    def last_action(self, user_id: str, chat_id: str | None, kind: str, *, max_age: timedelta = timedelta(hours=24)) -> dict | None:
        if not chat_id:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json, created_at FROM assistant_last_actions WHERE user_id=? AND chat_id=? AND kind=?",
                (self.normalize_user(user_id), str(chat_id)[:120], kind),
            ).fetchone()
        if not row or row[1] < _iso(_utcnow() - max_age):
            return None
        return _loads(row[0], None)

    def clear_action(self, user_id: str, chat_id: str | None, kind: str) -> None:
        if not chat_id:
            return
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM assistant_last_actions WHERE user_id=? AND chat_id=? AND kind=?",
                               (self.normalize_user(user_id), str(chat_id)[:120], kind))

    def settings(self, user_id: str) -> dict[str, Any]:
        user_id = self.normalize_user(user_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM proactive_settings WHERE user_id=?", (user_id,)
            ).fetchone()
        if not row:
            return {"user_id": user_id, **self.DEFAULTS}
        result = dict(row)
        for key in (
            "enabled", "reminders", "followups", "weather_warnings",
            "directed_messages", "memory_followups", "allow_nonurgent_interruptions",
            "quiet_hours_configured",
        ):
            result[key] = bool(result[key])
        if not result["quiet_hours_configured"]:
            result["quiet_start"] = ""
            result["quiet_end"] = ""
        return result

    def update_settings(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        user_id = self.normalize_user(user_id)
        values = {**self.DEFAULTS, **self.settings(user_id)}
        boolean_keys = {
            "enabled", "reminders", "followups", "weather_warnings",
            "directed_messages", "memory_followups", "allow_nonurgent_interruptions",
        }
        for key in boolean_keys:
            if key in updates:
                values[key] = bool(updates[key])
        for key in ("quiet_start", "quiet_end"):
            if key in updates:
                candidate = str(updates[key] or "").strip()
                if candidate and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", candidate):
                    raise ValueError(f"{key} must use 24-hour HH:MM format.")
                values[key] = candidate
        values["quiet_hours_configured"] = bool(values["quiet_start"] and values["quiet_end"])
        if "timezone" in updates:
            from services.reminders import valid_zone
            candidate = valid_zone(updates["timezone"])
            if not candidate:
                raise ValueError("Unknown timezone.")
            values["timezone"] = candidate
        now = _iso(_utcnow())
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO proactive_settings (
                    user_id, enabled, reminders, followups, weather_warnings,
                    directed_messages, memory_followups, allow_nonurgent_interruptions,
                    quiet_start, quiet_end, quiet_hours_configured, timezone, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled=excluded.enabled, reminders=excluded.reminders,
                    followups=excluded.followups, weather_warnings=excluded.weather_warnings,
                    directed_messages=excluded.directed_messages,
                    memory_followups=excluded.memory_followups,
                    allow_nonurgent_interruptions=excluded.allow_nonurgent_interruptions,
                    quiet_start=excluded.quiet_start, quiet_end=excluded.quiet_end,
                    quiet_hours_configured=excluded.quiet_hours_configured,
                    timezone=excluded.timezone, updated_at=excluded.updated_at
                """,
                (
                    user_id, *(int(values[key]) for key in (
                        "enabled", "reminders", "followups", "weather_warnings",
                        "directed_messages", "memory_followups", "allow_nonurgent_interruptions",
                    )), values["quiet_start"], values["quiet_end"],
                    int(values["quiet_hours_configured"]), values["timezone"], now,
                ),
            )
        return self.settings(user_id)

    @staticmethod
    def _parse_due(text: str, now: datetime) -> datetime | None:
        relative = re.search(r"(?i)\bin\s+(\d{1,4})\s*(minute|hour|day)s?\b", text)
        if relative:
            amount = int(relative.group(1))
            unit = relative.group(2).casefold()
            return now + {"minute": timedelta(minutes=amount), "hour": timedelta(hours=amount), "day": timedelta(days=amount)}[unit]
        if re.search(r"(?i)\bnext month\b", text):
            year = now.year + (1 if now.month == 12 else 0)
            month = 1 if now.month == 12 else now.month + 1
            return now.replace(year=year, month=month, day=1, hour=9, minute=0, second=0, microsecond=0)
        clock = re.search(r"(?i)\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text)
        if "tomorrow" in text.casefold():
            target = now + timedelta(days=1)
            hour, minute = 9, 0
            if clock:
                hour, minute = int(clock.group(1)), int(clock.group(2) or 0)
                if clock.group(3):
                    hour = hour % 12 + (12 if clock.group(3).casefold() == "pm" else 0)
            try:
                return target.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except ValueError:
                return None
        iso_date = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?\b", text)
        if iso_date:
            try:
                return datetime(
                    int(iso_date.group(1)), int(iso_date.group(2)), int(iso_date.group(3)),
                    int(iso_date.group(4) or 9), int(iso_date.group(5) or 0), tzinfo=now.tzinfo,
                )
            except ValueError:
                return None
        return None

    def _semantic_once(self, user_id, purpose, payload, schema, instruction, *, difficult=False):
        """Per-user persistent dedupe + 20/day + 90-second spacing; no raw text cache."""
        now = _utcnow()
        identity = dict(payload)
        if purpose == "event_date":
            identity["now"] = str(identity.get("now", ""))[:10]
        key = hashlib.sha256(json.dumps([purpose, identity], sort_keys=True).encode()).hexdigest()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT result_json FROM initiative_reasoning WHERE user_id=? AND cache_key=?", (user_id, key)).fetchone()
            if row:
                return _loads(row[0], None)
            budget = connection.execute(
                "SELECT COUNT(*), MAX(created_at) FROM initiative_reasoning WHERE user_id=? AND created_at>=?",
                (user_id, _iso(now - timedelta(days=1))),
            ).fetchone()
            if budget[0] >= 20 or budget[1] and budget[1] > _iso(now - timedelta(seconds=90)):
                return None
            connection.execute("INSERT INTO initiative_reasoning VALUES (?, ?, ?, NULL)", (user_id, key, _iso(now)))
        from services.background_reasoning import semantic_json
        try:
            result = (self.semantic or semantic_json)(user_id, instruction, payload, schema, difficult=difficult)
        except Exception:
            result = None
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE initiative_reasoning SET result_json=? WHERE user_id=? AND cache_key=?",
                               (json.dumps(result) if isinstance(result, dict) else None, user_id, key))
            connection.execute("DELETE FROM initiative_reasoning WHERE created_at<?", (_iso(now - timedelta(days=30)),))
        return result

    def schedule_event(
        self, user_id: str, event_type: str, due_at: datetime, payload: dict[str, Any],
        *, priority: str = "normal", interruption_policy: str = "natural_boundary",
        source_chat_id: str | None = None, source_message_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        user_id = self.normalize_user(user_id)
        event_type = str(event_type).casefold()
        priority = str(priority).casefold()
        if event_type not in EVENT_TYPES or priority not in PRIORITY_ORDER:
            raise ValueError("Unsupported proactive event type or priority.")
        event_id, now = uuid4().hex, _iso(_utcnow())
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO initiative_events (
                    event_id, user_id, event_type, due_at, expires_at, priority,
                    interruption_policy, payload_json, source_chat_id,
                    source_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, user_id, event_type, _iso(due_at), _iso(expires_at) if expires_at else None,
                 priority, interruption_policy, json.dumps(payload, ensure_ascii=False)[:12000],
                 source_chat_id, source_message_id, now, now),
            )
        return {"event_id": event_id, "event_type": event_type, "due_at": _iso(due_at)} if cursor.rowcount else None

    def extract_turn(self, user_id: str, text: str, *, chat_id: str | None, message_id: str | None) -> list[dict[str, Any]]:
        """Prefer deterministic dates; clarify rather than invent an ambiguous date."""
        settings = self.settings(user_id)
        if not settings["enabled"]:
            return []
        now = _utcnow()
        from services.reminders import zone
        local_now = now.astimezone(zone(settings["timezone"]))
        normalized = " ".join(str(text or "").split())
        # Explicit reminders are owned by the foreground personal-assistant handler (services.personal_assistant),
        # which confirms only what it actually scheduled. Creating one here too made silent phantom reminders
        # ("Remind me tomorrow." became a 9 AM reminder while Jupiter was still asking what it was for).
        if re.search(r"(?i)\bremind\s+me\b|\breminders?\b", normalized):
            return []
        due = self._parse_due(text, local_now)
        event_type = ""
        if re.search(r"(?i)\bdeadline\b", normalized):
            event_type = "deadline"
        elif re.search(r"(?i)\bbirthday\b", normalized):
            event_type = "birthday"
        elif re.search(r"(?i)\bmeeting\b", normalized):
            event_type = "meeting"
        elif re.search(r"(?i)\bappointment\b", normalized):
            event_type = "appointment"
        elif re.search(r"(?i)\btrip\b", normalized):
            event_type = "trip"
        elif re.search(r"(?i)\b(?:plan(?:ning)?|intend(?:ing)?|hope)\s+to\s+(?:buy|purchase|get)\b", normalized):
            event_type = "upcoming_event"
        elif re.search(r"(?i)\b(outdoor|outside|picnic|hike|shopping)\b", normalized):
            event_type = "outdoor_plan"
        if event_type and event_type != "reminder" and not settings["reminders"]:
            return []
        if event_type and due is None:
            result = self._semantic_once(user_id, "event_date", {
                "text": normalized[:1500], "now": local_now.isoformat(), "timezone": settings["timezone"],
            }, {"type": "object", "properties": {
                "due_at": {"type": ["string", "null"]}, "confidence": {"type": "number"},
                "ambiguous": {"type": "boolean"}}, "required": ["due_at", "confidence", "ambiguous"]},
                "Resolve only an explicitly requested event date. Do not invent a time or resolve multiple plausible dates. "
                "Use an ISO timestamp with timezone; return ambiguous=true if clarification is needed.")
            if isinstance(result, dict) and result.get("ambiguous") is False:
                try:
                    parsed = datetime.fromisoformat(str(result["due_at"]).replace("Z", "+00:00"))
                    if parsed.tzinfo and float(result.get("confidence", 0)) >= .85 and now < parsed <= now + timedelta(days=730):
                        due = parsed
                except (KeyError, ValueError, TypeError):
                    pass
        if not event_type or due is None or due <= now:
            return []
        priority = "important" if event_type in {"deadline", "appointment", "meeting"} else "normal"
        location_match = re.search(
            r"(?i)\b(?:at|near|in)\s+([A-Za-z][A-Za-z .'-]{1,80}?)(?=\s+(?:tomorrow|today|at|in|on)\b|[,.!?]|$)",
            normalized,
        )
        payload = {"summary": normalized[:1000]}
        if event_type == "outdoor_plan" and location_match:
            payload["location"] = " ".join(location_match.group(1).split())
        created = self.schedule_event(
            user_id, event_type, due, payload, priority=priority,
            source_chat_id=chat_id, source_message_id=message_id,
            expires_at=due + timedelta(days=2),
        )
        created_items = [created] if created else []
        if event_type == "meeting" and settings["followups"]:
            follow_up = self.schedule_event(
                user_id,
                "meeting_follow_up",
                due + timedelta(minutes=90),
                {"summary": "How did the meeting go? I can help capture decisions or next steps."},
                priority="low",
                source_chat_id=chat_id,
                source_message_id=message_id,
                expires_at=due + timedelta(days=2),
            )
            if follow_up:
                created_items.append(follow_up)
        elif event_type == "trip" and settings["followups"] and settings["memory_followups"]:
            follow_up = self.schedule_event(
                user_id,
                "follow_up",
                max(now, due - timedelta(days=3)),
                {
                    "summary": "Your trip is getting close — is the plan still on?",
                    "memory_update_expected": True,
                },
                priority="low",
                source_chat_id=chat_id,
                source_message_id=f"{message_id}:trip-follow-up" if message_id else None,
                expires_at=due + timedelta(days=3),
            )
            if follow_up:
                created_items.append(follow_up)
        elif event_type == "upcoming_event" and settings["followups"] and settings["memory_followups"]:
            follow_up = self.schedule_event(
                user_id,
                "memory_check_in",
                due,
                {
                    "summary": f"Are you still planning this: {normalized[:700]}",
                    "memory_update_expected": True,
                },
                priority="low",
                source_chat_id=chat_id,
                source_message_id=f"{message_id}:plan-follow-up" if message_id else None,
                expires_at=due + timedelta(days=14),
            )
            if follow_up:
                created_items.append(follow_up)
        return created_items

    def send_directed(
        self, sender_user_id: str, recipient_user_id: str, sender_display_name: str,
        body: str, *, deliver_after: datetime | None = None, expires_at: datetime | None = None,
        priority: str = "normal", source_message_id: str | None = None,
    ) -> dict[str, Any]:
        sender, recipient = self.normalize_user(sender_user_id), self.normalize_user(recipient_user_id)
        body = " ".join(str(body or "").split())
        if not sender or not recipient or sender == recipient:
            raise ValueError("A different registered recipient is required.")
        if not body:
            raise ValueError("A message is required.")
        if priority not in PRIORITY_ORDER:
            raise ValueError("Unsupported priority.")
        message_id, now = uuid4().hex, _utcnow()
        deliver_after = deliver_after or now
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO directed_messages (
                    message_id, sender_user_id, recipient_user_id, sender_display_name,
                    message_body, deliver_after, expires_at, priority, source_message_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, sender, recipient, str(sender_display_name or sender)[:120], body[:4000],
                 _iso(deliver_after), _iso(expires_at) if expires_at else None, priority,
                 source_message_id, _iso(now), _iso(now)),
            )
        return {"message_id": message_id, "status": "pending", "recipient": recipient}

    def sent_messages(self, sender_user_id: str) -> list[dict[str, Any]]:
        sender = self.normalize_user(sender_user_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT message_id, recipient_user_id, message_body, deliver_after,
                          expires_at, priority, status, created_at, updated_at
                   FROM directed_messages WHERE sender_user_id=? ORDER BY created_at DESC LIMIT 100""",
                (sender,),
            ).fetchall()
        return [dict(row) for row in rows]

    def edit_directed(self, sender_user_id: str, message_id: str, *, body: str | None = None, cancel: bool = False) -> bool:
        sender = self.normalize_user(sender_user_id)
        with closing(self._connect()) as connection, connection:
            if cancel:
                cursor = connection.execute(
                    "UPDATE directed_messages SET status='cancelled', updated_at=? "
                    "WHERE message_id=? AND sender_user_id=? AND status='pending'",
                    (_iso(_utcnow()), message_id, sender),
                )
            else:
                body = " ".join(str(body or "").split())
                if not body:
                    raise ValueError("A message is required.")
                cursor = connection.execute(
                    "UPDATE directed_messages SET message_body=?, updated_at=? "
                    "WHERE message_id=? AND sender_user_id=? AND status='pending'",
                    (body[:4000], _iso(_utcnow()), message_id, sender),
                )
        return bool(cursor.rowcount)

    @staticmethod
    def _in_quiet_hours(settings: dict[str, Any], now: datetime) -> bool:
        if not settings.get("quiet_hours_configured"):
            return False
        try:
            from services.reminders import zone
            local = now.astimezone(zone(str(settings["timezone"])))
            start_h, start_m = map(int, str(settings["quiet_start"]).split(":"))
            end_h, end_m = map(int, str(settings["quiet_end"]).split(":"))
            minute = local.hour * 60 + local.minute
            start, end = start_h * 60 + start_m, end_h * 60 + end_m
            return (minute >= start or minute < end) if start > end else start <= minute < end
        except (ValueError, ZoneInfoNotFoundError):
            return False

    def inbox(self, user_id: str, *, conversation_busy: bool = False,
              conversation_importance: str = "NORMAL", mid_answer: bool = False,
              topic_boundary: bool = False, native_reminders: bool = False) -> list[dict[str, Any]]:
        user_id, now = self.normalize_user(user_id), _utcnow()
        settings = self.settings(user_id)
        explicit_only = not settings["enabled"]
        quiet = self._in_quiet_hours(settings, now)
        try:
            from services.reminders import ReminderService
            reminders = ReminderService(self)
            reminders.roll_forward_missed(user_id, now)
        except Exception:
            reminders = None
        due_iso = _iso(now)
        candidates: list[dict[str, Any]] = []
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE initiative_events SET status='expired', updated_at=? "
                "WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<?",
                (due_iso, due_iso),
            )
            connection.execute(
                "UPDATE directed_messages SET status='expired', updated_at=? "
                "WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<?",
                (due_iso, due_iso),
            )
            for row in connection.execute(
                """SELECT * FROM initiative_events WHERE user_id=? AND status='pending'
                   AND due_at<=? AND (snoozed_until IS NULL OR snoozed_until<=?)
                   ORDER BY due_at LIMIT 20""", (user_id, due_iso, due_iso)
            ).fetchall():
                item = dict(row)
                payload = _loads(item.pop("payload_json", "{}"), {})
                explicit = (item.get("event_type") == "reminder" and (
                    payload.get("explicit") or item.get("interruption_policy") == "explicit_reminder")) or (
                    item.get("event_type") == "task_status")         # the user asked to be told; still waits
                    # for an answer in progress and (unless they opted out) for quiet hours to end — see below
                if explicit_only and not explicit:
                    continue
                candidates.append({**item, "kind": "initiative", "payload": payload, "explicit": bool(explicit)})
            if settings["directed_messages"] and not explicit_only:
                for row in connection.execute(
                    """SELECT * FROM directed_messages WHERE recipient_user_id=?
                       AND status='pending' AND deliver_after<=? ORDER BY deliver_after LIMIT 20""",
                    (user_id, due_iso),
                ).fetchall():
                    item = dict(row)
                    item["event_id"] = item["message_id"]
                    item["event_type"] = "directed_message"
                    item["kind"] = "directed_message"
                    item["payload"] = {
                        "summary": f"{item['sender_display_name']} asked me to tell you: {item['message_body']}",
                        "sender": item["sender_display_name"],
                        "follow_up_policy": "optional_natural_if_useful",
                    }
                    candidates.append(item)
            recent = connection.execute(
                "SELECT 1 FROM directed_messages WHERE recipient_user_id=? AND status='delivered' AND updated_at>? "
                "UNION ALL SELECT 1 FROM initiative_events WHERE user_id=? AND status='notified' AND updated_at>? LIMIT 1",
                (user_id, _iso(now - timedelta(seconds=90)), user_id, _iso(now - timedelta(seconds=90))),
            ).fetchone() is not None
        from services.delivery_policy import decide_delivery
        candidates.sort(key=lambda item: (PRIORITY_ORDER.get(item.get("priority"), 2), item.get("due_at") or item.get("deliver_after") or ""))
        delivered: list[dict[str, Any]] = []
        for item in candidates:
            if delivered and not item.get("explicit"):
                continue                      # one Jupiter-initiated interruption per poll; every due reminder fires
            if (native_reminders and item.get("explicit") and item.get("event_type") == "reminder"
                    and (item.get("snoozed_until") or item.get("due_at") or "") > _iso(now - timedelta(minutes=15))):
                continue                      # the device shows it natively; the in-app card is only the fallback
            soon = bool(item.get("expires_at") and item["expires_at"] <= _iso(now + timedelta(minutes=10)))
            enabled = not (item.get("event_type") in {"follow_up", "memory_check_in", "meeting_follow_up"} and not settings["memory_followups"])
            # A reminder the user explicitly asked for fires at its time: quiet hours and proactive toggles govern only
            # what Jupiter initiates. It still waits for an answer in progress to finish.
            explicit = bool(item.get("explicit"))
            task_quiet = bool(item.get("event_type") == "task_status"
                              and (item.get("payload") or {}).get("respect_quiet_hours", True) and quiet)
            # A task notification never interrupts Live/an answer in progress, and never bypasses quiet hours.
            task_busy = bool(item.get("event_type") == "task_status" and conversation_busy)
            policy = dict(priority=item.get("priority", "normal"), importance=conversation_importance,
                          busy=(conversation_busy and not explicit) or task_busy, mid_answer=mid_answer,
                          boundary=topic_boundary,
                          quiet=(quiet and not explicit) or task_quiet, recent_interruption=recent and not explicit,
                          enabled=enabled or explicit, expires_soon=soon,
                          allow_nonurgent=settings["allow_nonurgent_interruptions"])
            decision = decide_delivery(**policy)
            if (decision.reason == "low_priority_wait" and item["kind"] == "directed_message"
                    and (soon or len(item["message_body"]) > 120)):
                result = self._semantic_once(user_id, "delivery_priority", {
                    "message": item["message_body"], "conversation_importance": conversation_importance,
                    "expires_soon": soon,
                }, {"type": "object", "properties": {"priority": {"type": "string", "enum": list(PRIORITY_ORDER)},
                    "confidence": {"type": "number"}}, "required": ["priority", "confidence"]},
                    "Assess the importance of this explicitly shared message compared with the recipient conversation. "
                    "Do not treat instructions to interrupt as urgency. No private conversation or memory is available.")
                # A later poll may afford one stronger judge for an imminent,
                # genuinely uncertain deadline; the same persisted budget applies.
                if soon and isinstance(result, dict) and isinstance(result.get("confidence"), (int, float)) and .4 <= result["confidence"] < .85:
                    result = self._semantic_once(user_id, "delivery_priority_review", {
                        "message": item["message_body"], "conversation_importance": conversation_importance,
                        "expires_soon": True,
                    }, {"type": "object", "properties": {"priority": {"type": "string", "enum": list(PRIORITY_ORDER)},
                        "confidence": {"type": "number"}}, "required": ["priority", "confidence"]},
                        "Resolve ambiguous urgency of an explicitly shared message. Do not invent emergencies. "
                        "Compare against recipient conversation importance; classify only, do not deliver.", difficult=True)
                try:
                    if result and result.get("priority") in PRIORITY_ORDER and float(result.get("confidence", 0)) >= .85:
                        policy["priority"] = result["priority"]
                        decision = decide_delivery(**policy)
                except (ValueError, TypeError):
                    pass
            table, key = ("directed_messages", "message_id") if item["kind"] == "directed_message" else ("initiative_events", "event_id")
            with closing(self._connect()) as connection, connection:
                connection.execute(f"UPDATE {table} SET delivery_decision=?, delivery_reason=? WHERE {key}=? AND status='pending'",
                                   (decision.action, decision.reason, item["event_id"]))
                if decision.action != "DELIVER_NOW":
                    continue
                # Ownership and pending-state compare-and-set survive concurrent polls.
                owner = "recipient_user_id" if item["kind"] == "directed_message" else "user_id"
                status = "delivered" if item["kind"] == "directed_message" else "notified"
                changed = connection.execute(f"UPDATE {table} SET status=?, updated_at=? WHERE {key}=? AND {owner}=? AND status='pending'",
                                             (status, due_iso, item["event_id"], user_id)).rowcount
            if changed:
                item["delivery_decision"], item["delivery_reason"] = decision.action, decision.reason
                if reminders is not None and item.get("event_type") == "reminder" and item.get("series_id"):
                    try:
                        reminders.schedule_next(user_id, {**item, "recurrence": _loads(item.get("recurrence_json"), None)},
                                                after=now)
                    except Exception:
                        pass
                delivered.append(item)
                if item.get("event_type") == "task_status":
                    # The app-channel delivery record follows the inbox's compare-and-set: accepted -> delivered.
                    try:
                        with closing(self._connect()) as connection, connection:
                            connection.execute(
                                "UPDATE task_notification_deliveries SET status='delivered', updated_at=? "
                                "WHERE inbox_event_id=? AND owner=? AND status='accepted'",
                                (due_iso, item["event_id"], user_id))
                    except sqlite3.OperationalError:
                        pass                                 # no task store on this database
                if not item.get("explicit"):
                    break
        return delivered

    def mark_delivered(self, user_id: str, event_id: str) -> bool:
        """Compare-and-set pending -> notified for a reminder a device displayed natively (exactly once)."""
        now = _iso(_utcnow())
        with closing(self._connect()) as connection, connection:
            return bool(connection.execute(
                "UPDATE initiative_events SET status='notified', delivery_decision='DELIVER_NOW', "
                "delivery_reason='device_notification', updated_at=? WHERE event_id=? AND user_id=? AND status='pending'",
                (now, event_id, self.normalize_user(user_id)),
            ).rowcount)

    def act_on_event(self, user_id: str, event_id: str, action: str, snooze_minutes: int = 30) -> bool:
        user_id, action = self.normalize_user(user_id), str(action).casefold()
        now = _utcnow()
        if action not in {"dismiss", "complete", "snooze"}:
            raise ValueError("Unsupported proactive action.")
        status, snoozed_until = ("pending", _iso(now + timedelta(minutes=max(1, min(snooze_minutes, 10080))))) if action == "snooze" else ("dismissed" if action == "dismiss" else "completed", None)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE initiative_events SET status=?, snoozed_until=?, updated_at=? "
                "WHERE event_id=? AND user_id=? AND status IN ('pending','notified')",
                (status, snoozed_until, _iso(now), event_id, user_id),
            )
        return bool(cursor.rowcount)

    def share_fact(
        self, source_user_id: str, subject_label: str, fact_text: str, visibility_scope: str,
        allowed_user_ids: list[str], *, allow_source_attribution: bool = False,
        provenance: dict[str, Any] | None = None, sensitivity: str = "normal",
        subject_user_id: str | None = None, replace_existing: bool = False,
    ) -> dict[str, Any]:
        source = self.normalize_user(source_user_id)
        scope = str(visibility_scope or "PRIVATE").upper()
        sensitivity = str(sensitivity or "normal").casefold()
        provenance = dict(provenance or {})
        source_type = str(provenance.get("source_type") or "").casefold()
        sensitive_content = bool(re.search(
            r"(?i)\b(password|passcode|api[ -]?key|oauth|access token|refresh token|"
            r"bank account|card number|medical|diagnosis|prescription|gmail message|email body|"
            r"face embedding|biometric)\b",
            str(fact_text or ""),
        ))
        if scope not in VISIBILITY_SCOPES:
            raise ValueError("Unsupported visibility scope.")
        if scope != "PRIVATE" and (
            sensitivity != "normal" or source_type in SENSITIVE_SOURCES or sensitive_content
        ):
            raise ValueError("Sensitive or private-source memories cannot be shared across users.")
        allowed = sorted({self.normalize_user(item) for item in allowed_user_ids if self.normalize_user(item) != source})
        if scope == "SHARE_WITH_USERS" and not allowed:
            raise ValueError("At least one allowed registered user is required.")
        fact_id, now = uuid4().hex, _iso(_utcnow())
        subject = self.normalize_user(subject_user_id) if subject_user_id else None
        with closing(self._connect()) as connection, connection:
            if replace_existing:
                # Re-sharing the same person's context with the same people updates it instead of duplicating it.
                for row in connection.execute(
                    "SELECT fact_id, subject_label, allowed_user_ids_json FROM shared_people_facts WHERE source_user_id=?",
                    (source,),
                ).fetchall():
                    if (str(row[1]).casefold() == str(subject_label).casefold()
                            and sorted(_loads(row[2], [])) == allowed):
                        connection.execute("DELETE FROM shared_people_facts WHERE fact_id=? AND source_user_id=?",
                                           (row[0], source))
            connection.execute(
                """INSERT INTO shared_people_facts (
                    fact_id, source_user_id, subject_label, fact_text, visibility_scope,
                    allowed_user_ids_json, allow_source_attribution, provenance_json,
                    sensitivity, created_at, updated_at, subject_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fact_id, source, str(subject_label)[:160], str(fact_text)[:6000], scope,
                 json.dumps(allowed), int(allow_source_attribution), json.dumps(provenance)[:3000],
                 sensitivity, now, now, subject),
            )
        return {"fact_id": fact_id, "visibility_scope": scope}

    def unshare(self, source_user_id: str, *, subject_label: str | None = None,
                recipient_user_id: str | None = None) -> int:
        """Remove this owner's shared facts, optionally only about one subject and/or only for one recipient."""
        source = self.normalize_user(source_user_id)
        recipient = self.normalize_user(recipient_user_id) if recipient_user_id else None
        removed = 0
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT fact_id, subject_label, allowed_user_ids_json, visibility_scope FROM shared_people_facts "
                "WHERE source_user_id=?", (source,),
            ).fetchall()
            for fact_id, label, allowed_json, scope in rows:
                if subject_label and not self._same_subject(label, subject_label):
                    continue
                allowed = [user for user in _loads(allowed_json, []) if user]
                if recipient and scope == "SHARE_WITH_USERS":
                    if recipient not in allowed:
                        continue
                    remaining = [user for user in allowed if user != recipient]
                    if remaining:
                        connection.execute("UPDATE shared_people_facts SET allowed_user_ids_json=?, updated_at=? WHERE fact_id=?",
                                           (json.dumps(sorted(remaining)), _iso(_utcnow()), fact_id))
                        removed += 1
                        continue
                connection.execute("DELETE FROM shared_people_facts WHERE fact_id=? AND source_user_id=?", (fact_id, source))
                removed += 1
        return removed

    @staticmethod
    def _same_subject(stored: str, wanted: str) -> bool:
        left, right = " ".join(str(stored).casefold().split()), " ".join(str(wanted).casefold().split())
        return bool(left and right) and (left == right or left.split()[0] == right.split()[0] and
                                          (left.startswith(right) or right.startswith(left)))

    def delete_shared_facts_about(self, source_user_id: str, subject_label: str) -> int:
        return self.unshare(source_user_id, subject_label=subject_label)

    def shared_facts_for(self, viewer_user_id: str) -> list[dict[str, Any]]:
        viewer = self.normalize_user(viewer_user_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM shared_people_facts WHERE source_user_id<>? AND visibility_scope<>'PRIVATE'",
                (viewer,),
            ).fetchall()
        visible = []
        for row in rows:
            item = dict(row)
            allowed = set(_loads(item.pop("allowed_user_ids_json", "[]"), []))
            provenance = _loads(item.get("provenance_json", "{}"), {})
            if item["visibility_scope"] == "SHARE_WITH_USERS" and viewer not in allowed:
                continue
            if item["sensitivity"] != "normal" or str(provenance.get("source_type") or "").casefold() in SENSITIVE_SOURCES:
                continue
            subject_user = item.get("subject_user_id") or None
            visible.append({
                "fact_id": item["fact_id"],
                "subject": item["subject_label"],
                "fact": item["fact_text"],
                "source_user": item["source_user_id"] if item["allow_source_attribution"] else None,
                "attribution_allowed": bool(item["allow_source_attribution"]),
                "visibility_scope": item["visibility_scope"],
                # True only when the fact was shared *about this very account* (a stable account link, never a
                # first-name match): the precondition for "I've heard about you from ...".
                "about_viewer": bool(subject_user and subject_user == viewer),
                "shared_at": item.get("updated_at") or item.get("created_at"),
            })
        return visible[:50]

    def own_shared_facts(self, source_user_id: str) -> list[dict[str, Any]]:
        source = self.normalize_user(source_user_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT fact_id, subject_label, fact_text, visibility_scope,
                          allowed_user_ids_json, allow_source_attribution,
                          sensitivity, created_at, updated_at, subject_user_id
                   FROM shared_people_facts WHERE source_user_id=? ORDER BY updated_at DESC""",
                (source,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["allowed_users"] = _loads(item.pop("allowed_user_ids_json", "[]"), [])
            item["allow_source_attribution"] = bool(item["allow_source_attribution"])
            result.append(item)
        return result

    def delete_shared_fact(self, source_user_id: str, fact_id: str) -> bool:
        source = self.normalize_user(source_user_id)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM shared_people_facts WHERE fact_id=? AND source_user_id=?",
                (fact_id, source),
            )
        return bool(cursor.rowcount)
