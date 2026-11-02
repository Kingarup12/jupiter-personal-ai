from datetime import datetime

from storage.sqlite_store import SQLiteStore
from memory.vector_store import VectorStore
from memory.learning import MemoryLearning


class MemoryManager:

    def __init__(
        self,
        user_id="default",
        read_only=False,
        *,
        database_path=None,
        vector_path=None,
    ):

        self.read_only = bool(read_only)
        self.database = SQLiteStore(
            database_path=database_path,
            user_id=user_id,
            read_only=self.read_only,
        )
        self.vector_store = VectorStore(
            user_id=user_id,
            read_only=self.read_only,
            path=vector_path,
        )
        self.learning = MemoryLearning()
    # -----------------------------

    def save(self, memory):

        print("[Memory Manager] Saving memory...")

        self.database.save(memory)

        if self.vector_store.exists(memory.memory_id):

            self.vector_store.update(memory)

        else:

            self.vector_store.add(memory)

    # -----------------------------
    # Save Fact
    # -----------------------------

    def save_fact(self, fact):

        self.database.save_fact(fact)

    def save_bundle(self, memory, facts, *, supersedes=None, defer_index=False):
        self.database.save_memory_bundle(memory, facts, supersedes=supersedes)
        if defer_index:
            # Marker 221: the per-turn memory step commits to SQLite (the truth) at once; the derived semantic index
            # (a remote embedding call, up to ~15 s when unreachable) is refreshed in the background, so a reply never
            # waits for it and never reports a committed save as pending.
            import contextvars
            import threading
            threading.Thread(target=contextvars.copy_context().run,
                             args=(MemoryManager._refresh_index_detached, self.database.user_id, memory, supersedes),
                             daemon=True, name="memory-index").start()
            return
        # SQLite owns the durable commit. A derived index outage must not turn
        # a committed write into a false failure; retrieval rechecks every ID.
        try:
            if supersedes and self.vector_store.exists(supersedes):
                self.vector_store.delete(supersedes)
            if self.vector_store.exists(memory.memory_id):
                self.vector_store.update(memory)
            else:
                self.vector_store.add(memory)
        except Exception:
            from core.logger import Logger
            Logger.warning("Memory committed; derived semantic index refresh unavailable.")

    @staticmethod
    def _refresh_index_detached(user_id, memory, supersedes=None):
        manager = None
        try:
            manager = MemoryManager(user_id=user_id)
            if supersedes and manager.vector_store.exists(supersedes):
                manager.vector_store.delete(supersedes)
            if manager.vector_store.exists(memory.memory_id):
                manager.vector_store.update(memory)
            else:
                manager.vector_store.add(memory)
        except Exception:
            from core.logger import Logger
            Logger.warning("Memory committed; derived semantic index refresh unavailable.")
        finally:
            if manager is not None:
                try:
                    manager.close()
                except Exception:
                    pass

    def add_memory_source(self, memory_id, chat_id, message_id):
        return self.database.add_memory_source(memory_id, chat_id, message_id)

    def memory_source_count(self, memory_id):
        return self.database.memory_source_count(memory_id)

    def remove_chat_provenance(self, chat_id):
        """Remove source evidence and synchronize every retrieval representation."""

        if self.read_only:
            return {
                "deleted_memory_ids": [], "retained_memory_ids": [],
                "vector_entries_deleted": 0, "review_cleanup": {},
            }
        cleanup = self.database.remove_chat_memory_sources(chat_id)
        vector_deleted = 0
        for memory_id in cleanup.get("deleted_memory_ids") or []:
            if self.vector_store.exists(memory_id):
                self.vector_store.delete(memory_id)
                vector_deleted += 1
        cleanup["vector_entries_deleted"] = vector_deleted
        return cleanup

    # -----------------------------
    # Delete Facts
    # -----------------------------

    def delete_facts(self, memory_id):

        self.database.delete_facts(memory_id)

    # -----------------------------
    # Get Facts
    # -----------------------------

    def get_facts(self, memory_id=None):

        return self.database.get_facts(memory_id)

    # -----------------------------
    # Get All Memories
    # -----------------------------

    def get_all(self, include_archived=False):

        return self.database.get_all(include_archived=include_archived)

    # -----------------------------
    # Get Memory by ID
    # -----------------------------

    def get_by_id(self, memory_id):

        return self.database.get_by_id(memory_id)

    # -----------------------------
    # Profile and Archives
    # -----------------------------

    def get_personality_memories(self):

        return self.database.get_personality_memories()

    def get_by_tier(self, tier):

        return self.database.get_by_tier(tier)

    def get_archived(self):

        return self.database.get_archived()

    # -----------------------------
    # Companion continuity state
    # -----------------------------

    def set_state(self, state_key, value):
        if self.read_only:
            return False
        self.database.set_state(state_key, value)
        return True

    def get_state(self, state_key, default=None):
        return self.database.get_state(state_key, default)

    def save_internal_note(self, note_type, content, confidence=0.7, source="reflection", expires_at=None):
        return self.database.save_internal_note(
            note_type, content, confidence, source, expires_at
        )

    def get_internal_notes(self, note_type=None):
        return self.database.get_internal_notes(note_type)

    def save_episode(self, summary, topic=None, emotion="neutral", started_at=None, ended_at=None):
        self.database.save_episode(summary, topic, emotion, started_at, ended_at)

    def get_episodes(self, limit=20):
        return self.database.get_episodes(limit)

    def save_monthly_summary(self, month_key, summary):
        self.database.save_monthly_summary(month_key, summary)

    def get_monthly_summary(self, month_key):
        return self.database.get_monthly_summary(month_key)

    def archive_expired(self):
        """Move expired active memories out of semantic retrieval."""

        memory_ids = self.database.archive_expired()

        for memory_id in memory_ids:
            if self.vector_store.exists(memory_id):
                self.vector_store.delete(memory_id)

        return memory_ids

    def archive(self, memory_id, reason=None, superseded_by=None):

        archived = self.database.archive_memory(memory_id, reason, superseded_by)

        if archived and self.vector_store.exists(memory_id):
            self.vector_store.delete(memory_id)

        return archived

    # -----------------------------
    # Memory Count
    # -----------------------------

    def count(self):

        return self.vector_store.count()

    # -----------------------------
    # Keyword Search
    # -----------------------------

    def search(self, keyword):

        keyword = keyword.lower()

        results = []

        for memory in self.database.get_all():

            text = (
                f"{memory.category} "
                f"{memory.topic} "
                f"{memory.summary}"
            ).lower()

            if keyword in text:

                results.append(memory)

        return results

    # -----------------------------
    # Semantic Search
    # -----------------------------

    def semantic_search(self, query, n_results=5):

        results = self.vector_store.search(
            query,
            n_results
        )

        ids = results.get("ids", [])

        if not ids:
            return []

        memories = []

        for memory_id in ids[0]:

            memory = self.database.get_by_id(memory_id)

            if memory:

                memories.append(memory)

        return memories

    # -----------------------------
    # Find Similar Memories
    # -----------------------------

    def find_similar(self, memory, n_results=5):

        query = f"""
Category:
{memory.category}

Topic:
{memory.topic}

Summary:
{memory.summary}
"""

        exact_subject_matches = self.database.find_by_category_topic(
            memory.category,
            memory.topic,
        )
        semantic_matches = self.semantic_search(
            query=query,
            n_results=n_results
        )

        merged = {
            candidate.memory_id: candidate
            for candidate in exact_subject_matches + semantic_matches
        }

        return list(merged.values())[:n_results]

    # -----------------------------
    # Increment Usage
    # -----------------------------

    def increment_usage(self, memory):
        self.learning.reinforce(memory, datetime.now())
        self.database.save(memory)

    def decay_stale(self):
        """Apply at most one conservative decay step per stale memory period."""

        archived_ids = []
        for memory in self.database.get_all():
            before = (memory.importance, memory.confidence, memory.last_updated)
            should_archive = self.learning.decay(memory)
            after = (memory.importance, memory.confidence, memory.last_updated)
            if after != before:
                self.database.save(memory)
            if should_archive and self.archive(memory.memory_id, reason="stale_decay"):
                archived_ids.append(memory.memory_id)
        return archived_ids

    # -----------------------------
    # Delete Memory
    # -----------------------------

    def delete(self, memory_id):
        """Soft-delete from retrieval while retaining history for recovery/audit."""

        return self.archive(memory_id, reason="user_forget")

    # -----------------------------
    # Close
    # -----------------------------

    def close(self):

        self.database.close()
