"""Safe, reusable continuity extraction for normal Jupiter conversations.

The extractor is deliberately deterministic: it only promotes explicit, durable
signals already recognised by Jupiter's memory rules.  That keeps a casual
sentence, a one-off request, or an untrusted attempt to redefine Jupiter from
becoming permanent user state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re
from typing import Iterable

from core.logger import Logger
from memory.intent import MemoryIntentClassifier
from memory.service import is_core_identity_override_attempt


# Marker 221: the old free-form analyzer candidate is replaced by the per-turn extractor (one model call, validated).
MODEL_ASSISTED_CANDIDATES = False

TRIVIAL_TOPICS = {
    "general", "greeting", "small talk", "conversation", "test", "testing",
    "verification", "question or doubt", "everyday conversation",
}


@dataclass
class ExtractionCandidate:
    kind: str
    action: str
    confidence: float
    source_chat_id: str | None
    source_message_id: str | None
    memory: object | None = None
    instruction: str | None = None
    reason: str = ""

    def report(self) -> dict:
        base = {
            "kind": self.kind,
            "action": self.action,
            "confidence": self.confidence,
            "source_chat_id": self.source_chat_id,
            "source_message_id": self.source_message_id,
            "reason": self.reason,
        }
        if self.memory is not None:
            base.update({
                "memory_id": self.memory.memory_id,
                "title": self.memory.topic,
                "summary": self.memory.summary,
                "domain": self.memory.domain,
                "category": self.memory.category,
            })
        if self.instruction:
            base["instruction"] = self.instruction
        return base


class ContinuityExtractionPipeline:
    """Classify a completed normal turn and apply only approved candidate kinds."""

    def __init__(self):
        self.intent = MemoryIntentClassifier()

    @staticmethod
    def _action(memory, existing_memories: Iterable) -> str:
        memories = list(existing_memories or [])
        if any(
            str(item.summary).strip().lower() == str(memory.summary).strip().lower()
            for item in memories
        ):
            return "DUPLICATE"
        if any(
            item.memory_id == memory.memory_id
            or (
                str(item.topic).strip().lower() == str(memory.topic).strip().lower()
                and str(item.domain).upper() == str(memory.domain).upper()
            )
            for item in memories
        ):
            return "UPDATE"
        return "NEW"

    @staticmethod
    def _is_durable(memory) -> bool:
        # Short-lived questions and ordinary small talk remain conversation
        # state; they are neither historical Topics nor permanent memories.
        return (
            str((memory.attributes or {}).get("memory_tier") or "").upper() != "P4"
            and str(memory.memory_type).upper() not in {"WORKING", "SHORT_TERM"}
            and (str(memory.retention).lower() == "forever"
                 or str(memory.domain).upper() in {"PROFILE", "PREFERENCE", "GOAL", "LIFE", "RELATIONSHIP", "DEVICE"})
        )

    @staticmethod
    def _topic_candidate(memory) -> bool:
        title = " ".join(str(memory.topic or "").split())
        return (
            bool(title)
            and title.lower() not in TRIVIAL_TOPICS
            and len(title) >= 3
            and str(memory.memory_type).upper() != "SHORT_TERM"
        )

    @staticmethod
    def _atomic_memories(memory, transcript_provenance: dict | None = None) -> list:
        """Return small independently reviewable facts from a compound statement."""

        raw_provenance = dict(transcript_provenance or {})
        provenance = {
            key: raw_provenance[key]
            for key in ("source", "confidence", "uncertain", "completion_reason")
            if raw_provenance.get(key) not in (None, "")
        }
        if isinstance(raw_provenance.get("corrections"), list) and raw_provenance["corrections"]:
            provenance["correction_count"] = min(len(raw_provenance["corrections"]), 99)
        base_attributes = dict(memory.attributes or {})
        if provenance:
            base_attributes["transcript_provenance"] = provenance

        fact_rows = [fact for fact in (memory.facts or []) if isinstance(fact, dict)]
        if len(fact_rows) > 1:
            results = []
            for fact in fact_rows:
                summary = str(fact.get("fact") or fact.get("summary") or fact.get("object") or memory.summary).strip()
                digest = hashlib.sha1(summary.casefold().encode("utf-8")).hexdigest()[:10]
                results.append(replace(
                    memory,
                    memory_id=f"{memory.memory_id}.{digest}",
                    summary=summary,
                    facts=[fact],
                    attributes={**base_attributes, "atomic_fact": True},
                ))
            return results

        summary = " ".join(str(memory.summary or "").split())
        uncertain = bool(re.search(
            r"(?i)\b(?:may|might|maybe|possibly|perhaps|guess|unsure|uncertain|"
            r"not sure|tentative|hope to|plan to)\b",
            summary,
        ))
        # Split only durable compound statements; ordinary phrases containing
        # "and" remain intact unless both sides look like independent clauses.
        pieces = [part.strip(" ,.;") for part in re.split(r"(?i)[.;]+|\b(?:and then|then)\b", summary) if part.strip(" ,.;")]
        if len(pieces) == 1:
            and_parts = [part.strip(" ,.;") for part in re.split(r"(?i)\band\b", summary) if part.strip(" ,.;")]
            if len(and_parts) > 1 and all(len(part.split()) >= 3 for part in and_parts):
                pieces = and_parts
        if len(pieces) <= 1:
            return [replace(
                memory,
                confidence=min(float(memory.confidence), 0.74) if uncertain else memory.confidence,
                certainty=min(float(memory.certainty), 0.74) if uncertain else memory.certainty,
                attributes={
                    **base_attributes,
                    "uncertain": uncertain or bool(provenance.get("uncertain")),
                },
            )]

        modal_match = re.search(r"(?i)\b(may|might|plan to|hope to)\b", pieces[0])
        modal = modal_match.group(1).lower() if modal_match else ""
        results = []
        for piece in pieces:
            atomic = piece
            if not re.match(r"(?i)^(?:i|my|we|the user)\b", atomic):
                atomic = f"I {modal + ' ' if modal else ''}{atomic}"
            digest = hashlib.sha1(atomic.casefold().encode("utf-8")).hexdigest()[:10]
            results.append(replace(
                memory,
                memory_id=f"{memory.memory_id}.{digest}",
                summary=atomic,
                facts=[{
                    "subject": "user",
                    "predicate": "shared_" + "_".join(
                        word for word in str(memory.topic or "fact").casefold().split() if word.isalnum()
                    )[:60],
                    "object": atomic,
                    "confidence": float(memory.certainty),
                }],
                confidence=min(float(memory.confidence), 0.74) if uncertain else memory.confidence,
                certainty=min(float(memory.certainty), 0.74) if uncertain else memory.certainty,
                attributes={
                    **base_attributes,
                    "atomic_fact": True,
                    "uncertain": uncertain or bool(provenance.get("uncertain")),
                },
            ))
        return results

    def extract_turn(
        self,
        user_message: str,
        recent_conversation: list[dict] | None,
        existing_memories: Iterable,
        *,
        chat_id: str | None = None,
        message_id: str | None = None,
        is_testing: bool = False,
        model_assisted: bool = False,
        transcript_provenance: dict | None = None,
    ) -> tuple[list[ExtractionCandidate], list[ExtractionCandidate]]:
        """Return ``(candidates, ignored)`` without writing persistent state."""

        source = {"source_chat_id": chat_id, "source_message_id": message_id}
        if is_testing:
            return [], [ExtractionCandidate("ignored", "IGNORE", 1.0, **source, reason="Testing Mode never persists extraction output.")]
        if is_core_identity_override_attempt(user_message):
            return [], [ExtractionCandidate("ignored", "IGNORE", 1.0, **source, reason="Protected identity/security override attempt.")]
        from brain.conversation import temporary_speaker_possible
        if temporary_speaker_possible([*(recent_conversation or []), {"role": "user", "content": user_message}]):
            return [], [ExtractionCandidate("ignored", "IGNORE", 1.0, **source,
                        reason="Temporary participant attribution is uncertain; keep this turn conversation-local.")]

        clean_recent_conversation = [
            item for item in (recent_conversation or [])
            if isinstance(item, dict) and not item.get("_synthetic") and item.get("role") == "user"
        ]
        memory = self.intent.classify(user_message, clean_recent_conversation)
        from memory.intent import regex_memory_allowed
        if memory is not None and not regex_memory_allowed(memory):
            memory = None                        # marker 221: durable facts come from memory/turn_memory.py
        candidate_reason = "Explicit durable user statement."
        if memory is None and model_assisted and MODEL_ASSISTED_CANDIDATES:
            try:
                from memory.analyzer import MemoryAnalyzer

                memory = MemoryAnalyzer().analyze(user_message, clean_recent_conversation)
                candidate_reason = "Model-assisted durable memory candidate."
            except Exception as error:
                Logger.warning(f"Model-assisted memory candidate detection skipped: {error}")
        if memory is None or not self._is_durable(memory):
            return [], [ExtractionCandidate("ignored", "IGNORE", 0.0, **source, reason="No clear durable user signal.")]

        candidates = []
        for atomic_memory in self._atomic_memories(memory, transcript_provenance):
            action = self._action(atomic_memory, existing_memories)
            atomic_memory.source = "conversation:{}:{}".format(chat_id or "unknown", message_id or "unknown")
            confidence = min(1.0, max(0.0, float(atomic_memory.confidence)))
            candidates.append(ExtractionCandidate(
                "memory", action, confidence, **source,
                memory=atomic_memory, reason=candidate_reason,
            ))
            if atomic_memory.memory_id.startswith("identity.name") or atomic_memory.domain == "PROFILE":
                candidates.append(ExtractionCandidate(
                    "profile", action, confidence, **source, memory=atomic_memory,
                    reason="Explicit preferred/formal name signal.",
                ))
            if atomic_memory.memory_type == "PERSONALITY":
                candidates.append(ExtractionCandidate(
                    "personality", action, confidence,
                    **source, memory=atomic_memory, instruction=atomic_memory.summary,
                    reason="Explicit stable communication preference.",
                ))
            if self._topic_candidate(atomic_memory):
                candidates.append(ExtractionCandidate(
                    "topic", "UPDATE" if action == "UPDATE" else "NEW",
                    confidence, **source, memory=atomic_memory,
                    reason="Meaningful durable subject; Topics remain derived from preserved memories.",
                ))
        return candidates, []

    def apply_normal_turn(self, memory_service, candidates, *, allow_auto_updates: bool) -> int:
        """Persist a normal-turn candidate set after the reply has been returned."""

        applied_ids = set()
        changed = 0
        for candidate in candidates:
            if candidate.kind not in {"memory", "profile", "personality"} or candidate.memory is None:
                continue
            if candidate.memory.memory_id in applied_ids:
                continue
            is_personality = candidate.memory.memory_type == "PERSONALITY"
            if is_personality and not allow_auto_updates:
                continue
            if memory_service.persist_extracted_memory(
                candidate.memory,
                allow_personality_updates=allow_auto_updates,
            ):
                changed += 1
            applied_ids.add(candidate.memory.memory_id)
        return changed
