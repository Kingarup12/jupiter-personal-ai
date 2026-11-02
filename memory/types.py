"""Memory categories and lifetime helpers used across Jupiter."""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta
from enum import StrEnum

from config.settings import SHORT_TERM_MEMORY_DAYS


class MemoryType(StrEnum):
    """The cognitive role of a memory, not merely its topic."""

    PERSONALITY = "PERSONALITY"
    SEMANTIC = "SEMANTIC"
    EPISODIC = "EPISODIC"
    SHORT_TERM = "SHORT_TERM"
    WORKING = "WORKING"
    ARCHIVE = "ARCHIVE"


class MemoryTier(StrEnum):
    CORE_ACTIVE = "P0"
    ACTIVE_LONG_TERM = "P1"
    PERMANENT_ARCHIVE = "P2"
    EPISODIC_TIME_AWARE = "P3"
    WORKING_RECENT = "P4"


class MemoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    HISTORICAL = "HISTORICAL"
    SUPERSEDED = "SUPERSEDED"
    DISPUTED = "DISPUTED"
    EXPIRED = "EXPIRED"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"


class RetentionMode(StrEnum):
    THREE_CALENDAR_MONTHS = "THREE_CALENDAR_MONTHS"
    DEFAULT_90_DAYS = "DEFAULT_90_DAYS"
    FIXED_DURATION = "FIXED_DURATION"
    UNTIL_DATE = "UNTIL_DATE"
    UNTIL_EVENT = "UNTIL_EVENT"
    UNTIL_RESOLVED = "UNTIL_RESOLVED"
    UNTIL_EVENT_PLUS_GRACE = "UNTIL_EVENT_PLUS_GRACE"
    CUSTOM = "CUSTOM"


class MemoryDomain(StrEnum):
    """What a memory is about, independent of its cognitive lifetime."""

    GENERAL = "GENERAL"
    PROFILE = "PROFILE"
    SEMANTIC = "SEMANTIC"
    EPISODIC = "EPISODIC"
    SHORT_TERM = "SHORT_TERM"
    PROJECT = "PROJECT"
    GOAL = "GOAL"
    RELATIONSHIP = "RELATIONSHIP"
    DEVICE = "DEVICE"
    PREFERENCE = "PREFERENCE"
    EMOTION = "EMOTION"
    SKILL = "SKILL"
    HEALTH = "HEALTH"
    WORK = "WORK"
    EDUCATION = "EDUCATION"
    LIFE = "LIFE"
    LANGUAGE_LEARNING = "LANGUAGE_LEARNING"


PERSISTENT_MEMORY_TYPES = {
    MemoryType.PERSONALITY,
    MemoryType.SEMANTIC,
}


def normalize_memory_type(value: object) -> str:
    """Return a safe type for imperfect LLM output."""

    if not isinstance(value, str):
        return MemoryType.SEMANTIC.value

    normalized = value.upper().strip().replace("-", "_").replace(" ", "_")
    if normalized in MemoryType._value2member_map_:
        return normalized
    return MemoryType.SEMANTIC.value


def normalize_domain(value: object, category: str = "", memory_type: str = "") -> str:
    """Return a safe domain and infer one when older data has none."""

    if isinstance(value, str):
        normalized = value.upper().strip().replace("-", "_").replace(" ", "_")
        if normalized in MemoryDomain._value2member_map_:
            return normalized

    category_key = category.lower().strip()
    category_domains = {
        "identity": MemoryDomain.PROFILE.value,
        "profile": MemoryDomain.PROFILE.value,
        "relationships": MemoryDomain.RELATIONSHIP.value,
        "relationship": MemoryDomain.RELATIONSHIP.value,
        "projects": MemoryDomain.PROJECT.value,
        "project": MemoryDomain.PROJECT.value,
        "goals": MemoryDomain.GOAL.value,
        "goal": MemoryDomain.GOAL.value,
        "devices": MemoryDomain.DEVICE.value,
        "device": MemoryDomain.DEVICE.value,
        "preferences": MemoryDomain.PREFERENCE.value,
        "preference": MemoryDomain.PREFERENCE.value,
        "emotions": MemoryDomain.EMOTION.value,
        "emotion": MemoryDomain.EMOTION.value,
        "skills": MemoryDomain.SKILL.value,
        "skill": MemoryDomain.SKILL.value,
        "health": MemoryDomain.HEALTH.value,
        "work": MemoryDomain.WORK.value,
        "career": MemoryDomain.WORK.value,
        "education": MemoryDomain.EDUCATION.value,
        "life chapter": MemoryDomain.LIFE.value,
        "life milestone": MemoryDomain.LIFE.value,
    }
    if category_key in category_domains:
        return category_domains[category_key]

    normalized_type = normalize_memory_type(memory_type)
    if normalized_type == MemoryType.PERSONALITY.value:
        return MemoryDomain.PROFILE.value
    if normalized_type in {MemoryType.EPISODIC.value, MemoryType.SHORT_TERM.value}:
        return normalized_type
    return MemoryDomain.GENERAL.value


def retention_label(memory_type: str) -> str:
    """Use one canonical, human-readable retention label per memory type."""

    normalized = normalize_memory_type(memory_type)
    if normalized in {
        MemoryType.PERSONALITY.value,
        MemoryType.SEMANTIC.value,
        MemoryType.ARCHIVE.value,
    }:
        return "Forever"
    if normalized == MemoryType.EPISODIC.value:
        return "Three Calendar Months"
    if normalized == MemoryType.SHORT_TERM.value:
        return "20 Days"
    return "Session"


def expiry_for(memory_type: str, created_at: datetime) -> str | None:
    """Return the ISO expiry timestamp, or ``None`` for permanent memory."""

    normalized = normalize_memory_type(memory_type)
    if normalized == MemoryType.EPISODIC.value:
        return add_calendar_months(created_at, 3).isoformat()
    if normalized == MemoryType.SHORT_TERM.value:
        return (created_at + timedelta(days=SHORT_TERM_MEMORY_DAYS)).isoformat()
    if normalized == MemoryType.WORKING.value:
        return created_at.isoformat()
    return None


def retention_expiry(
    mode: RetentionMode | str,
    created_at: datetime,
    *,
    duration_days: int | None = None,
    until_date: datetime | None = None,
) -> str | None:
    """Resolve only deterministic expiry modes; event modes remain open."""

    normalized = RetentionMode(str(mode))
    if normalized in {
        RetentionMode.THREE_CALENDAR_MONTHS,
        RetentionMode.DEFAULT_90_DAYS,
    }:
        return add_calendar_months(created_at, 3).isoformat()
    if normalized == RetentionMode.FIXED_DURATION:
        if duration_days is None or duration_days < 1:
            raise ValueError("FIXED_DURATION requires a positive duration_days value.")
        return (created_at + timedelta(days=duration_days)).isoformat()
    if normalized == RetentionMode.UNTIL_DATE:
        if until_date is None or until_date <= created_at:
            raise ValueError("UNTIL_DATE requires a future until_date value.")
        return until_date.isoformat()
    # Event/resolution/custom policies are reviewed by the deterministic event
    # scheduler and are intentionally not converted to an arbitrary date here.
    return None


def add_calendar_months(value: datetime, months: int) -> datetime:
    """Advance by whole calendar months while preserving local wall-clock time.

    Month-end dates are clamped to the final day of the destination month, so
    January 31 + three months becomes April 30 rather than a fixed-day offset.
    """

    if months < 0:
        raise ValueError("months must be non-negative")
    month_index = (value.month - 1) + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)
