from datetime import datetime
import re

from core.logger import Logger
from memory.schema import Memory
from memory.ranking import MemoryRanker
from memory.manager import MemoryManager
from memory.types import MemoryTier, MemoryType
from memory.reference_resolver import PersonalReferenceResolver
from config.settings import MAX_RELEVANT_MEMORIES, SEMANTIC_SEARCH_LIMIT


class MemoryRetriever:

    _EXPLICIT_ARCHIVE_RECALL = re.compile(
        r"(?i)\b(remember|recall|look up|search (?:my )?memor(?:y|ies)|what did i|"
        r"when did i|previously|in the past|earlier|last (?:week|month|year)|yesterday)\b"
    )

    def __init__(self, manager=None):

        self.ranker = MemoryRanker()
        self.manager = manager or MemoryManager()

    # ---------------------------------
    # Retrieve Relevant Memories
    # ---------------------------------

    def retrieve(
        self,
        user_message: str,
        memories: list[Memory],
        preferred_domains=(),
    ):

        # -----------------------------
        # Semantic Search
        # -----------------------------

        allow_archive_recall = bool(self._EXPLICIT_ARCHIVE_RECALL.search(user_message))

        def tier_of(memory: Memory) -> str:
            attributes = memory.attributes if isinstance(memory.attributes, dict) else {}
            saved = str(attributes.get("memory_tier") or "").upper()
            if saved in {tier.value for tier in MemoryTier}:
                return saved
            memory_type = str(memory.memory_type or "").upper()
            if memory_type == MemoryType.PERSONALITY.value:
                return MemoryTier.CORE_ACTIVE.value
            if memory_type == MemoryType.ARCHIVE.value:
                return MemoryTier.PERMANENT_ARCHIVE.value
            if memory_type == MemoryType.EPISODIC.value:
                return MemoryTier.EPISODIC_TIME_AWARE.value
            if memory_type in {MemoryType.WORKING.value, MemoryType.SHORT_TERM.value}:
                return MemoryTier.WORKING_RECENT.value
            return MemoryTier.ACTIVE_LONG_TERM.value

        def eligible(memory: Memory) -> bool:
            tier = tier_of(memory)
            if tier == MemoryTier.ACTIVE_LONG_TERM.value:
                return True
            if allow_archive_recall and tier in {
                MemoryTier.PERMANENT_ARCHIVE.value,
                MemoryTier.EPISODIC_TIME_AWARE.value,
            }:
                return True
            return False

        try:
            semantic_memories = [
                memory
                for memory in self.manager.semantic_search(
                    user_message,
                    n_results=SEMANTIC_SEARCH_LIMIT,
                )
                if memory.memory_type != MemoryType.PERSONALITY.value
                and memory.domain != "PROFILE"
                and eligible(memory)
            ]
        except Exception as error:
            # A missing local embedding model must not stop normal conversation.
            Logger.warning(f"Semantic retrieval unavailable; using keyword recall: {error}")
            semantic_memories = []

        # -----------------------------
        # Keyword Search
        # -----------------------------

        keywords = self.extract_keywords(user_message)

        keyword_memories = []

        searchable_memories = [
            memory
            for memory in memories
            if memory.memory_type != MemoryType.PERSONALITY.value and memory.domain != "PROFILE"
            and eligible(memory)
        ]

        for memory in searchable_memories:

            text = (
                f"{memory.category} "
                f"{memory.topic} "
                f"{memory.summary}"
            ).lower()

            if any(keyword in text for keyword in keywords):
                keyword_memories.append(memory)

        # -----------------------------
        # Merge
        # -----------------------------

        merged = {}

        for memory in semantic_memories:
            merged[memory.memory_id] = memory

        for memory in keyword_memories:
            merged[memory.memory_id] = memory

        # Personal references must be resolved before a capability constructs
        # its arguments. Structured relationship candidates are bounded and
        # relation-matched; unrelated memories are not injected.
        for memory in searchable_memories:
            if PersonalReferenceResolver.candidates(user_message, [memory]):
                merged[memory.memory_id] = memory

        candidates = list(merged.values())

        # No semantic, keyword, or structured-relation match means there is no
        # bounded memory context for this turn. Injecting the entire user's
        # memory store here would expose unrelated private facts and weaken
        # capability argument resolution.
        if not candidates:
            return []

        ranked = self.ranker.rank(user_message, candidates)
        if preferred_domains and preferred_domains != ("GENERAL",):
            preferred = set(preferred_domains)
            ranked.sort(key=lambda memory: memory.domain not in preferred)

        return self.select_diverse(ranked, MAX_RELEVANT_MEMORIES)

    def select_diverse(self, ranked_memories, limit):
        """Avoid spending the entire context budget on one repeated topic/domain."""

        selected = []
        domain_counts = {}
        topic_counts = {}
        deferred = []

        for memory in ranked_memories:
            domain = memory.domain or "GENERAL"
            topic = memory.topic.lower()
            if domain_counts.get(domain, 0) >= 2 or topic_counts.get(topic, 0) >= 1:
                deferred.append(memory)
                continue
            selected.append(memory)
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
            if len(selected) == limit:
                return selected

        # A small memory store should still use the available budget even if its
        # records are similar; diversity is a preference, not lost recall.
        for memory in deferred:
            if len(selected) == limit:
                break
            selected.append(memory)
        return selected

    # ---------------------------------
    # Keyword Extraction
    # ---------------------------------

    def extract_keywords(self, message):

        stop_words = {

            "what", "which", "who", "when", "where",
            "why", "how", "is", "are", "was", "were",
            "do", "does", "did", "my", "me", "i",
            "you", "the", "a", "an", "to", "of",
            "for", "own", "have", "tell", "about",
            "please", "can", "could", "would",
            # Marker 221: filler that matched junk memories on nearly every message (English + Hinglish)
            "with", "doing", "right", "now", "just", "and", "but", "that", "this", "there", "then", "your", "yours",
            "know", "remember", "today", "tonight", "okay", "yes", "will", "should", "shall", "want", "need", "like",
            "hai", "hain", "tha", "thi", "the", "kya", "kyu", "kyun", "kaise", "kaun", "kab", "kahan", "mai", "main",
            "mein", "mera", "meri", "mere", "mujhe", "tum", "tumhe", "aap", "uska", "uski", "uske", "unka", "unki",
            "vo", "woh", "wo", "yeh", "ye", "nahi", "nhi", "bhi", "aur", "toh", "pata", "batao", "bata", "kar", "karo",
            "boss", "bhai", "yaar", "jupiter"

        }

        keywords = []

        for word in message.lower().split():

            word = word.strip(".,?!:;()[]{}\"'")

            if len(word) < 3:
                continue

            if word in stop_words:
                continue

            keywords.append(word)

        return keywords

    # ---------------------------------
    # Mark Used
    # ---------------------------------

    def mark_used(self, memory: Memory):

        self.manager.increment_usage(memory)

        return memory
