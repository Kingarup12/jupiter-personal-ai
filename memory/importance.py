"""Deterministic importance prediction that complements, not replaces, the LLM."""


class MemoryImportancePredictor:
    ORDER = {"Ephemeral": 0, "Low": 1, "Medium": 2, "High": 3, "Critical": 4}
    DOMAIN_BASELINE = {
        "PROFILE": "High",
        "GOAL": "High",
        "PROJECT": "High",
        "HEALTH": "High",
        "RELATIONSHIP": "Medium",
        "EDUCATION": "Medium",
        "WORK": "Medium",
        "PREFERENCE": "Medium",
        "DEVICE": "Medium",
    }
    FUTURE_CUES = {"goal", "dream", "deadline", "exam", "project", "important", "always", "never", "prefer"}

    def apply(self, memory, user_message):
        baseline = self.DOMAIN_BASELINE.get(memory.domain, "Low")
        words = set(user_message.lower().split())
        factors = [f"domain={memory.domain.lower()}"]
        if words.intersection(self.FUTURE_CUES):
            baseline = self._raise(baseline)
            factors.append("future_usefulness")
        if memory.emotion not in {"", "neutral"}:
            baseline = self._raise(baseline)
            factors.append("emotional_salience")
        if memory.certainty < 0.6:
            baseline = self._lower(baseline)
            factors.append("low_certainty")
        if self.ORDER[baseline] > self.ORDER.get(memory.importance, 1):
            memory.importance = baseline
        memory.attributes = {**memory.attributes, "importance_factors": factors}
        return memory

    def _raise(self, value):
        inverse = list(self.ORDER)
        return inverse[min(self.ORDER[value] + 1, len(inverse) - 1)]

    def _lower(self, value):
        inverse = list(self.ORDER)
        return inverse[max(self.ORDER[value] - 1, 0)]
