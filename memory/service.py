from datetime import datetime

from core.logger import Logger
from config.settings import REFLECTION_THRESHOLD

from memory.analyzer import MemoryAnalyzer
from memory.validator import MemoryValidator
from memory.updater import MemoryUpdater
from memory.manager import MemoryManager
from memory.retriever import MemoryRetriever
from memory.reflection import MemoryReflection
from memory.reflection_v2 import ReflectionV2
from memory.episode import EpisodicReplay, EpisodicState
from memory.knowledge_graph import KnowledgeGraph
from memory.intent import MemoryIntentClassifier
from memory.monthly_summary import MonthlySummarizer
from memory.timeline import LifeTimeline
from memory.compression import MemoryCompressor
from memory.profile import UserProfileStore
from memory.personality import PersonalityStore
from brain.emotion import EmotionDetector


def is_core_identity_override_attempt(text: str) -> bool:
    """Identify untrusted attempts to rewrite Jupiter/application authority.

    This only controls durable-memory persistence. The message still goes to
    the model, which responds naturally using the authoritative system persona.
    """
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False

    assistant_reference = any(term in normalized for term in (
        "jupiter", "you", "your", "assistant", "ai"
    ))
    authority_reference = any(term in normalized for term in (
        "created", "creator", "core identity", "system prompt", "system rule",
        "developer rule", "owner role", "admin role", "user role", "permissions"
    ))
    override_language = any(term in normalized for term in (
        "remember", "forget", "from now on", "next time", "pretend", "act as",
        "ignore previous", "ignore all", "replace", "change", "override",
        "tell me that", "say that"
    ))
    creator_claim = (
        assistant_reference
        and any(term in normalized for term in ("created you", "your creator", "creator is", "created by"))
    )
    return assistant_reference and authority_reference and (override_language or creator_claim)


class MemoryService:

    def __init__(self, user_id="default", read_only=False):

        self.user_id = user_id
        self.read_only = bool(read_only)
        self.manager = MemoryManager(user_id=user_id, read_only=self.read_only)

        self.analyzer = MemoryAnalyzer()
        self.intent = MemoryIntentClassifier()
        self.validator = MemoryValidator()

        self.updater = MemoryUpdater(self.manager)

        self.retriever = MemoryRetriever(self.manager)

        self.reflection = MemoryReflection()
        self.reflection_v2 = ReflectionV2()
        self.episodic_replay = EpisodicReplay()
        self.monthly_summarizer = MonthlySummarizer()
        self.compressor = MemoryCompressor()
        self.profile = UserProfileStore(user_id=user_id)
        self.personality = PersonalityStore(user_id=user_id)
        self.emotions = EmotionDetector()

        # Number of newly saved/updated memories
        self.saved_memories = 0

        # Reflection interval
        self.reflection_threshold = REFLECTION_THRESHOLD

        if not self.read_only:
            self.archive_expired()
            self._backfill_profile()
            try:
                self.run_offline_reflection_if_due()
            except Exception as error:
                # Reflection is a best-effort maintenance task. It must never keep
                # Jupiter from opening a normal conversation.
                Logger.warning(f"Offline reflection skipped: {error}")

    # ---------------------------------
    # Process User Message
    # ---------------------------------

    def process(
        self,
        user_message,
        recent_conversation=None,
        should_consider_memory=True,
        allow_personality_updates=True,
    ):

        if getattr(self, "read_only", False):
            return False

        from brain.conversation import temporary_speaker_possible
        if temporary_speaker_possible([*(recent_conversation or []), {"role": "user", "content": user_message}]):
            return False

        # Reject identity/authority injection before it can influence either
        # personality observations or durable profile state.
        if is_core_identity_override_attempt(user_message):
            Logger.info("Skipped durable memory for an untrusted core-identity override attempt.")
            return False

        # Style and emotional continuity are evaluated every turn.  They are
        # intentionally distinct from long-term semantic memory, so greetings
        # still update relationship continuity without being saved as facts.
        # Conversation-style observations are personality learning.  Keep them
        # out of durable state when this user disabled automatic updates.
        if allow_personality_updates:
            self.personality.observe_explicit(user_message, source="conversation:unknown:unknown")
        self.profile.observe_message(
            user_message,
            self.emotions.detect(user_message),
        )

        if not should_consider_memory:
            Logger.info("Executive plan skipped memory analysis for this turn.")
            return False

        self.archive_expired()

        memories_to_process = []
        clauses = self.intent.split_clauses(user_message)
        
        from memory.intent import regex_memory_allowed
        for clause in clauses:
            memory = self.intent.classify(clause, recent_conversation)
            if memory is not None and not regex_memory_allowed(memory):
                memory = None                    # marker 221: durable facts come from memory/turn_memory.py
            if memory is not None:
                Logger.info("Deterministic user memory intent detected.")
                memories_to_process.append(memory)
                
        # Only fallback to analyzer if no deterministic intents matched (marker 221: replaced by memory/turn_memory.py)
        from services.continuity_extraction import MODEL_ASSISTED_CANDIDATES
        if not memories_to_process and MODEL_ASSISTED_CANDIDATES:
            memory = self.analyzer.analyze(user_message, recent_conversation)
            if memory is not None:
                memories_to_process.append(memory)

        if not memories_to_process:
            Logger.info("Memory ignored.")
            return False

        changed_overall = False
        
        for memory in memories_to_process:
            Logger.info("Memory detected.")
            if not self.validator.validate(memory):
                Logger.warning("Memory validation failed.")
                continue

            Logger.info("Memory validated.")
            is_personality = memory.memory_type == "PERSONALITY"
            if is_personality and not allow_personality_updates:
                Logger.info("Skipped automatic personality update because the user disabled auto-updates.")
                continue

            # Personality is a dedicated profile with revision history. New
            # preference changes no longer create ordinary personal-memory rows.
            if is_personality:
                self.personality.update_from_memory(memory)
                changed = True
            else:
                changed = self.updater.process(memory)

            if changed:
                if not is_personality:
                    self.profile.update_from_memory(memory)
                self._record_topic_mention(memory)
                self.saved_memories += 1
                changed_overall = True

                Logger.info(
                    f"Reflection Counter: "
                    f"{self.saved_memories}/"
                    f"{self.reflection_threshold}"
                )

                if self.saved_memories >= self.reflection_threshold:
                    self.run_reflection()
                    self.saved_memories = 0

        return changed_overall

    # ---------------------------------
    # Reflection
    # ---------------------------------

    def run_reflection(self):

        Logger.info("Running Memory Reflection...")

        inferred_memories = self.reflection.reflect(
            self.manager.get_all()
        )

        for memory in inferred_memories:

            if self.validator.validate(memory):

                self.updater.process(memory)

        existing_notes = {
            note["note_type"]: note["content"]
            for note in self.manager.get_internal_notes()
        }
        for note_type, content, confidence in self.reflection_v2.reflect(
            self.manager.get_all()
        ).notes:
            if existing_notes.get(note_type) != content:
                self.manager.save_internal_note(
                    note_type,
                    content,
                    confidence=confidence,
                    source="reflection",
                )

        self.save_monthly_summary()
        self._compress_memories()
        self.manager.set_state("last_reflection_at", datetime.now().isoformat())

        Logger.success("Memory Reflection Finished.")

    # ---------------------------------
    # Hybrid Memory Retrieval
    # ---------------------------------

    def retrieve(self, user_message):

        Logger.info("Searching memories...")

        if not self.read_only:
            self.archive_expired()

        return self.retriever.retrieve(
            user_message,
            self.manager.get_all()
        )

    # ---------------------------------
    # Get All Memories
    # ---------------------------------

    def get_all(self):

        if not self.read_only:
            self.archive_expired()

        return self.manager.get_all()

    def get_personality_memories(self):

        return self.manager.get_personality_memories()

    def get_p0_capsule(self, identity=None, *, skip_learned_names: bool = False) -> str:
        """Retrieve factual P0 context without duplicating Personality.

        ``skip_learned_names``: the account's registered display name is the sole name authority (marker 169/174), so a
        stale learned "formal/preferred name" memory must not compete with it after the user renames themselves."""
        memories = self.manager.get_by_tier("P0")
        if not memories:
            return ""
        seen = set()
        lines = []
        for memory in memories:
            if memory.visibility == "SECRET":
                continue
            if identity is not None and not identity.allows_memory(memory):
                continue
            attributes = memory.attributes if isinstance(memory.attributes, dict) else {}
            if skip_learned_names and (str(memory.memory_id or "").startswith("identity.name")
                                       or any(attributes.get(key) for key in ("formal_name", "preferred_name", "name"))):
                continue
            text = " ".join(str(memory.summary or "").split())
            key = " ".join(part for part in text.casefold().split() if part.isalnum())
            if text and key not in seen:
                seen.add(key)
                lines.append(f"- {text}")
        return "\n".join(lines[:12])

    def get_personality_capsule(self) -> str:
        return self.personality.capsule()

    def personality_data(self):
        return self.personality.public_payload()

    def set_personality(self, preferences):
        return self.personality.set_manual(preferences).get("preferences", {})

    def get_language_learning_capsule(self) -> str | None:
        """Retrieve language learning history and context as a compact string."""
        from memory.types import MemoryDomain
        memories = self.get_all()
        learning_memories = [m for m in memories if getattr(m, 'domain', None) == MemoryDomain.LANGUAGE_LEARNING]
        if not learning_memories:
            return None
        return "\n".join(f"- {m.content}" for m in learning_memories if m.content)

    def persist_extracted_memory(self, memory, *, allow_personality_updates=True):
        """Apply one already-vetted extraction candidate.

        The caller has recorded its source conversation/message IDs.  This
        method still routes through ``MemoryUpdater`` so duplicate, merge, and
        contradiction handling remains the single persistence authority.
        """

        if self.read_only:
            return False
        memory.summary = " ".join(str(memory.summary or "").split())[:800]
        if is_core_identity_override_attempt(memory.summary):
            return False
        attributes = dict(memory.attributes or {})
        attributes.pop("original_statement", None)
        provenance = attributes.get("transcript_provenance")
        if isinstance(provenance, dict):
            attributes["transcript_provenance"] = {
                key: provenance[key]
                for key in ("source", "confidence", "uncertain", "completion_reason")
                if provenance.get(key) not in (None, "")
            }
            corrections = provenance.get("corrections")
            if isinstance(corrections, list) and corrections:
                attributes["transcript_provenance"]["correction_count"] = min(len(corrections), 99)
        memory.attributes = attributes
        structured_facts = [
            fact for fact in (memory.facts or [])
            if isinstance(fact, dict)
            and str(fact.get("subject") or "").strip()
            and str(fact.get("predicate") or "").strip()
            and str(fact.get("object") or "").strip()
        ]
        if not structured_facts and memory.summary:
            structured_facts = [{
                "subject": "user",
                "predicate": "shared_" + "_".join(
                    part for part in str(memory.topic or "fact").casefold().split() if part.isalnum()
                )[:60],
                "object": memory.summary,
                "confidence": float(memory.certainty or memory.confidence or 0.8),
            }]
        memory.facts = structured_facts
        if not self.validator.validate(memory):
            Logger.warning("Rejected invalid extracted memory before persistence.")
            return False
        is_personality = memory.memory_type == "PERSONALITY"
        if is_personality and not allow_personality_updates:
            return False
        if is_personality:
            self.personality.update_from_memory(memory)
            return True
        if self.personality.overlaps(getattr(memory, "summary", "")):
            Logger.info("Skipped factual memory because the canonical Personality store already contains it.")
            return False
        changed = self.updater.process(memory)
        if changed:
            # MemoryUpdater committed record, triples and source together.
            self.profile.update_from_memory(memory)
            self._record_topic_mention(memory)
        return changed

    def get_archived(self):

        return self.manager.get_archived()

    def knowledge_graph(self):
        """Expose a user-scoped fact graph for inspection and future UI tooling."""

        return KnowledgeGraph(self.manager.get_facts())

    def timeline(self):
        """Expose a chronological view including archived history."""

        return LifeTimeline().by_year(self.manager.get_all(include_archived=True))

    # ---------------------------------
    # Cross-session continuity
    # ---------------------------------

    def finalize_session(self, messages, active_topic=None, emotion="neutral"):
        """Persist one compact session replay when the terminal session ends."""

        episode = self.episodic_replay.summarize(
            messages,
            EpisodicState(active_topic=active_topic, user_emotion=emotion),
        )
        if episode is None:
            return False
        self.manager.save_episode(**episode)
        self.save_monthly_summary()
        try:
            self.run_offline_reflection_if_due(force=True)
        except Exception as error:
            Logger.warning(f"Offline reflection skipped: {error}")
        return True

    def profile_data(self):
        """Expose the durable user profile to a UI without using folder names."""

        return self.profile.load()

    def hydrate_authenticated_name(self, name: str | None):
        return self.profile.hydrate_authenticated_name(name)

    def get_user_preferences(self):
        """Return the small, user-scoped preference layer used by the client.

        It lives in companion state rather than role/session state so it cannot
        affect authorization and survives a switch between WT and mobile.
        """

        saved = self.manager.get_state("user_preferences", {})
        saved = saved if isinstance(saved, dict) else {}
        personality = self.personality.load().get("preferences") or {}
        custom_entry = personality.get("custom_instruction") or {}
        custom_instruction = str(custom_entry.get("value") or "").strip()[:1000]
        legacy_custom = str(saved.get("custom_instruction") or "").strip()[:1000]
        if not custom_instruction and legacy_custom and not self.read_only:
            self.personality.set_manual({"custom_instruction": legacy_custom})
            custom_instruction = legacy_custom
        return {
            "auto_updates": bool(saved.get("auto_updates", True)),
            "custom_instruction": custom_instruction,
        }

    def set_user_preferences(self, auto_updates=None, custom_instruction=None):
        preferences = self.get_user_preferences()
        if auto_updates is not None:
            preferences["auto_updates"] = bool(auto_updates)
        if custom_instruction is not None:
            preferences["custom_instruction"] = " ".join(
                str(custom_instruction).split()
            )[:1000]
            self.personality.set_manual({"custom_instruction": preferences["custom_instruction"]})
        self.manager.set_state("user_preferences", {"auto_updates": preferences["auto_updates"]})
        return preferences

    def run_offline_reflection_if_due(self, force=False):
        """Perform safe 'offline reflection' at most weekly (or session end).

        This is maintenance, not hidden chain-of-thought: it writes only
        inspectable notes and source-labelled summary memories from user data.
        """

        last_run = self.manager.get_state("last_offline_reflection_at")
        if not force and last_run:
            try:
                if (datetime.now() - datetime.fromisoformat(last_run)).days < 7:
                    return False
            except (TypeError, ValueError):
                pass
        memories = self.manager.get_all()
        report = self.reflection_v2.reflect(memories)
        existing = {
            (note["note_type"], note["content"])
            for note in self.manager.get_internal_notes()
        }
        for note_type, content, confidence in report.notes:
            if (note_type, content) not in existing:
                self.manager.save_internal_note(
                    note_type, content, confidence=confidence, source="offline_reflection"
                )
        self._compress_memories(memories)
        self.save_monthly_summary()
        self.manager.set_state("last_offline_reflection_at", datetime.now().isoformat())
        return True

    def _compress_memories(self, memories=None):
        """Add compact inferred summaries while preserving every source memory."""

        changed = False
        for summary in self.compressor.compress(memories or self.manager.get_all()):
            existing = self.manager.get_by_id(summary.memory_id)
            if existing and existing.attributes.get("observations") == summary.attributes.get("observations"):
                continue
            if existing:
                summary.created_at = existing.created_at
                summary.usage_count = existing.usage_count
            self.manager.save(summary)
            changed = True
        return changed

    def _record_topic_mention(self, memory):
        """Let repeated user topics become important without an LLM guess."""

        topic = (memory.topic or "").strip().lower()
        if len(topic) < 3 or topic in {"general", "personal goal", "important life event"}:
            return
        counts = self.manager.get_state("topic_mentions", {})
        if not isinstance(counts, dict):
            counts = {}
        counts[topic] = int(counts.get(topic, 0)) + 1
        # Keep operational state bounded even for a very long-lived user.
        if len(counts) > 300:
            counts = dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)[:300])
        self.manager.set_state("topic_mentions", counts)
        observations = counts[topic]
        if observations < 3:
            return
        for candidate in self.manager.get_all():
            if candidate.topic.strip().lower() == topic:
                self.manager.learning.reinforce_topic(candidate, observations)
                self.manager.save(candidate)

    def _backfill_profile(self):
        """Make profile.json available immediately for existing Jupiter users."""

        for memory in self.manager.get_all():
            if (
                memory.memory_type == "PERSONALITY"
                or memory.domain in {"PROFILE", "PREFERENCE", "GOAL", "RELATIONSHIP"}
                or memory.category == "Life Chapter"
            ):
                self.profile.update_from_memory(memory)

    def save_monthly_summary(self):
        month_key = datetime.now().strftime("%Y-%m")
        summary = self.monthly_summarizer.build(self.manager.get_all(), month_key)
        if summary:
            self.manager.save_monthly_summary(month_key, summary)
        return summary

    def archive_expired(self):

        if self.read_only:
            return []

        archived = self.manager.archive_expired()
        archived.extend(self.manager.decay_stale())

        if archived:
            Logger.info(f"Archived {len(archived)} expired memories.")

        return archived

    # ---------------------------------
    # Get Memory By ID
    # ---------------------------------

    def get_by_id(self, memory_id):

        return self.manager.get_by_id(memory_id)

    # ---------------------------------
    # Delete Memory
    # ---------------------------------

    def delete(self, memory_id):
        if self.read_only:
            return False
        memory = self.manager.get_by_id(memory_id)
        if memory is None or not self.manager.delete(memory_id):
            return False
        attributes = memory.attributes if isinstance(memory.attributes, dict) else {}
        person_name = attributes.get("person_name") or (
            attributes.get("entity") if memory.domain == "RELATIONSHIP" else None
        )
        if isinstance(person_name, str) and person_name.strip():
            normalized = " ".join(person_name.casefold().split())
            still_active = any(
                candidate.memory_id != memory_id
                and candidate.domain == "RELATIONSHIP"
                and " ".join(str((candidate.attributes or {}).get("person_name") or (candidate.attributes or {}).get("entity") or "").casefold().split()) == normalized
                for candidate in self.manager.get_all()
            )
            if not still_active:
                self.profile.remove_person(person_name)
        return True

    # ---------------------------------
    # Close
    # ---------------------------------

    def close(self):

        self.manager.close()
