"""Derive soft, inspectable topic interests from the user's own memories."""

from collections import defaultdict


class InterestModel:
    STOP_TOPICS = {"name", "identity", "daily life", "response length"}

    def scores(self, memories):
        totals = defaultdict(float)
        for memory in memories:
            topic = memory.topic.strip().lower()
            if not topic or topic in self.STOP_TOPICS:
                continue
            weight = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1, "Ephemeral": 0.5}.get(memory.importance, 1)
            totals[topic] += weight + min(memory.usage_count, 10) * 0.2
        if not totals:
            return {}
        maximum = max(totals.values())
        return {
            topic: round((score / maximum) * 10, 1)
            for topic, score in sorted(totals.items(), key=lambda item: item[1], reverse=True)
        }
