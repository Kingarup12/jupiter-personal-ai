"""Deterministic memory reinforcement and conservative decay."""

from datetime import datetime

from memory.types import MemoryType


class MemoryLearning:
    """Let useful memories earn importance without making data disappear suddenly."""

    PROMOTION_THRESHOLDS = ((15, "Critical"), (5, "High"))
    IMPORTANCE_ORDER = ("Ephemeral", "Low", "Medium", "High", "Critical")

    def reinforce(self, memory, now=None):
        now = now or datetime.now()
        memory.usage_count += 1
        memory.last_accessed = now.isoformat()
        memory.last_updated = now.isoformat()
        # Evolve confidence much faster: 20 mentions = 1.0 confidence
        memory.confidence = min(1.0, round(memory.confidence + 0.04, 2))

        for threshold, importance in self.PROMOTION_THRESHOLDS:
            if memory.usage_count >= threshold:
                memory.importance = importance
                break
        attributes = dict(memory.attributes or {})
        tier = str(attributes.get("memory_tier") or "P1").upper()
        if tier in {"P2", "P3"} and memory.usage_count >= 5 and memory.confidence >= 0.8:
            attributes["promotion_review_recommended"] = True
            attributes["promotion_reason"] = "repeated_relevant_recall_requires_confirmation"
        if tier == "P1" and memory.usage_count >= 15 and attributes.get("user_declared_important"):
            attributes["promotion_review_recommended"] = True
            attributes["promotion_reason"] = "explicit_importance_and_repeated_relevance"
        memory.attributes = attributes
        return memory

    def reinforce_topic(self, memory, observations, now=None):
        """Raise confidence when a topic keeps appearing across conversations."""

        now = now or datetime.now()
        if observations >= 40:
            target = "Critical"
        elif observations >= 10:
            target = "High"
        elif observations >= 3:
            target = "Medium"
        else:
            return memory
        if self.IMPORTANCE_ORDER.index(memory.importance) < self.IMPORTANCE_ORDER.index(target):
            memory.importance = target
        memory.confidence = min(0.99, round(max(memory.confidence, 0.55) + 0.02, 2))
        memory.attributes = {**memory.attributes, "topic_observations": observations}
        # Repetition may recommend promotion, but it must not silently change
        # a time-limited P3 into permanent P2 without user review.
        if (
            memory.attributes.get("memory_tier") == "P3"
            and observations >= 10
            and memory.confidence >= 0.8
        ):
            memory.attributes["promotion_review_recommended"] = True
            memory.attributes["promotion_reason"] = "repeated_consistent_pattern_requires_confirmation"
        memory.last_updated = now.isoformat()
        return memory

    def decay(self, memory, now=None):
        """Fade stale memories gradually and signal only very old archive candidates."""

        if memory.memory_type == MemoryType.PERSONALITY.value:
            return False
        if not memory.last_accessed:
            return False

        now = now or datetime.now()
        try:
            age_days = max(0, (now - datetime.fromisoformat(memory.last_accessed)).days)
        except ValueError:
            return False
        attributes = dict(memory.attributes or {})
        evidence_count = max(
            int(memory.usage_count or 0),
            int(attributes.get("topic_observations") or 0),
        )
        user_protected = bool(
            attributes.get("user_confirmed")
            or attributes.get("custom_retention_days")
            or attributes.get("review_date")
        )

        tier = str(attributes.get("memory_tier") or "P1").upper()
        # A very old, low-confidence, unprotected memory is already an archive
        # candidate. Do not make callers wait for a second decay pass merely
        # because its tier is also eligible for a gradual P1 demotion.
        if (
            age_days >= 730
            and evidence_count <= 1
            and memory.confidence <= 0.35
            and memory.importance in {"Ephemeral", "Low", "Medium"}
            and not user_protected
        ):
            return True
        if (
            tier == "P1"
            and age_days >= 540
            and evidence_count <= 1
            and memory.confidence < 0.5
            and not user_protected
        ):
            attributes["memory_tier"] = "P2"
            attributes["demotion_reason"] = "long_term_relevance_decay"
            memory.attributes = attributes
            memory.importance = "Medium"
            memory.last_updated = now.isoformat()
            return False

        # Time-aware archives may fade in relevance before their calendar
        # expiry, but expiry itself remains authoritative and is never replaced
        # with a fixed 90-day approximation.
        fade_after = {
            MemoryType.SHORT_TERM.value: 14,
            MemoryType.EPISODIC.value: 60,
        }.get(memory.memory_type)
        if (
            fade_after is not None
            and age_days >= fade_after
            and evidence_count < 2
            and memory.importance not in {"High", "Critical"}
            and not user_protected
        ):
            try:
                last_adjusted = datetime.fromisoformat(memory.last_updated)
                if (now - last_adjusted).days >= 14:
                    current_index = self.IMPORTANCE_ORDER.index(memory.importance)
                    if current_index > 0:
                        memory.importance = self.IMPORTANCE_ORDER[current_index - 1]
                    memory.confidence = max(0.2, round(memory.confidence - 0.05, 2))
                    memory.last_updated = now.isoformat()
            except (TypeError, ValueError):
                pass
            return False

        if (
            age_days < 180
            or evidence_count > 1
            or memory.confidence >= 0.6
            or memory.importance in {"High", "Critical"}
            or user_protected
        ):
            return False

        try:
            last_adjusted = datetime.fromisoformat(memory.last_updated)
            if (now - last_adjusted).days < 180:
                return False
        except (TypeError, ValueError):
            pass

        current_index = self.IMPORTANCE_ORDER.index(memory.importance)
        if current_index > 0:
            memory.importance = self.IMPORTANCE_ORDER[current_index - 1]
        memory.confidence = max(0.1, round(memory.confidence - 0.05, 2))
        memory.last_updated = now.isoformat()

        # Archive only after a long period at low confidence. The archive stays
        # available for inspection and can be restored by developer tooling.
        return age_days >= 730 and memory.importance in {"Ephemeral", "Low"} and memory.confidence <= 0.35
