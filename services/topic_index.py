"""A stable, editable topic index derived only from durable Jupiter memories."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime


_TRIVIAL_TOPICS = {
    "general",
    "greeting",
    "small talk",
    "conversation",
    "test",
    "testing",
    "verification",
    "personal goal",
    "important life event",
}
_TRIVIAL_PATTERN = re.compile(
    r"^(?:hi|hello|hey)(?:\s+jupiter)?[!.?]*$|"
    r"^how are you[?.!]*$|"
    r"\breply (?:with )?exactly\b|"
    r"\b(?:metric|mocked failure|ui|wt) (?:test|verification)\b",
    re.IGNORECASE,
)


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _canonical_key(title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    if normalized.startswith("jupiter "):
        tokens = [token for token in normalized.split() if token not in {"project", "development", "work", "app", "application"}]
        normalized = " ".join(tokens)
        if normalized == "jupiter":
            normalized = "jupiter ai"
    return normalized


def _topic_id(canonical_key: str) -> str:
    return "topic-" + hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:16]


def _meaningful(memory) -> bool:
    title = _clean(getattr(memory, "topic", ""), 120)
    summary = _clean(getattr(memory, "summary", ""), 600)
    attributes = getattr(memory, "attributes", {}) or {}
    if not title or not summary:
        return False
    if title.lower() in _TRIVIAL_TOPICS or _TRIVIAL_PATTERN.search(title) or _TRIVIAL_PATTERN.search(summary):
        return False
    if str(getattr(memory, "visibility", "")).upper() == "SECRET":
        return False
    if str(getattr(memory, "memory_type", "")).upper() == "SHORT_TERM":
        return False
    if bool(attributes.get("inferred")) or str(getattr(memory, "source", "")).lower() == "reflection":
        return False
    return len(_canonical_key(title)) >= 3


def _updated_value(memory) -> str:
    value = _clean(getattr(memory, "last_updated", "") or getattr(memory, "created_at", ""), 80)
    try:
        datetime.fromisoformat(value)
        return value
    except ValueError:
        return ""


def build_topics(memory_manager) -> list[dict]:
    overrides = memory_manager.get_state("topic_index_overrides", {}) or {}
    hidden = memory_manager.get_state("topic_index_hidden", {}) or {}
    groups: dict[str, list] = defaultdict(list)
    for memory in memory_manager.get_all():
        if _meaningful(memory):
            groups[_canonical_key(_clean(memory.topic, 120))].append(memory)

    topics: list[dict] = []
    for canonical_key, memories in groups.items():
        topic_id = _topic_id(canonical_key)
        if topic_id in hidden:
            continue
        title_counts = Counter(_clean(memory.topic, 120) for memory in memories)
        title = sorted(title_counts, key=lambda item: (-title_counts[item], -len(item), item.lower()))[0]
        ordered = sorted(memories, key=_updated_value, reverse=True)
        updated_at = max((_updated_value(memory) for memory in memories), default="")
        summary = _clean(ordered[0].summary, 420)
        domain_counts = Counter(_clean(getattr(memory, "domain", "GENERAL"), 50) for memory in memories)
        category = domain_counts.most_common(1)[0][0] if domain_counts else "GENERAL"

        subtopic_groups: dict[str, list] = defaultdict(list)
        for memory in ordered:
            label = _clean(getattr(memory, "category", ""), 80)
            if not label or label.lower() in {"general", title.lower()}:
                label = _clean(getattr(memory, "domain", "Context"), 80).replace("_", " ").title()
            subtopic_groups[label].append(memory)
        subtopics = []
        for label, members in subtopic_groups.items():
            summaries = []
            for member in members:
                item = _clean(member.summary, 280)
                if item and item not in summaries:
                    summaries.append(item)
            subtopics.append({
                "id": f"{topic_id}-" + hashlib.sha256(label.lower().encode("utf-8")).hexdigest()[:10],
                "title": label,
                "summary": " ".join(summaries[:2]),
                "updated_at": max((_updated_value(member) for member in members), default=""),
            })

        override = overrides.get(topic_id, {}) if isinstance(overrides, dict) else {}
        topics.append({
            "id": topic_id,
            "title": _clean(override.get("title") or title, 120),
            "summary": _clean(override.get("summary") or summary, 600),
            "category": category.replace("_", " ").title(),
            "updated_at": updated_at,
            "subtopics": sorted(subtopics, key=lambda item: item["updated_at"], reverse=True),
            "memory_count": len(memories),
            "edited": bool(override),
        })
    return sorted(topics, key=lambda item: item["updated_at"], reverse=True)


def edit_topic(memory_manager, topic_id: str, title: str, summary: str) -> dict:
    current = next((topic for topic in build_topics(memory_manager) if topic["id"] == topic_id), None)
    if current is None:
        raise KeyError("Topic not found")
    clean_title = _clean(title, 120)
    clean_summary = _clean(summary, 600)
    if not clean_title or not clean_summary:
        raise ValueError("Topic title and summary are required.")
    overrides = memory_manager.get_state("topic_index_overrides", {}) or {}
    overrides[topic_id] = {
        "title": clean_title,
        "summary": clean_summary,
        "updated_at": datetime.now().isoformat(),
    }
    memory_manager.set_state("topic_index_overrides", overrides)
    return next(topic for topic in build_topics(memory_manager) if topic["id"] == topic_id)


def delete_topic(memory_manager, topic_id: str) -> bool:
    current = next((topic for topic in build_topics(memory_manager) if topic["id"] == topic_id), None)
    if current is None:
        raise KeyError("Topic not found")
    hidden = memory_manager.get_state("topic_index_hidden", {}) or {}
    hidden[topic_id] = {"deleted_at": datetime.now().isoformat(), "source_memories_preserved": True}
    memory_manager.set_state("topic_index_hidden", hidden)
    return True
