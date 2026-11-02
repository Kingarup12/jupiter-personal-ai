"""Conservative recurring-pattern detector; never presents a guess as fact."""

from collections import Counter


class HabitEngine:
    def detect(self, memories):
        occurrences = Counter()
        for memory in memories:
            if memory.domain not in {"SHORT_TERM", "EPISODIC", "HEALTH", "GENERAL"}:
                continue
            for fact in memory.facts:
                if isinstance(fact, dict):
                    key = (fact.get("predicate", "").lower(), fact.get("object", "").lower())
                    if all(key):
                        occurrences[key] += 1
        return [
            {"pattern": f"{predicate} {object_}", "occurrences": count, "confidence": min(0.85, 0.35 + count * 0.1)}
            for (predicate, object_), count in occurrences.items()
            if count >= 3
        ]
