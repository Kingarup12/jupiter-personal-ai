import json
import sqlite3
from pathlib import Path
from datetime import datetime
from uuid import uuid4

from config.settings import USERS_DIR, _safe_user_id

from memory.schema import Memory
from memory.fact import Fact
from memory.types import add_calendar_months
from services.interactive_response import sanitize_interactive_metadata


class SQLiteStore:

    def __init__(self, database_path=None, user_id="default", read_only=False):
        self.user_id = str(user_id or "default")
        if database_path:
            db_path = Path(database_path)
        else:
            user_directory = USERS_DIR / _safe_user_id(self.user_id)
            user_directory.mkdir(parents=True, exist_ok=True)
            db_path = user_directory / "memories.db"

        self.read_only = bool(read_only)
        resolved = Path(db_path).resolve()
        self.database_path = resolved
        if self.read_only and resolved.is_file():
            self.connection = sqlite3.connect(
                f"file:{resolved.as_posix()}?mode=ro", uri=True
            )
        else:
            # A private/no-save conversation for a brand-new user receives an
            # ephemeral empty store rather than creating persistent user data.
            self.connection = sqlite3.connect(":memory:" if self.read_only else resolved)
        self.connection.row_factory = sqlite3.Row
        self.cursor = self.connection.cursor()

        if not self.read_only or not resolved.is_file():
            self.create_tables()

    # ---------------------------------
    # Create Tables
    # ---------------------------------

    def create_tables(self):

        # -----------------------------
        # Memories
        # -----------------------------

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS memories(

            memory_id TEXT NOT NULL,

            category TEXT,
            topic TEXT,
            summary TEXT,

            facts TEXT,
            entities TEXT,
            attributes TEXT,
            certainty REAL,

            importance TEXT,
            retention TEXT,
            visibility TEXT,

            confidence REAL,
            source TEXT,

            usage_count INTEGER,
            last_accessed TEXT,

            created_at TEXT,
            last_updated TEXT,

            memory_type TEXT NOT NULL DEFAULT 'SEMANTIC',
            domain TEXT NOT NULL DEFAULT 'GENERAL',
            emotion TEXT DEFAULT 'neutral',
            expires_at TEXT,
            archived_at TEXT,
            superseded_by TEXT,
            archive_reason TEXT,
            user_id TEXT NOT NULL DEFAULT 'default',
            PRIMARY KEY (user_id, memory_id)

        )
        """)

        # -----------------------------
        # Facts
        # -----------------------------

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS facts(

            fact_id TEXT PRIMARY KEY,

            memory_id TEXT,

            subject TEXT,
            predicate TEXT,
            object TEXT,

            confidence REAL,
            source TEXT,

            created_at TEXT,
            last_updated TEXT,
            user_id TEXT NOT NULL DEFAULT 'default'
        )
        """)

        # Companion-owned operational state is kept separate from user facts.
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS companion_state(
            user_id TEXT NOT NULL,
            state_key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, state_key)
        )
        """)

        # -----------------------------
        # Chats and Messages
        # -----------------------------

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats(
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            title TEXT,
            updated_at TEXT,
            pinned INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id)
        )
        """)

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            message_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            sender TEXT,
            text TEXT,
            timestamp TEXT,
            model_used TEXT,
            attachments_json TEXT NOT NULL DEFAULT '[]',
            capability_metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (user_id, chat_id, message_id)
        )
        """)

        # Deletion tombstones are authoritative.  An offline client may still
        # upload an old local copy, but that chat ID must never be recreated.
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS deleted_chats(
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            purge_at TEXT,
            title TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            messages_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (user_id, chat_id)
        )
        """)
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_deletion_tombstones(
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            PRIMARY KEY (user_id, chat_id)
        )
        """)

        # A durable memory can be supported by more than one conversation.
        # Keep those links separate from the legacy single ``source`` field so
        # removing one chat does not remove a still-supported memory.
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS memory_sources(
            memory_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, memory_id, chat_id, message_id)
        )
        """)

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS internal_notes(
            note_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            note_type TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT
        )
        """)

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS episodes(
            episode_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            topic TEXT,
            emotion TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL
        )
        """)

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS monthly_summaries(
            summary_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            month_key TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, month_key)
        )
        """)

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS memory_review_queue(
            review_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            source_chat_id TEXT,
            source_message_id TEXT,
            candidate_fact TEXT NOT NULL,
            candidate_json TEXT NOT NULL,
            proposed_tier TEXT,
            confidence REAL NOT NULL,
            confidence_state TEXT NOT NULL,
            reason_for_review TEXT NOT NULL,
            relevant_existing_memory_ids TEXT NOT NULL DEFAULT '[]',
            contradiction INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            selected_tier TEXT,
            custom_comment TEXT,
            resolved_at TEXT
        )
        """)
        self.cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_review_source
        ON memory_review_queue(user_id, source_message_id)
        WHERE source_message_id IS NOT NULL
        """)
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS memory_review_feedback(
            feedback_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            review_id TEXT NOT NULL,
            candidate_fact TEXT NOT NULL,
            proposed_tier TEXT,
            selected_tier TEXT NOT NULL,
            custom_comment TEXT,
            duration_hint TEXT,
            review_after TEXT,
            promotion_hint TEXT,
            demotion_hint TEXT,
            explicit_exception INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(review_id) REFERENCES memory_review_queue(review_id)
        )
        """)

        self._migrate_memory_schema()
        self._migrate_memory_primary_key()
        self._migrate_fact_schema()
        self._migrate_message_schema()
        self._migrate_deleted_chat_schema()
        self._backfill_memory_sources()
        self._create_indexes()
        self.connection.commit()

    # ---------------------------------
    # Schema Migration
    # ---------------------------------

    def _backfill_memory_sources(self):
        """Backfill only legacy sources whose chat/message IDs are explicit."""

        rows = self.cursor.execute(
            "SELECT memory_id, source, created_at FROM memories WHERE user_id=?",
            (self.user_id,),
        ).fetchall()
        for row in rows:
            source = str(row["source"] or "")
            if not source.startswith("conversation:"):
                continue
            parts = source.split(":", 2)
            if len(parts) != 3 or not parts[1] or not parts[2] or "unknown" in parts[1:]:
                continue
            self.cursor.execute(
                """
                INSERT OR IGNORE INTO memory_sources (
                    memory_id, user_id, chat_id, message_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (row["memory_id"], self.user_id, parts[1], parts[2], row["created_at"] or datetime.now().isoformat()),
            )

    def _migrate_message_schema(self):
        existing_columns = {
            row["name"] for row in self.cursor.execute("PRAGMA table_info(messages)")
        }
        additions = {
            "attachments_json": "TEXT NOT NULL DEFAULT '[]'",
            "capability_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in existing_columns:
                self.cursor.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")

    def _migrate_deleted_chat_schema(self):
        existing_columns = {
            row["name"] for row in self.cursor.execute("PRAGMA table_info(deleted_chats)")
        }
        additions = {
            "purge_at": "TEXT",
            "title": "TEXT",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "messages_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in additions.items():
            if name not in existing_columns:
                self.cursor.execute(f"ALTER TABLE deleted_chats ADD COLUMN {name} {definition}")
        self.cursor.execute(
            """
            INSERT OR IGNORE INTO chat_deletion_tombstones (chat_id, user_id, deleted_at)
            SELECT chat_id, user_id, deleted_at FROM deleted_chats
            """
        )

    def _migrate_memory_schema(self):
        """Add lifecycle columns when opening a database from an older build."""

        existing_columns = {
            row["name"]
            for row in self.cursor.execute("PRAGMA table_info(memories)")
        }

        additions = {
            "memory_type": "TEXT NOT NULL DEFAULT 'SEMANTIC'",
            "domain": "TEXT NOT NULL DEFAULT 'GENERAL'",
            "emotion": "TEXT DEFAULT 'neutral'",
            "expires_at": "TEXT",
            "archived_at": "TEXT",
            "superseded_by": "TEXT",
            "archive_reason": "TEXT",
            "user_id": "TEXT NOT NULL DEFAULT 'default'",
        }

        for name, definition in additions.items():
            if name not in existing_columns:
                self.cursor.execute(
                    f"ALTER TABLE memories ADD COLUMN {name} {definition}"
                )

        self.cursor.execute(
            "UPDATE memories SET user_id=? WHERE user_id IS NULL",
            (self.user_id,),
        )

    def _migrate_memory_primary_key(self):
        """Add user-scoped uniqueness without rebuilding or dropping legacy data.

        SQLite cannot change an existing primary key in place. The previous
        implementation rebuilt and dropped the production table during normal
        startup. A unique index provides the required invariant additively;
        PostgreSQL migration handles the eventual authoritative schema.
        """

        self.cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_user_memory
            ON memories(user_id, memory_id)
            """
        )

    def _migrate_fact_schema(self):
        """Scope fact triples by user just like their source memories."""

        existing_columns = {
            row["name"] for row in self.cursor.execute("PRAGMA table_info(facts)")
        }
        if "user_id" not in existing_columns:
            self.cursor.execute(
                "ALTER TABLE facts ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'"
            )
        self.cursor.execute(
            "UPDATE facts SET user_id=? WHERE user_id IS NULL",
            (self.user_id,),
        )

    def _create_indexes(self):
        self.cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_user_active
            ON memories(user_id, archived_at, expires_at)
            """
        )
        self.cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_facts_user_memory
            ON facts(user_id, memory_id)
            """
        )

    # ---------------------------------
    # Save Memory
    # ---------------------------------

    def save(self, memory, *, commit=True):

        self.cursor.execute("""
        INSERT INTO memories (
            memory_id, category, topic, summary, facts, entities, attributes,
            certainty, importance, retention, visibility, confidence, source,
            usage_count, last_accessed, created_at, last_updated, memory_type,
            domain, emotion, expires_at, archived_at, superseded_by, archive_reason, user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO UPDATE SET
            category=excluded.category,
            topic=excluded.topic,
            summary=excluded.summary,
            facts=excluded.facts,
            entities=excluded.entities,
            attributes=excluded.attributes,
            certainty=excluded.certainty,
            importance=excluded.importance,
            retention=excluded.retention,
            visibility=excluded.visibility,
            confidence=excluded.confidence,
            source=excluded.source,
            usage_count=excluded.usage_count,
            last_accessed=excluded.last_accessed,
            last_updated=excluded.last_updated,
            memory_type=excluded.memory_type,
            domain=excluded.domain,
            emotion=excluded.emotion,
            expires_at=excluded.expires_at,
            archived_at=excluded.archived_at,
            superseded_by=excluded.superseded_by,
            archive_reason=excluded.archive_reason
        """, (

            memory.memory_id,

            memory.category,
            memory.topic,
            memory.summary,

            json.dumps(memory.facts),
            json.dumps(memory.entities),
            json.dumps(memory.attributes),
            memory.certainty,

            memory.importance,
            memory.retention,
            memory.visibility,

            memory.confidence,
            memory.source,

            memory.usage_count,
            memory.last_accessed,

            memory.created_at,
            memory.last_updated,
            memory.memory_type,
            memory.domain,
            memory.emotion,
            memory.expires_at,
            memory.archived_at,
            memory.superseded_by,
            memory.archive_reason,
            self.user_id

        ))

        if commit:
            self.connection.commit()

    def save_memory_bundle(self, memory, facts, *, supersedes=None):
        """Commit the current fact, triples and provenance together, or nothing."""
        parts = str(memory.source or "").split(":", 2)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if len(parts) == 3 and parts[0] == "conversation" and self.is_chat_deleted(parts[1]):
                raise ValueError("Deleted conversation cannot supply new memory.")
            if supersedes:
                self.archive_memory(supersedes, "superseded_by_newer_user_statement",
                                    memory.memory_id, commit=False)
            self.save(memory, commit=False)
            self.delete_facts(memory.memory_id, commit=False)
            for fact in facts:
                self.save_fact(fact, commit=False)
            if len(parts) == 3 and parts[0] == "conversation" and "unknown" not in parts[1:]:
                self.add_memory_source(memory.memory_id, parts[1], parts[2], commit=False)

    def add_memory_source(self, memory_id, chat_id, message_id, *, commit=True):
        """Record one deterministic conversation source for a durable memory."""

        if not memory_id or not chat_id or not message_id:
            return False
        self.cursor.execute(
            """
            INSERT OR IGNORE INTO memory_sources (
                memory_id, user_id, chat_id, message_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (memory_id, self.user_id, chat_id, message_id, datetime.now().isoformat()),
        )
        if commit:
            self.connection.commit()
        return self.cursor.rowcount > 0

    def memory_source_count(self, memory_id):
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM memory_sources WHERE user_id=? AND memory_id=?",
            (self.user_id, memory_id),
        ).fetchone()
        return int(row["count"] or 0) if row else 0

    def remove_chat_memory_sources(self, chat_id):
        """Remove provenance and return detailed last-source/retained results."""

        deleted_memory_ids = []
        retained_memory_ids = []
        with self.connection:
            memory_ids = [
                row["memory_id"]
                for row in self.connection.execute(
                    "SELECT DISTINCT memory_id FROM memory_sources WHERE user_id=? AND chat_id=?",
                    (self.user_id, chat_id),
                ).fetchall()
            ]
            self.connection.execute(
                "DELETE FROM memory_sources WHERE user_id=? AND chat_id=?",
                (self.user_id, chat_id),
            )
            for memory_id in memory_ids:
                remaining = self.connection.execute(
                    "SELECT chat_id, message_id FROM memory_sources "
                    "WHERE user_id=? AND memory_id=? ORDER BY created_at ASC LIMIT 1",
                    (self.user_id, memory_id),
                ).fetchone()
                if remaining is not None:
                    self.connection.execute(
                        "UPDATE memories SET source=?, last_updated=? WHERE user_id=? AND memory_id=?",
                        (
                            f"conversation:{remaining['chat_id']}:{remaining['message_id']}",
                            datetime.now().isoformat(), self.user_id, memory_id,
                        ),
                    )
                    retained_memory_ids.append(memory_id)
                    continue
                self.connection.execute(
                    "DELETE FROM facts WHERE user_id=? AND memory_id=?",
                    (self.user_id, memory_id),
                )
                result = self.connection.execute(
                    "DELETE FROM memories WHERE user_id=? AND memory_id=?",
                    (self.user_id, memory_id),
                )
                if result.rowcount:
                    deleted_memory_ids.append(memory_id)
        review_cleanup = self.cleanup_chat_memory_reviews(chat_id)
        return {
            "deleted_memory_ids": deleted_memory_ids,
            "retained_memory_ids": retained_memory_ids,
            "review_cleanup": review_cleanup,
        }

    def cleanup_chat_memory_reviews(self, chat_id):
        """Delete pending source-only reviews and redact resolved policy evidence."""

        with self.connection:
            rows = self.connection.execute(
                "SELECT review_id, status FROM memory_review_queue WHERE user_id=? AND source_chat_id=?",
                (self.user_id, chat_id),
            ).fetchall()
            pending_ids = [row["review_id"] for row in rows if row["status"] == "pending"]
            resolved_ids = [row["review_id"] for row in rows if row["status"] != "pending"]
            for review_id in pending_ids:
                self.connection.execute(
                    "DELETE FROM memory_review_feedback WHERE user_id=? AND review_id=?",
                    (self.user_id, review_id),
                )
                self.connection.execute(
                    "DELETE FROM memory_review_queue WHERE user_id=? AND review_id=?",
                    (self.user_id, review_id),
                )
            for review_id in resolved_ids:
                # Retain only the generalized tier-policy choice. Personal
                # candidate content and deleted conversation provenance vanish.
                self.connection.execute(
                    "UPDATE memory_review_queue SET source_chat_id=NULL, source_message_id=NULL, "
                    "candidate_fact='[deleted source]', candidate_json='{}', "
                    "reason_for_review='Source conversation deleted; generalized policy feedback retained.' "
                    "WHERE user_id=? AND review_id=?",
                    (self.user_id, review_id),
                )
                self.connection.execute(
                    "UPDATE memory_review_feedback SET candidate_fact='[deleted source]' "
                    "WHERE user_id=? AND review_id=?",
                    (self.user_id, review_id),
                )
        return {
            "pending_removed": len(pending_ids),
            "resolved_policy_redacted": len(resolved_ids),
        }

    # ---------------------------------
    # Save Fact
    # ---------------------------------

    def save_fact(self, fact, *, commit=True):

        existing = self.connection.execute("SELECT user_id FROM facts WHERE fact_id=?", (fact.fact_id,)).fetchone()
        if existing and existing["user_id"] != self.user_id:
            raise ValueError("Fact belongs to another account.")

        self.cursor.execute("""
        INSERT INTO facts (
            fact_id, memory_id, subject, predicate, object, confidence, source,
            created_at, last_updated, user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fact_id) DO UPDATE SET
            memory_id=excluded.memory_id,
            subject=excluded.subject,
            predicate=excluded.predicate,
            object=excluded.object,
            confidence=excluded.confidence,
            source=excluded.source,
            last_updated=excluded.last_updated,
            user_id=excluded.user_id
        """, (

            fact.fact_id,

            fact.memory_id,

            fact.subject,
            fact.predicate,
            fact.object,

            fact.confidence,
            fact.source,

            fact.created_at,
            fact.last_updated,
            self.user_id

        ))

        if commit:
            self.connection.commit()

    # ---------------------------------
    # Delete Facts
    # ---------------------------------

    def delete_facts(self, memory_id, *, commit=True):

        self.cursor.execute(

            "DELETE FROM facts WHERE memory_id=? AND user_id=?",

            (memory_id, self.user_id)

        )

        if commit:
            self.connection.commit()

    # ---------------------------------
    # Get Facts
    # ---------------------------------

    def get_facts(self, memory_id=None):

        if memory_id is None:

            self.cursor.execute(
                "SELECT * FROM facts WHERE user_id=?",
                (self.user_id,)
            )

        else:

            self.cursor.execute(

                "SELECT * FROM facts WHERE memory_id=? AND user_id=?",

                (memory_id, self.user_id)

            )

        rows = self.cursor.fetchall()

        facts = []

        for row in rows:

            facts.append(

                Fact(

                    fact_id=row["fact_id"],

                    memory_id=row["memory_id"],

                    subject=row["subject"],
                    predicate=row["predicate"],
                    object=row["object"],

                    confidence=row["confidence"],
                    source=row["source"],

                    created_at=row["created_at"],
                    last_updated=row["last_updated"]

                )

            )

        return facts

    # ---------------------------------
    # Convert Row -> Memory
    # ---------------------------------

    def row_to_memory(self, row):

        if row is None:
            return None

        return Memory(

            memory_id=row["memory_id"],

            category=row["category"],
            topic=row["topic"],
            summary=row["summary"],

            memory_type=row["memory_type"],
            domain=row["domain"],
            emotion=row["emotion"] or "neutral",

            facts=json.loads(row["facts"]) if row["facts"] else [],
            entities=json.loads(row["entities"]) if row["entities"] else [],
            attributes=json.loads(row["attributes"]) if row["attributes"] else {},
            certainty=row["certainty"] if row["certainty"] is not None else 1.0,

            importance=row["importance"],
            retention=row["retention"],
            visibility=row["visibility"],

            confidence=row["confidence"],
            source=row["source"],

            usage_count=row["usage_count"],
            last_accessed=row["last_accessed"],

            created_at=row["created_at"],
            last_updated=row["last_updated"],

            expires_at=row["expires_at"],
            archived_at=row["archived_at"],
            superseded_by=row["superseded_by"],
            archive_reason=row["archive_reason"]

        )

    # ---------------------------------
    # Get All Memories
    # ---------------------------------

    def get_all(self, include_archived=False):

        if include_archived:
            self.cursor.execute(
                "SELECT * FROM memories WHERE user_id=?",
                (self.user_id,)
            )
        else:
            self.cursor.execute(
                """
                SELECT * FROM memories
                WHERE user_id=?
                  AND archived_at IS NULL
                  AND (expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime'))
                """,
                (self.user_id,)
            )

        rows = self.cursor.fetchall()

        return [

            self.row_to_memory(row)

            for row in rows

        ]

    # ---------------------------------
    # Get Memory by ID
    # ---------------------------------

    def get_by_id(self, memory_id):

        self.cursor.execute(
            """
            SELECT * FROM memories
            WHERE memory_id=?
              AND user_id=?
              AND archived_at IS NULL
              AND (expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime'))
            """,
            (memory_id, self.user_id)
        )

        row = self.cursor.fetchone()

        return self.row_to_memory(row)

    def find_by_category_topic(self, category, topic):
        """Return active records with the same stable subject before vector search."""

        self.cursor.execute(
            """
            SELECT * FROM memories
            WHERE user_id=?
              AND category=? COLLATE NOCASE
              AND topic=? COLLATE NOCASE
              AND archived_at IS NULL
              AND (expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime'))
            """,
            (self.user_id, category, topic),
        )

        return [self.row_to_memory(row) for row in self.cursor.fetchall()]

    # ---------------------------------
    # Profile and Archive Memory
    # ---------------------------------

    def get_personality_memories(self):

        self.cursor.execute(
            """
            SELECT * FROM memories
            WHERE user_id=?
              AND (memory_type='PERSONALITY' OR domain IN ('PROFILE', 'PREFERENCE'))
              AND archived_at IS NULL
              AND (expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime'))
            ORDER BY importance DESC, last_updated DESC
            """,
            (self.user_id,)
        )

        memories = [self.row_to_memory(row) for row in self.cursor.fetchall()]
        return [
            memory for memory in memories
            if str((memory.attributes or {}).get("memory_tier") or "P0").upper() == "P0"
        ]

    def get_by_tier(self, tier):
        """Return active factual memories for one authoritative P0-P4 tier."""

        normalized = str(tier or "").upper()
        if normalized not in {"P0", "P1", "P2", "P3", "P4"}:
            return []
        rows = self.cursor.execute(
            """
            SELECT * FROM memories
            WHERE user_id=?
              AND archived_at IS NULL
              AND (expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime'))
            ORDER BY importance DESC, last_updated DESC
            """,
            (self.user_id,),
        ).fetchall()
        memories = [self.row_to_memory(row) for row in rows]
        return [
            memory for memory in memories
            if str((memory.attributes or {}).get("memory_tier") or "P1").upper() == normalized
            and str(memory.memory_type or "").upper() != "PERSONALITY"
        ]

    def get_archived(self):

        self.cursor.execute(
            """
            SELECT * FROM memories
            WHERE user_id=? AND archived_at IS NOT NULL
            ORDER BY archived_at DESC
            """,
            (self.user_id,)
        )

        return [self.row_to_memory(row) for row in self.cursor.fetchall()]

    def archive_expired(self):
        """Archive, rather than delete, memories whose lifetime has ended."""

        now = datetime.now().isoformat()

        self.cursor.execute(
            """
            SELECT memory_id FROM memories
            WHERE user_id=?
              AND archived_at IS NULL
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            """,
            (self.user_id, now),
        )
        memory_ids = [row["memory_id"] for row in self.cursor.fetchall()]

        if memory_ids:
            self.cursor.execute(
                """
                UPDATE memories
                SET archived_at=?, last_updated=?
                WHERE user_id=?
                  AND archived_at IS NULL
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                """,
                (now, now, self.user_id, now),
            )
            self.connection.commit()

        return memory_ids

    def archive_memory(self, memory_id, reason=None, superseded_by=None, *, commit=True):
        """Archive one active record while retaining it for history and export."""

        now = datetime.now().isoformat()
        self.cursor.execute(
            """
            UPDATE memories
            SET archived_at=?, last_updated=?,
                archive_reason=COALESCE(?, archive_reason),
                superseded_by=COALESCE(?, superseded_by)
            WHERE memory_id=? AND user_id=? AND archived_at IS NULL
            """,
            (now, now, reason, superseded_by, memory_id, self.user_id),
        )
        if commit:
            self.connection.commit()
        return self.cursor.rowcount > 0

    # ---------------------------------
    # Delete Memory
    # ---------------------------------

    def delete(self, memory_id):

        self.delete_facts(memory_id)

        self.cursor.execute(

            "DELETE FROM memories WHERE memory_id=? AND user_id=?",

            (memory_id, self.user_id)

        )

        self.connection.commit()

    # ---------------------------------
    # Companion state and internal notes
    # ---------------------------------

    def set_state(self, state_key, value):
        now = datetime.now().isoformat()
        self.cursor.execute(
            """
            INSERT INTO companion_state (user_id, state_key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, state_key) DO UPDATE SET
                value=excluded.value, updated_at=excluded.updated_at
            """,
            (self.user_id, state_key, json.dumps(value), now),
        )
        self.connection.commit()

    def get_state(self, state_key, default=None):
        self.cursor.execute(
            "SELECT value FROM companion_state WHERE user_id=? AND state_key=?",
            (self.user_id, state_key),
        )
        row = self.cursor.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    # ---------------------------------
    # User-specific memory Review queue
    # ---------------------------------

    def enqueue_memory_review(self, item):
        if self.read_only:
            return None
        now = str(item.get("created_at") or datetime.now().isoformat())
        review_id = str(item.get("id") or item.get("review_id") or uuid4())
        try:
            self.cursor.execute(
                """
                INSERT INTO memory_review_queue(
                    review_id, user_id, source_chat_id, source_message_id,
                    candidate_fact, candidate_json, proposed_tier, confidence,
                    confidence_state, reason_for_review,
                    relevant_existing_memory_ids, contradiction, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    review_id, self.user_id, item.get("source_chat_id"),
                    item.get("source_message_id"), str(item.get("candidate_fact") or "")[:4000],
                    json.dumps(item.get("candidate") or {}, ensure_ascii=False, separators=(",", ":")),
                    item.get("proposed_tier"), float(item.get("confidence") or 0),
                    str(item.get("confidence_state") or "unsure"),
                    str(item.get("reason_for_review") or "Jupiter needs clarification.")[:1000],
                    json.dumps(item.get("relevant_existing_memory_ids") or []),
                    int(bool(item.get("contradiction"))), now,
                ),
            )
            self.connection.commit()
            return review_id
        except sqlite3.IntegrityError:
            row = self.cursor.execute(
                "SELECT review_id FROM memory_review_queue WHERE user_id=? AND source_message_id=?",
                (self.user_id, item.get("source_message_id")),
            ).fetchone()
            return row["review_id"] if row else None

    def list_memory_reviews(self, status="pending", limit=100):
        rows = self.cursor.execute(
            """
            SELECT * FROM memory_review_queue
            WHERE user_id=? AND status=? ORDER BY created_at ASC LIMIT ?
            """,
            (self.user_id, str(status or "pending"), max(1, min(int(limit), 250))),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["id"] = item.pop("review_id")
            item["candidate"] = json.loads(item.pop("candidate_json") or "{}")
            item["relevant_existing_memory_ids"] = json.loads(
                item.get("relevant_existing_memory_ids") or "[]"
            )
            item["contradiction"] = bool(item.get("contradiction"))
            items.append(item)
        return items

    def get_memory_review(self, review_id):
        row = self.cursor.execute(
            "SELECT * FROM memory_review_queue WHERE review_id=? AND user_id=?",
            (review_id, self.user_id),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["id"] = item.pop("review_id")
        item["candidate"] = json.loads(item.pop("candidate_json") or "{}")
        item["relevant_existing_memory_ids"] = json.loads(
            item.get("relevant_existing_memory_ids") or "[]"
        )
        item["contradiction"] = bool(item.get("contradiction"))
        return item

    def resolve_memory_review(self, review_id, selected_tier, custom_comment="", feedback=None):
        if self.read_only:
            return None
        now = datetime.now().isoformat()
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM memory_review_queue WHERE review_id=? AND user_id=? AND status='pending'",
                (review_id, self.user_id),
            ).fetchone()
            if not row:
                return None
            status = "dismissed" if selected_tier == "DONT_SAVE" else "resolved"
            self.connection.execute(
                """
                UPDATE memory_review_queue SET status=?, selected_tier=?,
                    custom_comment=?, resolved_at=?
                WHERE review_id=? AND user_id=?
                """,
                (status, selected_tier, str(custom_comment or "")[:2000], now, review_id, self.user_id),
            )
            details = feedback or {}
            self.connection.execute(
                """
                INSERT INTO memory_review_feedback(
                    feedback_id, user_id, review_id, candidate_fact, proposed_tier,
                    selected_tier, custom_comment, duration_hint, review_after,
                    promotion_hint, demotion_hint, explicit_exception, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), self.user_id, review_id, row["candidate_fact"],
                    row["proposed_tier"], selected_tier, str(custom_comment or "")[:2000],
                    details.get("duration"), details.get("review_after"),
                    details.get("promotion_hint"), details.get("demotion_hint"),
                    int(bool(details.get("explicit_exception"))), now,
                ),
            )
        return self.get_memory_review(review_id)

    def memory_review_feedback(self, limit=200):
        rows = self.cursor.execute(
            """
            SELECT * FROM memory_review_feedback WHERE user_id=?
            ORDER BY created_at DESC LIMIT ?
            """,
            (self.user_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_internal_note(self, note_type, content, confidence=0.7, source="reflection", expires_at=None):
        note_id = str(uuid4())
        self.cursor.execute(
            """
            INSERT INTO internal_notes (
                note_id, user_id, note_type, content, confidence, source, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (note_id, self.user_id, note_type, content, confidence, source, datetime.now().isoformat(), expires_at),
        )
        self.connection.commit()
        return note_id

    def get_internal_notes(self, note_type=None):
        query = """
            SELECT * FROM internal_notes
            WHERE user_id=?
              AND (expires_at IS NULL OR expires_at > ?)
        """
        values = [self.user_id, datetime.now().isoformat()]
        if note_type:
            query += " AND note_type=?"
            values.append(note_type)
        query += " ORDER BY created_at DESC"
        self.cursor.execute(query, values)
        return [dict(row) for row in self.cursor.fetchall()]

    def save_episode(self, summary, topic=None, emotion="neutral", started_at=None, ended_at=None):
        now = datetime.now().isoformat()
        self.cursor.execute(
            """
            INSERT INTO episodes (episode_id, user_id, summary, topic, emotion, started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()), self.user_id, summary, topic, emotion,
                started_at or now, ended_at or now,
            ),
        )
        self.connection.commit()

    def get_episodes(self, limit=20):
        self.cursor.execute(
            """
            SELECT * FROM episodes WHERE user_id=?
            ORDER BY ended_at DESC LIMIT ?
            """,
            (self.user_id, limit),
        )
        return [dict(row) for row in self.cursor.fetchall()]

    # ---------------------------------
    # Chat Storage Methods
    # ---------------------------------

    def save_chat_metadata(self, chat_id, title, updated_at, pinned, archived, *, skip_deleted=False):
        self.cursor.execute(
            """
            INSERT INTO chats (chat_id, user_id, title, updated_at, pinned, archived)
            SELECT ?, ?, ?, ?, ?, ? WHERE NOT ? OR NOT EXISTS (
                SELECT 1 FROM chat_deletion_tombstones WHERE user_id=? AND chat_id=?
            )
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                title=excluded.title,
                updated_at=excluded.updated_at,
                pinned=excluded.pinned,
                archived=excluded.archived
            """,
            (chat_id, self.user_id, title, updated_at, pinned, archived, int(skip_deleted), self.user_id, chat_id)
        )
        self.connection.commit()

    def get_chats(self):
        self.cursor.execute(
            """
            SELECT * FROM chats WHERE user_id=?
            ORDER BY updated_at DESC
            """,
            (self.user_id,)
        )
        return [dict(row) for row in self.cursor.fetchall()]

    def get_deleted_chat_ids(self):
        self.cursor.execute(
            "SELECT chat_id FROM chat_deletion_tombstones WHERE user_id=? ORDER BY deleted_at",
            (self.user_id,),
        )
        return [row["chat_id"] for row in self.cursor.fetchall()]

    def is_chat_deleted(self, chat_id):
        row = self.cursor.execute(
            "SELECT 1 FROM chat_deletion_tombstones WHERE user_id=? AND chat_id=?",
            (self.user_id, chat_id),
        ).fetchone()
        return row is not None

    def save_message(
        self,
        message_id,
        chat_id,
        sender,
        text,
        timestamp,
        model_used="",
        attachments=None,
        capability_metadata=None,
        preserve_model_diagnostic=False,
    ):
        self.cursor.execute(
            """
            INSERT INTO messages (
                message_id, chat_id, user_id, sender, text, timestamp, model_used,
                attachments_json, capability_metadata_json
            ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT ? OR NOT EXISTS (
                SELECT 1 FROM chat_deletion_tombstones WHERE user_id=? AND chat_id=?
            )
            ON CONFLICT(user_id, chat_id, message_id) DO UPDATE SET
                sender=excluded.sender,
                text=excluded.text,
                timestamp=excluded.timestamp,
                model_used=CASE WHEN ? THEN messages.model_used ELSE excluded.model_used END,
                attachments_json=excluded.attachments_json,
                capability_metadata_json=excluded.capability_metadata_json
            """,
            (
                message_id,
                chat_id,
                self.user_id,
                sender,
                text,
                timestamp,
                model_used,
                json.dumps(attachments or [], ensure_ascii=False, separators=(",", ":")),
                json.dumps(sanitize_interactive_metadata(capability_metadata), ensure_ascii=False, separators=(",", ":")),
                int(preserve_model_diagnostic), self.user_id, chat_id, int(preserve_model_diagnostic),
            )
        )
        self.connection.commit()

    def get_chat_messages(self, chat_id):
        self.cursor.execute(
            """
            SELECT * FROM messages WHERE user_id=? AND chat_id=?
            ORDER BY timestamp ASC
            """,
            (self.user_id, chat_id)
        )
        messages = []
        for row in self.cursor.fetchall():
            message = dict(row)
            try:
                message["attachments"] = json.loads(message.pop("attachments_json") or "[]")
            except (TypeError, ValueError):
                message["attachments"] = []
            try:
                message["capabilityMetadata"] = sanitize_interactive_metadata(json.loads(
                    message.pop("capability_metadata_json") or "{}"
                ))
            except (TypeError, ValueError):
                message["capabilityMetadata"] = {}
            messages.append(message)
        return messages

    def get_message_model_diagnostic(self, chat_id, message_id):
        """Return one server-owned model label without exposing it in chat history."""
        row = self.connection.execute(
            """
            SELECT model_used FROM messages
            WHERE user_id=? AND chat_id=? AND message_id=? AND sender IN ('ai', 'assistant')
            """,
            (self.user_id, chat_id, message_id),
        ).fetchone()
        return str(row["model_used"] or "").strip() if row else ""

    def save_message_model_diagnostic(self, message_id, chat_id, text, timestamp, model_used):
        """Upsert only server-produced diagnostic data; never rewrite existing content."""
        self.connection.execute(
            """
            INSERT INTO messages (
                message_id, chat_id, user_id, sender, text, timestamp, model_used,
                attachments_json, capability_metadata_json
            ) SELECT ?, ?, ?, 'ai', ?, ?, ?, '[]', '{}' WHERE NOT EXISTS (
                SELECT 1 FROM chat_deletion_tombstones WHERE user_id=? AND chat_id=?
            )
            ON CONFLICT(user_id, chat_id, message_id) DO UPDATE SET
                model_used=excluded.model_used
            WHERE messages.sender IN ('ai', 'assistant')
            """,
            (message_id, chat_id, self.user_id, text, timestamp, model_used, self.user_id, chat_id),
        )
        self.connection.commit()

    def delete_chat(self, chat_id):
        """Move a chat into the recoverable three-calendar-month Bin."""
        chat = self.connection.execute(
            "SELECT * FROM chats WHERE user_id=? AND chat_id=?",
            (self.user_id, chat_id),
        ).fetchone()
        message_rows = self.connection.execute(
            "SELECT * FROM messages WHERE user_id=? AND chat_id=? ORDER BY timestamp ASC",
            (self.user_id, chat_id),
        ).fetchall()
        now = datetime.now()
        metadata = dict(chat) if chat is not None else {
            "chat_id": chat_id,
            "user_id": self.user_id,
            "title": "Deleted chat",
            "updated_at": now.isoformat(),
            "pinned": 0,
            "archived": 0,
        }
        messages = [dict(row) for row in message_rows]
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO deleted_chats (
                    chat_id, user_id, deleted_at, purge_at, title, metadata_json, messages_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET
                    deleted_at=excluded.deleted_at,
                    purge_at=excluded.purge_at,
                    title=excluded.title,
                    metadata_json=excluded.metadata_json,
                    messages_json=excluded.messages_json
                """,
                (
                    chat_id,
                    self.user_id,
                    now.isoformat(),
                    add_calendar_months(now, 3).isoformat(),
                    str(metadata.get("title") or "Deleted chat"),
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(messages, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO chat_deletion_tombstones (chat_id, user_id, deleted_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET deleted_at=excluded.deleted_at
                """,
                (chat_id, self.user_id, now.isoformat()),
            )
            self.connection.execute(
                "DELETE FROM messages WHERE user_id=? AND chat_id=?",
                (self.user_id, chat_id),
            )
            self.connection.execute(
                "DELETE FROM chats WHERE user_id=? AND chat_id=?",
                (self.user_id, chat_id),
            )
        return True

    def purge_expired_bin(self):
        now = datetime.now().isoformat()
        with self.connection:
            result = self.connection.execute(
                "DELETE FROM deleted_chats WHERE user_id=? AND purge_at IS NOT NULL AND purge_at<=?",
                (self.user_id, now),
            )
        return int(result.rowcount or 0)

    def expired_bin_chat_ids(self):
        now = datetime.now().isoformat()
        rows = self.connection.execute(
            "SELECT chat_id FROM deleted_chats WHERE user_id=? AND purge_at IS NOT NULL AND purge_at<=?",
            (self.user_id, now),
        ).fetchall()
        return [str(row["chat_id"]) for row in rows]

    def list_deleted_chats(self):
        self.purge_expired_bin()
        rows = self.connection.execute(
            """
            SELECT chat_id, title, deleted_at, purge_at
            FROM deleted_chats WHERE user_id=?
            ORDER BY deleted_at DESC
            """,
            (self.user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def restore_deleted_chat(self, chat_id):
        row = self.connection.execute(
            "SELECT * FROM deleted_chats WHERE user_id=? AND chat_id=?",
            (self.user_id, chat_id),
        ).fetchone()
        if row is None:
            return False
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
            messages = json.loads(row["messages_json"] or "[]")
        except (TypeError, ValueError):
            return False
        if not isinstance(metadata, dict) or not isinstance(messages, list):
            return False
        with self.connection:
            self.connection.execute(
                "DELETE FROM chat_deletion_tombstones WHERE user_id=? AND chat_id=?",
                (self.user_id, chat_id),
            )
            self.connection.execute(
                """
                INSERT OR REPLACE INTO chats (chat_id, user_id, title, updated_at, pinned, archived)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    self.user_id,
                    str(metadata.get("title") or row["title"] or "Restored chat"),
                    str(metadata.get("updated_at") or datetime.now().isoformat()),
                    int(bool(metadata.get("pinned"))),
                    int(bool(metadata.get("archived"))),
                ),
            )
            for message in messages:
                if not isinstance(message, dict) or not message.get("message_id"):
                    continue
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO messages (
                        message_id, chat_id, user_id, sender, text, timestamp, model_used,
                        attachments_json, capability_metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message["message_id"], chat_id, self.user_id,
                        message.get("sender"), message.get("text"), message.get("timestamp"),
                        message.get("model_used"), message.get("attachments_json") or "[]",
                        message.get("capability_metadata_json") or "{}",
                    ),
                )
            self.connection.execute(
                "DELETE FROM deleted_chats WHERE user_id=? AND chat_id=?",
                (self.user_id, chat_id),
            )
        return True

    def permanently_delete_chat(self, chat_id):
        with self.connection:
            result = self.connection.execute(
                "DELETE FROM deleted_chats WHERE user_id=? AND chat_id=?",
                (self.user_id, chat_id),
            )
        return bool(result.rowcount)

    def empty_bin(self):
        with self.connection:
            result = self.connection.execute(
                "DELETE FROM deleted_chats WHERE user_id=?",
                (self.user_id,),
            )
        return int(result.rowcount or 0)

    def save_monthly_summary(self, month_key, summary):
        self.cursor.execute(
            """
            INSERT INTO monthly_summaries (summary_id, user_id, month_key, summary, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, month_key) DO UPDATE SET
                summary=excluded.summary, created_at=excluded.created_at
            """,
            (str(uuid4()), self.user_id, month_key, summary, datetime.now().isoformat()),
        )
        self.connection.commit()

    def get_monthly_summary(self, month_key):
        self.cursor.execute(
            "SELECT summary FROM monthly_summaries WHERE user_id=? AND month_key=?",
            (self.user_id, month_key),
        )
        row = self.cursor.fetchone()
        return row["summary"] if row else None

    # ---------------------------------
    # Close
    # ---------------------------------

    def close(self):

        self.connection.close()
