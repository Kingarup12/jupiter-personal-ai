"""User-scoped memory confidence, review queue, and training feedback."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from memory.schema import Memory
from memory.types import MemoryTier, add_calendar_months, expiry_for, retention_label
from storage.sqlite_store import SQLiteStore


_EXPLICIT_PERMANENT = re.compile(r"(?i)\b(remember|save)\b.{0,30}\b(permanently|forever|always)\b")
_EXPLICIT_SAVE = re.compile(r"(?i)\b(?:please\s+)?(?:remember|save)\b")
_EXPLICIT_DONT_SAVE = re.compile(
    r"(?i)\b(?:(?:don['’]?t|do not|never)\s+(?:remember|save)|"
    r"keep\s+(?:this|that|it)\s+only\s+in\s+(?:this|the)\s+(?:chat|conversation))\b"
)


TIER_PRESENTATION = {
    "P0": {"label": "Core Permanent", "description": "Durable user facts with broad future impact."},
    "P1": {"label": "Long-term", "description": "Active durable information Jupiter should keep available."},
    "P2": {"label": "Archive", "description": "Permanent archive recalled only when the user explicitly asks."},
    "P3": {"label": "Episode", "description": "Three-month archive recalled only when the user explicitly asks."},
    "P4": {"label": "Recent", "description": "Working or current-chat context."},
    "DONT_SAVE": {"label": "Don't save", "description": "Keep this out of long-term memory."},
}


class MemoryReviewService:
    def __init__(self, user_id: str, store: SQLiteStore | None = None):
        self.user_id = str(user_id or "default")
        self.store = store or SQLiteStore(user_id=self.user_id)
        self._owns_store = store is None

    @staticmethod
    def confidence_state(confidence: float) -> str:
        if confidence >= 0.8:
            return "confident"
        if confidence >= 0.65:
            return "reasonably_confident"
        return "unsure"

    @staticmethod
    def proposed_tier(memory: Memory) -> str:
        memory_type = str(memory.memory_type or "").upper()
        domain = str(memory.domain or "").upper()
        if domain == "PROFILE":
            return MemoryTier.CORE_ACTIVE.value
        if memory_type == "EPISODIC":
            return MemoryTier.EPISODIC_TIME_AWARE.value
        if memory_type in {"SHORT_TERM", "WORKING"}:
            return MemoryTier.WORKING_RECENT.value
        if memory_type == "ARCHIVE":
            return MemoryTier.PERMANENT_ARCHIVE.value
        return MemoryTier.ACTIVE_LONG_TERM.value

    @staticmethod
    def apply_tier(memory: Memory, tier: str, *, user_confirmed: bool = False, comment: str = "") -> Memory:
        memory.attributes = dict(memory.attributes or {})
        memory.attributes.update({
            "user_confirmed": bool(user_confirmed),
            "memory_tier": tier,
            **({"review_comment": comment[:1000]} if comment else {}),
        })
        tier_settings = {
            "P0": ("Critical", "SEMANTIC"),
            "P1": ("High", "SEMANTIC"),
            "P2": ("Medium", "ARCHIVE"),
            "P3": ("Medium", "EPISODIC"),
            "P4": ("Low", "WORKING"),
        }
        memory.importance, memory.memory_type = tier_settings[tier]
        memory.retention = retention_label(memory.memory_type)
        memory.expires_at = expiry_for(memory.memory_type, datetime.now())
        duration = re.search(r"(?i)\b(?:for\s+)?(\d{1,4})\s*(day|week|month)s?\b", comment)
        if duration:
            count = int(duration.group(1))
            unit = duration.group(2).casefold()
            if unit == "month":
                memory.retention = f"{count} Calendar Month{'s' if count != 1 else ''}"
                memory.expires_at = add_calendar_months(datetime.now(), count).isoformat()
                memory.attributes["custom_retention_months"] = count
            else:
                days = count * {"day": 1, "week": 7}[unit]
                memory.retention = f"{days} Days"
                memory.expires_at = (datetime.now() + timedelta(days=days)).isoformat()
                memory.attributes["custom_retention_days"] = days
        if re.search(r"(?i)\bask me again next month\b", comment):
            memory.attributes["review_after"] = add_calendar_months(datetime.now(), 1).date().isoformat()
            memory.attributes["review_reason"] = "Reviewer requested a natural follow-up next month."
        return memory

    @staticmethod
    def _feedback_from_comment(comment: str) -> dict[str, Any]:
        normalized = " ".join(str(comment or "").split())
        duration = re.search(r"(?i)\b(?:for\s+)?(\d{1,4})\s*(day|week|month)s?\b", normalized)
        review_after = (
            add_calendar_months(datetime.now(), 1).date().isoformat()
            if re.search(r"(?i)\bask me again next month\b", normalized)
            else None
        )
        return {
            "duration": (
                f"{duration.group(1)} {duration.group(2).lower()}"
                f"{'' if duration.group(1) == '1' else 's'}"
                if duration else None
            ),
            "review_after": review_after,
            "promotion_hint": normalized[:500] if re.search(r"(?i)\bpromote|stronger memory\b", normalized) else None,
            "demotion_hint": normalized[:500] if re.search(r"(?i)\bdemote|temporary|short[- ]term\b", normalized) else None,
            "explicit_exception": bool(re.search(r"(?i)\b(?:only|unless|except|temporary)\b", normalized)),
        }

    @staticmethod
    def _candidate_payload(memory: Memory, tier: str) -> dict[str, Any]:
        payload = asdict(memory)
        attributes = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
        transcript_provenance = (
            attributes.get("transcript_provenance")
            if isinstance(attributes.get("transcript_provenance"), dict)
            else {}
        )
        payload["structured_metadata"] = {
            "subject": attributes.get("subject") or memory.topic,
            "relationship": attributes.get("relationship"),
            "fact": memory.summary,
            "confidence": float(memory.confidence),
            "timestamp": memory.created_at,
            "source": memory.source,
            "memory_level": tier,
            "review_date": attributes.get("review_date"),
            "expiration_or_reevaluation": memory.expires_at or attributes.get("reevaluation_rule"),
            "uncertainty": max(
                1.0 - float(memory.confidence),
                float(transcript_provenance.get("uncertainty") or 0.0),
            ),
            "user_confirmed": bool(attributes.get("user_confirmed", False)),
            "transcript_provenance": transcript_provenance,
        }
        return payload

    @staticmethod
    def _strong_contradiction(memory: Memory, existing_memories: list[Memory]) -> tuple[bool, list[str]]:
        """Detect opposing facts, not merely different wording under one topic."""

        def normalized(value: Any) -> str:
            return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))

        def structured(memory_item: Memory) -> list[tuple[str, str]]:
            pairs: list[tuple[str, str]] = []
            # Device statements such as "my phone is a Pixel" naturally use
            # different entity values for each update.  Compare them by their
            # stable device topic so a replacement can be reviewed as a
            # contradiction rather than treated as an unrelated fact.
            if str(memory_item.domain or "").upper() == "DEVICE":
                attributes = memory_item.attributes if isinstance(memory_item.attributes, dict) else {}
                entity = normalized(attributes.get("entity") or memory_item.summary)
                if entity:
                    return [(f"device:{normalized(memory_item.topic)}", entity)]
            for fact in memory_item.facts or []:
                if not isinstance(fact, dict):
                    continue
                subject = normalized(fact.get("subject") or memory_item.attributes.get("subject") or "user")
                predicate = normalized(fact.get("predicate") or fact.get("relationship"))
                value = normalized(fact.get("object") or fact.get("value") or fact.get("fact"))
                if predicate and value:
                    pairs.append((f"{subject}:{predicate}", value))
            attributes = memory_item.attributes if isinstance(memory_item.attributes, dict) else {}
            predicate = normalized(attributes.get("predicate") or attributes.get("relationship"))
            value = normalized(attributes.get("object") or attributes.get("value"))
            if predicate and value:
                pairs.append((f"{normalized(attributes.get('subject') or 'user')}:{predicate}", value))
            if pairs:
                return pairs

            summary = normalized(memory_item.summary)
            patterns = (
                (r"^(?:the )?user(?:s)? (?:name is|is named|prefers to be called) (.+)$", "user:name"),
                (r"^(?:the )?user (?:lives|resides|is based) in (.+)$", "user:residence"),
                (r"^(?:the )?user prefers (.+)$", "user:preference"),
                (r"^(?:my|the user s) ([a-z0-9 ]{2,40}) is (.+)$", "user:"),
            )
            for pattern, key in patterns:
                match = re.match(pattern, summary)
                if not match:
                    continue
                if key.endswith(":"):
                    return [(key + normalized(match.group(1)), normalized(match.group(2)))]
                return [(key, normalized(match.group(1)))]
            return []

        incoming = structured(memory)
        if not incoming:
            return False, []
        relevant_ids: list[str] = []
        contradiction = False
        incoming_by_key = dict(incoming)
        for item in existing_memories:
            if item.memory_id == memory.memory_id or float(item.confidence or 0) < 0.75:
                continue
            for key, existing_value in structured(item):
                incoming_value = incoming_by_key.get(key)
                if incoming_value is None:
                    continue
                relevant_ids.append(item.memory_id)
                # Identical and contained values are paraphrases/refinements, not contradictions.
                if (
                    incoming_value == existing_value
                    or incoming_value in existing_value
                    or existing_value in incoming_value
                ):
                    continue
                contradiction = True
        return contradiction, list(dict.fromkeys(relevant_ids))[:5]

    def compact_policy(self, candidate_text: str = "", limit: int = 8) -> dict[str, Any]:
        """Return compact aggregate policy plus only relevant prior examples."""
        feedback = self.store.memory_review_feedback(limit=200)
        tier_counts: dict[str, int] = {}
        words = {word for word in re.findall(r"[a-z0-9]{3,}", candidate_text.casefold())}
        relevant = []
        for item in feedback:
            tier = str(item.get("selected_tier") or "")
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            fact_words = set(re.findall(r"[a-z0-9]{3,}", str(item.get("candidate_fact") or "").casefold()))
            if words and words.intersection(fact_words):
                relevant.append({
                    "selected_tier": tier,
                    "comment": str(item.get("custom_comment") or "")[:300],
                    "duration": item.get("duration_hint"),
                    "review_after": item.get("review_after"),
                    "promotion_hint": item.get("promotion_hint"),
                    "demotion_hint": item.get("demotion_hint"),
                    "explicit_exception": bool(item.get("explicit_exception")),
                })
        return {"tier_counts": tier_counts, "relevant_examples": relevant[:limit]}

    def evaluate(
        self,
        memory: Memory,
        existing_memories: list[Memory],
        *,
        source_chat_id: str | None,
        source_message_id: str | None,
        source_text: str,
    ) -> dict[str, Any]:
        if str(memory.memory_type or "").upper() == "PERSONALITY":
            return {"action": "personality", "tier": None, "confidence_state": "confident"}
        if _EXPLICIT_DONT_SAVE.search(source_text):
            return {"action": "dont_save", "tier": MemoryTier.WORKING_RECENT.value}
        tier = self.proposed_tier(memory)
        personalized_policy = self.compact_policy(memory.summary)
        prior_choices = [
            item.get("selected_tier")
            for item in personalized_policy["relevant_examples"]
            if item.get("selected_tier") in TIER_PRESENTATION
        ]
        if prior_choices:
            tier = max(set(prior_choices), key=prior_choices.count)
            if tier == "DONT_SAVE":
                return {"action": "dont_save", "tier": MemoryTier.WORKING_RECENT.value}
        relevant_feedback = personalized_policy["relevant_examples"]
        if any(item.get("promotion_hint") for item in relevant_feedback):
            tier = {"P4": "P3", "P3": "P2", "P2": "P1"}.get(tier, tier)
        if any(item.get("demotion_hint") for item in relevant_feedback):
            tier = {"P0": "P1", "P1": "P3", "P2": "P3", "P3": "P4"}.get(tier, tier)
        learned_duration = next(
            (str(item.get("duration") or "") for item in relevant_feedback if item.get("duration")),
            "",
        )
        confidence = min(1.0, max(0.0, float(memory.confidence or memory.certainty or 0.5)))
        attributes = memory.attributes if isinstance(memory.attributes, dict) else {}
        transcript_provenance = (
            attributes.get("transcript_provenance")
            if isinstance(attributes.get("transcript_provenance"), dict)
            else {}
        )
        transcript_confidence = transcript_provenance.get("confidence")
        low_confidence_transcript = bool(
            attributes.get("uncertain")
            or attributes.get("uncertainty_reason")
            or transcript_provenance.get("uncertain")
            or (
                isinstance(transcript_confidence, (int, float))
                and float(transcript_confidence) < 0.78
            )
        )
        if low_confidence_transcript:
            confidence = min(confidence, 0.69)
        state = self.confidence_state(confidence)
        contradiction, relevant_ids = self._strong_contradiction(memory, existing_memories)
        explicit_permanent = bool(_EXPLICIT_PERMANENT.search(source_text))
        explicit_save = bool(_EXPLICIT_SAVE.search(source_text))
        needs_review = (
            low_confidence_transcript
            or (state == "unsure" and not explicit_save)
            or contradiction
            or (
                tier == MemoryTier.CORE_ACTIVE.value
                and confidence < 0.9
                and not (explicit_permanent or explicit_save)
            )
        )
        if not needs_review:
            if tier == MemoryTier.WORKING_RECENT.value:
                return {"action": "dont_save", "tier": tier, "confidence_state": state}
            return {
                "action": "save",
                "tier": tier,
                "confidence_state": state,
                "policy_comment": learned_duration,
            }
        # Marker 221 (real owner queue: two identical "This is a friend from my college." cards): one card per question.
        folded = " ".join(str(memory.summary or "").casefold().split())
        try:
            pending = self.pending()
        except Exception:
            pending = []
        if any(" ".join(str(item.get("candidate_fact") or "").casefold().split()) == folded for item in pending):
            return {"action": "dont_save", "tier": tier, "confidence_state": state, "duplicate_review": True}
        reason = (
            "This may conflict with a trusted existing memory."
            if contradiction else
            "This may affect Jupiter's core understanding of you."
            if tier == MemoryTier.CORE_ACTIVE.value else
            "Jupiter is not confident enough to choose a memory level automatically."
        )
        item = {
            "id": str(uuid4()),
            "user_id": self.user_id,
            "source_chat_id": source_chat_id,
            "source_message_id": source_message_id,
            "candidate_fact": memory.summary,
            "candidate": self._candidate_payload(memory, tier),
            "proposed_tier": tier,
            "confidence": confidence,
            "confidence_state": state,
            "reason_for_review": reason,
            "relevant_existing_memory_ids": relevant_ids,
            "contradiction": contradiction,
            "created_at": datetime.now().isoformat(),
            "personalized_policy": personalized_policy,
        }
        item["id"] = self.store.enqueue_memory_review(item) or item["id"]
        return {"action": "review", "item": item, "tier": tier, "confidence_state": state}

    def pending(self) -> list[dict[str, Any]]:
        return self.store.list_memory_reviews("pending")

    def question_card(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item["id"],
            "kind": "memory_review",
            "title": "Jupiter needs clarification",
            "question": "Jupiter isn't fully sure how this should be remembered.",
            "detail": item.get("candidate_fact") or "",
            "reason": item.get("reason_for_review") or "",
            "actions": [
                {"value": value, **presentation}
                for value, presentation in TIER_PRESENTATION.items()
            ],
            "allow_comment": True,
            "status": item.get("status") or "pending",
            "selected_value": item.get("selected_tier"),
            "custom_comment": item.get("custom_comment") or "",
        }

    def resolve(self, review_id: str, selected_tier: str, custom_comment: str = "") -> dict[str, Any] | None:
        tier = str(selected_tier or "").upper()
        if tier not in TIER_PRESENTATION:
            raise ValueError("Unsupported memory review choice.")
        pending = self.store.get_memory_review(review_id)
        if not pending or pending.get("status") != "pending":
            return None
        resolved = self.store.resolve_memory_review(
            review_id,
            tier,
            custom_comment,
            feedback=self._feedback_from_comment(custom_comment),
        )
        if tier in {"DONT_SAVE", "P4"}:
            return resolved
        payload = pending.get("candidate") or {}
        allowed = set(Memory.__dataclass_fields__)
        memory = Memory(**{key: value for key, value in payload.items() if key in allowed})
        self.apply_tier(memory, tier, user_confirmed=True, comment=custom_comment)
        memory.confidence = max(float(memory.confidence), 0.9)
        from memory.service import MemoryService

        service = MemoryService(user_id=self.user_id)
        try:
            service.persist_extracted_memory(memory, allow_personality_updates=True)
        finally:
            service.close()
        return resolved

    def close(self) -> None:
        if self._owns_store:
            self.store.close()
