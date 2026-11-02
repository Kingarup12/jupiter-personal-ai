"""Auditable memory compression; it produces summaries, never deletes facts."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from memory.schema import Memory


class MemoryCompressor:
    """Find strongly repeated interests and represent them as soft summaries."""

    STOP_WORDS = {"general", "conversation", "daily", "life", "question", "doubt", "name"}

    @staticmethod
    def _is_reflection(memory) -> bool:
        # Summaries produced here must never be re-counted as new evidence (no summaries-of-summaries,
        # no inflated observation counts or confidence on repeated compaction passes).
        return str(getattr(memory, "source", "") or "") == "reflection" or str(
            getattr(memory, "memory_id", "") or "").startswith("reflection.")

    def compress(self, memories):
        memories = [memory for memory in memories if not self._is_reflection(memory)]
        topics = Counter(
            memory.topic.strip()
            for memory in memories
            if memory.topic.strip() and memory.topic.strip().lower() not in self.STOP_WORDS
        )
        results = []
        now = datetime.now().isoformat()
        for topic, count in topics.items():
            if count < 3:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", topic.lower()).strip("_")[:48]
            results.append(
                Memory(
                    memory_id=f"reflection.interest.{slug}",
                    category="Learned Interest",
                    topic=topic,
                    summary=f"The user has repeatedly discussed {topic}, suggesting an ongoing interest.",
                    memory_type="SEMANTIC",
                    domain="PREFERENCE",
                    facts=[{"subject": "User", "predicate": "recurring_interest", "object": topic}],
                    attributes={"interest": topic, "observations": count, "inferred": True},
                    importance="Medium",
                    retention="Forever",
                    visibility="PERSONAL",
                    confidence=min(0.85, 0.45 + count * 0.05),
                    source="reflection",
                    created_at=now,
                    last_updated=now,
                    last_accessed=now,
                )
            )
        results.extend(self.compress_by_domain(memories))
        return results

    def compress_by_domain(self, memories):
        """Create higher-level summaries from memories sharing domain and topic patterns."""
        from collections import defaultdict
        
        domain_groups = defaultdict(list)
        for memory in memories:
            if self._is_reflection(memory):
                continue
            if memory.domain in ('PROFILE', 'SHORT_TERM', 'EPISODIC'):
                continue  # Don't compress transient or identity memories
            key = (memory.domain, memory.category)
            domain_groups[key].append(memory)
        
        results = []
        now = datetime.now().isoformat()
        for (domain, category), group in domain_groups.items():
            if len(group) < 4:
                continue
            topics = list({m.topic.strip() for m in group if m.topic.strip()})
            if not topics:
                continue
            slug = re.sub(r'[^a-z0-9]+', '_', f"{domain}_{category}".lower()).strip('_')[:48]
            topic_list = ', '.join(topics[:8])
            summary = f"The user has {len(group)} memories about {category.lower()}: {topic_list}."
            if len(topics) > 8:
                summary += f" And {len(topics) - 8} more topics."
            results.append(
                Memory(
                    memory_id=f"reflection.domain.{slug}",
                    category="Domain Summary",
                    topic=f"{category} Overview",
                    summary=summary,
                    memory_type="SEMANTIC",
                    domain=domain,
                    facts=[{"subject": "User", "predicate": "has_interest_cluster", "object": topic_list}],
                    attributes={"domain": domain, "category": category, "member_count": len(group), "topics": topics[:8], "inferred": True},
                    importance="Medium",
                    retention="Forever",
                    visibility="PERSONAL",
                    confidence=min(0.85, 0.40 + len(group) * 0.05),
                    source="reflection",
                    created_at=now,
                    last_updated=now,
                    last_accessed=now,
                )
            )
        return results
