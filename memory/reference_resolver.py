"""Resolve personal references against bounded, user-scoped persistent memory."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass


_PERSONAL_MARKERS = frozenset({
    "my", "mine", "our", "mera", "meri", "mere", "apna", "apni", "apne",
})
_PAST_MARKERS = re.compile(r"(?i)\b(?:former|previous|old|past|used\s+to|was|tha|thi|the)\b")


def _ascii_words(value: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.findall(r"[a-z0-9]+", folded.casefold())


def _signature(value: str) -> str:
    """Build a generic consonant abbreviation; no relation-specific aliases."""

    words = _ascii_words(value)
    compact = "".join(words)
    if not compact:
        return ""
    consonants = compact[0] + re.sub(r"[aeiou]", "", compact[1:])
    return re.sub(r"(.)\1+", r"\1", consonants)


def _candidate_key(memory_id: str, entity: str) -> str:
    digest = hashlib.sha256(f"{memory_id}\0{entity}".encode("utf-8")).hexdigest()[:12]
    return f"REF_{digest}".upper()


@dataclass(frozen=True)
class PersonalReferenceCandidate:
    key: str
    memory_id: str
    relation: str
    entity: str
    confidence: float
    status: str


@dataclass(frozen=True)
class PersonalReferenceResolution:
    state: str = "none"
    relation: str = ""
    entity: str = ""
    confidence: float = 0.0
    memory_id: str = ""
    candidates: tuple[PersonalReferenceCandidate, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.state == "resolved" and bool(self.entity)


class PersonalReferenceResolver:
    """Match possessive references to structured active-memory relationships."""

    @staticmethod
    def _relation_entity(memory) -> tuple[str, str, str]:
        attributes = dict(getattr(memory, "attributes", {}) or {})
        relation = str(
            attributes.get("relation_to_user")
            or attributes.get("relationship")
            or attributes.get("user_relation")
            or ""
        ).strip()
        entity = str(
            attributes.get("entity")
            or attributes.get("person_name")
            or attributes.get("organization")
            or attributes.get("device_name")
            or ""
        ).strip()
        status = str(attributes.get("relationship_status") or attributes.get("status") or "current").casefold()

        for fact in getattr(memory, "facts", []) or []:
            if not isinstance(fact, dict):
                continue
            predicate = str(fact.get("predicate") or "").casefold()
            if predicate == "relationship_to_user":
                entity = entity or str(fact.get("subject") or "").strip()
                relation = relation or str(fact.get("object") or "").strip()

        summary = str(getattr(memory, "summary", "") or "")
        if not relation:
            match = re.search(r"(?i)\buser(?:'s|’s)\s+([a-z][a-z -]{1,40})", summary)
            relation = match.group(1).strip(" .") if match else ""
        if not entity and relation:
            topic = str(getattr(memory, "topic", "") or "").strip()
            if topic and topic.casefold() not in {relation.casefold(), "important life event"}:
                entity = topic
        return relation, entity, status

    @classmethod
    def candidates(
        cls,
        message: str,
        memories: list,
        *,
        require_personal_marker: bool = True,
    ) -> list[PersonalReferenceCandidate]:
        query_words = set(_ascii_words(message))
        if require_personal_marker and not query_words.intersection(_PERSONAL_MARKERS):
            return []
        asks_past = bool(_PAST_MARKERS.search(str(message or "")))
        ranked: list[PersonalReferenceCandidate] = []
        for memory in memories or []:
            relation, entity, status = cls._relation_entity(memory)
            if not relation or not entity:
                continue
            if status in {"past", "former", "historical", "expired", "superseded"} and not asks_past:
                continue
            relation_words = set(_ascii_words(relation))
            relation_forms = set(relation_words)
            relation_forms.add(_signature(relation))
            if len(relation_words) > 1:
                relation_forms.add("".join(word[0] for word in relation_words if word))
            hits = {word for word in query_words if word in relation_forms or _signature(word) in relation_forms}
            if require_personal_marker and not hits:
                continue
            confidence = min(1.0, max(0.0, float(getattr(memory, "confidence", 0.5) or 0.5)))
            attributes = dict(getattr(memory, "attributes", {}) or {})
            confidence += 0.04 if status in {"current", "active"} else 0.0
            confidence += 0.03 if attributes.get("user_confirmed") else 0.0
            ranked.append(PersonalReferenceCandidate(
                key=_candidate_key(str(getattr(memory, "memory_id", "")), entity),
                memory_id=str(getattr(memory, "memory_id", "")),
                relation=relation,
                entity=entity,
                confidence=min(1.0, confidence),
                status=status,
            ))
        ranked.sort(key=lambda item: (item.confidence, item.status in {"current", "active"}), reverse=True)
        return ranked[:8]

    @classmethod
    def resolve(
        cls,
        message: str,
        memories: list,
        *,
        selected_key: str = "",
    ) -> PersonalReferenceResolution:
        candidates = cls.candidates(
            message,
            memories,
            require_personal_marker=not bool(selected_key),
        )
        if selected_key:
            selected = next((item for item in candidates if item.key == selected_key.upper()), None)
            if selected:
                return PersonalReferenceResolution(
                    "resolved", selected.relation, selected.entity, selected.confidence,
                    selected.memory_id, tuple(candidates),
                )
        if not candidates:
            return PersonalReferenceResolution()
        top = candidates[0]
        competing = [
            item for item in candidates[1:]
            if item.entity.casefold() != top.entity.casefold()
            and item.relation.casefold() == top.relation.casefold()
            and top.confidence - item.confidence < 0.08
        ]
        if competing:
            return PersonalReferenceResolution(
                "ambiguous", top.relation, confidence=top.confidence,
                candidates=tuple([top, *competing][:8]),
            )
        return PersonalReferenceResolution(
            "resolved", top.relation, top.entity, top.confidence,
            top.memory_id, tuple(candidates),
        )
