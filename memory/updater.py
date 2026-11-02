from datetime import datetime

from config.settings import MIN_REASONING_CONFIDENCE
from core.logger import Logger
from memory.contradiction_detector import ContradictionDetector
from memory.duplicate_detector import DuplicateDetector
from memory.fact_extractor import FactExtractor


class MemoryUpdater:
    """Safely insert, merge, or supersede a long-term memory."""

    def __init__(self, memory_manager):
        self.memory_manager = memory_manager
        self.detector = DuplicateDetector()
        self.contradiction_detector = ContradictionDetector()
        self.fact_extractor = FactExtractor()
        self.last_persisted_memory_id = None

    def save_facts(self, memory):
        Logger.info("Extracting facts...")
        facts = self.fact_extractor.extract(memory)
        self.memory_manager.delete_facts(memory.memory_id)

        for fact in facts:
            self.memory_manager.save_fact(fact)

        Logger.success(f"{len(facts)} facts saved.")

    def _save_new(self, memory):
        self.memory_manager.save_bundle(memory, self.fact_extractor.extract(memory))
        self.last_persisted_memory_id = memory.memory_id
        Logger.success("Memory saved.")
        return True

    def _replacement_id(self, memory_id):
        suffix = datetime.now().strftime("%Y%m%d%H%M%S%f")
        return f"{memory_id}.v{suffix}"

    def _archive_and_replace(self, old_memory, new_memory):
        """Keep the former fact as history and create a new active current state."""

        replacement_id = self._replacement_id(old_memory.memory_id)
        new_memory.memory_id = replacement_id
        new_memory.last_updated = datetime.now().isoformat()
        self.memory_manager.save_bundle(new_memory, self.fact_extractor.extract(new_memory),
                                        supersedes=old_memory.memory_id)
        self.last_persisted_memory_id = new_memory.memory_id
        Logger.success(f"Archived old memory {old_memory.memory_id} as history.")
        return True

    def process(self, new_memory):
        self.last_persisted_memory_id = None
        profile_candidate = self._profile_candidate(new_memory)
        if profile_candidate is not None:
            return self._update_profile(profile_candidate, new_memory)

        all_memories = self.memory_manager.get_all()

        if not all_memories:
            Logger.info("First memory detected.")
            return self._save_new(new_memory)

        candidate_memories = self.memory_manager.find_similar(new_memory, n_results=5)
        Logger.info(f"Memory search returned {len(candidate_memories)} candidates.")

        duplicate = self.detector.detect(candidate_memories, new_memory)

        if duplicate is None:
            Logger.info("New memory detected.")
            return self._save_new(new_memory)

        Logger.info(f"Possible duplicate found: {duplicate.memory_id}")

        if self.is_same(duplicate, new_memory):
            duplicate.confidence = min(1.0, round(duplicate.confidence + 0.02, 2))
            duplicate.last_updated = datetime.now().isoformat()
            duplicate.source = new_memory.source or duplicate.source
            self.memory_manager.save_bundle(duplicate, self.fact_extractor.extract(duplicate))
            self.last_persisted_memory_id = duplicate.memory_id
            Logger.info("Memory confirmed; confidence reinforced.")
            return True

        contradiction = self.contradiction_detector.detect(duplicate, new_memory)
        if (
            contradiction.contradiction
            and contradiction.confidence >= MIN_REASONING_CONFIDENCE
        ):
            Logger.warning(f"Contradiction detected: {contradiction.reason}")

            if contradiction.action == "replace":
                return self._archive_and_replace(duplicate, new_memory)

            Logger.warning("Keeping the existing memory and preserving the new claim.")
            new_memory.memory_id = self._replacement_id(new_memory.memory_id)
            return self._save_new(new_memory)

        merged_summary = self.detector.last_result.merged_summary
        if merged_summary:
            new_memory.summary = merged_summary.strip()

        Logger.info("Updating existing memory...")
        new_memory.memory_id = duplicate.memory_id
        new_memory.created_at = duplicate.created_at
        new_memory.last_updated = datetime.now().isoformat()
        new_memory.usage_count = duplicate.usage_count
        new_memory.last_accessed = duplicate.last_accessed
        new_memory.confidence = min(
            1.0,
            round(max(duplicate.confidence, new_memory.confidence) + 0.02, 2),
        )

        if not new_memory.source:
            new_memory.source = duplicate.source

        self.memory_manager.save_bundle(new_memory, self.fact_extractor.extract(new_memory))
        self.last_persisted_memory_id = new_memory.memory_id
        Logger.success("Memory updated.")
        return True

    def _profile_candidate(self, new_memory):
        """Update identity and communication preferences without LLM guessing."""

        if new_memory.domain != "PROFILE":
            return None

        direct = self.memory_manager.get_by_id(new_memory.memory_id)
        if direct is not None:
            return direct

        profile_memories = self.memory_manager.get_personality_memories()
        if new_memory.memory_id == "identity.name":
            return next(
                (
                    memory
                    for memory in profile_memories
                    if memory.memory_id.startswith("identity.name")
                    or memory.topic.lower() in {"name", "formal name", "user name"}
                ),
                None,
            )

        return next(
            (
                memory
                for memory in profile_memories
                if memory.domain == new_memory.domain
                and memory.topic.lower() == new_memory.topic.lower()
            ),
            None,
        )

    def _update_profile(self, old_memory, new_memory):
        if self.is_same(old_memory, new_memory):
            old_memory.confidence = min(1.0, round(old_memory.confidence + 0.02, 2))
            old_memory.last_updated = datetime.now().isoformat()
            old_memory.source = new_memory.source or old_memory.source
            self.memory_manager.save_bundle(old_memory, self.fact_extractor.extract(old_memory))
            self.last_persisted_memory_id = old_memory.memory_id
            Logger.info("Profile memory confirmed; confidence reinforced.")
            return True

        Logger.info("Profile memory changed; preserving the earlier value as history.")
        return self._archive_and_replace(old_memory, new_memory)

    @staticmethod
    def is_same(old_memory, new_memory):
        return old_memory.summary.strip().lower() == new_memory.summary.strip().lower()
