"""Durable jobs, outbox work, memory policy, and structured-operation guards."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from services.architecture_store import ArchitectureStore


class JobStatus(StrEnum):
    PLANNING = "PLANNING"
    WAITING_OPTIONAL_INPUT = "WAITING_OPTIONAL_INPUT"
    WAITING_REQUIRED_INPUT = "WAITING_REQUIRED_INPUT"
    RUNNING = "RUNNING"
    REVIEWING = "REVIEWING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED_ALL_FREE_ROUTES = "FAILED_ALL_FREE_ROUTES"
    # Marker 159 (task notifications): a generic failure and a user/system cancellation are distinct terminal states.
    # PostgreSQL: migrations/postgresql/0003_job_status_failed_cancelled.sql extends the CHECK constraint to match.
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    # A tracked task that nothing has started yet (no worker connected/reporting). Never shown as RUNNING.
    AWAITING_WORKER = "AWAITING_WORKER"


class QuestionCriticality(StrEnum):
    OPTIONAL = "OPTIONAL"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class ContinuationPackage:
    original_objective: str
    confirmed_requirements: dict[str, Any]
    user_preferences: dict[str, Any]
    personality_brief: str
    memory_capsule: str
    completed_work: list[str]
    remaining_work: list[str]
    partial_output: str
    output_format: str
    style_requirements: list[str] = field(default_factory=list)
    do_not_repeat: list[str] = field(default_factory=list)

    def as_model_data(self) -> dict[str, Any]:
        return {
            "original_objective": self.original_objective,
            "confirmed_requirements": self.confirmed_requirements,
            "user_preferences": self.user_preferences,
            "personality_brief": self.personality_brief,
            "memory_capsule": self.memory_capsule,
            "completed_work": self.completed_work,
            "remaining_work": self.remaining_work,
            "partial_output": self.partial_output,
            "output_format": self.output_format,
            "style_requirements": self.style_requirements,
            "do_not_repeat": self.do_not_repeat,
        }


class JobContinuityEngine:
    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    def start(
        self,
        *,
        user_id: str,
        objective: str,
        task_type: str,
        requirements: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        stable_key = idempotency_key or hashlib.sha256(
            f"{user_id}\0{task_type}\0{objective}".encode("utf-8")
        ).hexdigest()
        return self.store.create_job(
            user_id=user_id,
            goal=objective,
            task_type=task_type,
            requirements=requirements,
            idempotency_key=stable_key,
        )

    def checkpoint(
        self,
        *,
        job_id: str,
        user_id: str,
        package: ContinuationPackage,
        status: JobStatus = JobStatus.RUNNING,
    ) -> None:
        self.store.checkpoint_job(
            job_id=job_id,
            user_id=user_id,
            checkpoint=package.as_model_data(),
            partial_output=package.partial_output,
            status=status.value,
        )

    def enqueue_background(
        self,
        *,
        user_id: str,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_suffix: str,
    ) -> str:
        return self.store.enqueue_outbox(
            user_id=user_id,
            event_type=event_type,
            aggregate_type="job",
            aggregate_id=job_id,
            payload=payload,
            idempotency_key=f"job:{job_id}:{idempotency_suffix}",
        )


class StructuredProposalValidator:
    """Models propose typed data; only backend policy performs mutations."""

    _TOP_LEVEL = {"memory_candidates", "personality_changes", "topic", "reminders"}
    _MEMORY_FIELDS = {
        "tier", "memory_type", "canonical_fact", "canonical_key", "structured_data",
        "confidence", "source_message_ids", "user_confirmed", "user_pinned",
        "inferred", "valid_from", "valid_until", "expires_at", "sensitivity",
        "visibility_scope",
    }
    _PERSONALITY_FIELDS = {
        "language_style", "tone", "answer_length", "formality", "humour_level",
        "emoji_level", "explanation_style", "coding_style", "preferred_formats",
        "custom_instructions",
    }

    @classmethod
    def validate(cls, proposal: Any) -> dict[str, Any]:
        if not isinstance(proposal, dict) or set(proposal) - cls._TOP_LEVEL:
            raise ValueError("Invalid structured-operation envelope.")
        memories = proposal.get("memory_candidates", [])
        personalities = proposal.get("personality_changes", [])
        if not isinstance(memories, list) or not isinstance(personalities, list):
            raise ValueError("Structured-operation collections must be arrays.")
        for memory in memories:
            if not isinstance(memory, dict) or set(memory) - cls._MEMORY_FIELDS:
                raise ValueError("Invalid memory candidate.")
            if memory.get("tier") not in {"P0", "P1", "P2", "P3", "P4"}:
                raise ValueError("Invalid memory tier.")
            confidence = memory.get("confidence", 0.5)
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise ValueError("Invalid memory confidence.")
        for change in personalities:
            if not isinstance(change, dict) or set(change) - cls._PERSONALITY_FIELDS:
                raise ValueError("Invalid personality change.")
        return proposal


class MemoryPolicyStore:
    """Canonical P0-P4 writes with duplicate merge and correction history."""

    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    @staticmethod
    def canonical_key(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", ".", value.lower()).strip(".")
        if not normalized:
            raise ValueError("A stable canonical memory key is required.")
        return normalized[:240]

    def remember(
        self,
        *,
        user_id: str,
        tier: str,
        memory_type: str,
        canonical_fact: str,
        canonical_key: str,
        source_type: str,
        source_message_ids: list[str],
        confidence: float,
        structured_data: dict[str, Any] | None = None,
        explicit_user_request: bool = False,
        correction: bool = False,
        no_persist: bool = False,
    ) -> str | None:
        if no_persist:
            return None
        if tier not in {"P0", "P1", "P2", "P3", "P4"}:
            raise ValueError("Unsupported memory tier.")
        key = self.canonical_key(canonical_key)
        fact = " ".join(canonical_fact.split())[:4000]
        if not fact:
            raise ValueError("Memory fact is empty.")
        with self.store.transaction() as connection:
            self.store.ensure_user(connection, user_id)
            existing = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id=%s AND canonical_key=%s AND status='ACTIVE'
                  AND deleted_at IS NULL FOR UPDATE
                """,
                (user_id, key),
            ).fetchone()
            if existing and existing["canonical_fact"] == fact:
                connection.execute(
                    """
                    UPDATE memories SET
                        source_message_ids=(SELECT jsonb_agg(DISTINCT value)
                            FROM jsonb_array_elements(source_message_ids || %s::jsonb)),
                        confidence=GREATEST(confidence, %s),
                        user_confirmed=user_confirmed OR %s,
                        user_pinned=user_pinned OR %s,
                        last_mentioned_at=now(), last_verified_at=CASE WHEN %s THEN now()
                            ELSE last_verified_at END,
                        updated_at=now(), version=version+1
                    WHERE memory_id=%s
                    """,
                    (
                        json.dumps(source_message_ids), confidence,
                        explicit_user_request, explicit_user_request,
                        explicit_user_request, existing["memory_id"],
                    ),
                )
                return str(existing["memory_id"])

            if existing and not correction:
                connection.execute(
                    """
                    INSERT INTO memory_review_queue (
                        user_id, candidate, suggested_tier, confidence, reason,
                        possible_choices
                    ) VALUES (%s,%s::jsonb,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        user_id,
                        json.dumps({"canonical_key": key, "canonical_fact": fact}),
                        tier,
                        confidence,
                        "Candidate conflicts with an active canonical fact.",
                        json.dumps(["keep_current", "replace", "store_historical"]),
                    ),
                )
                return None

            old_id = existing["memory_id"] if existing else None
            if existing:
                connection.execute(
                    """
                    UPDATE memories SET status='HISTORICAL', updated_at=now(), version=version+1
                    WHERE memory_id=%s
                    """,
                    (old_id,),
                )
            row = connection.execute(
                """
                INSERT INTO memories (
                    user_id, tier, memory_type, canonical_fact, canonical_key,
                    structured_data, confidence, source_type, source_message_ids,
                    user_confirmed, user_pinned, inferred, embedding_pending
                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,true)
                RETURNING memory_id
                """,
                (
                    user_id, tier, memory_type, fact, key,
                    json.dumps(structured_data or {}), confidence, source_type,
                    json.dumps(source_message_ids), explicit_user_request,
                    explicit_user_request, not explicit_user_request,
                ),
            ).fetchone()
            memory_id = str(row["memory_id"])
            connection.execute(
                """
                INSERT INTO memory_revisions (
                    memory_id, user_id, prior_status, new_status,
                    prior_value, new_value, correction_authority,
                    source_message_ids
                ) VALUES (%s,%s,%s,'ACTIVE',%s::jsonb,%s::jsonb,%s,%s::jsonb)
                """,
                (
                    memory_id, user_id,
                    existing["status"] if existing else None,
                    json.dumps(dict(existing), default=str) if existing else None,
                    json.dumps({"canonical_fact": fact, "tier": tier}),
                    "EXPLICIT_USER" if explicit_user_request else "POLICY_VALIDATED_INFERENCE",
                    json.dumps(source_message_ids),
                ),
            )
            if old_id:
                connection.execute(
                    "UPDATE memories SET structured_data=structured_data || %s::jsonb WHERE memory_id=%s",
                    (json.dumps({"superseded_by": memory_id}), old_id),
                )
            connection.execute(
                """
                INSERT INTO durable_outbox (
                    user_id, event_type, aggregate_type, aggregate_id,
                    payload, idempotency_key
                ) VALUES (%s,'MEMORY_EMBED','memory',%s,%s::jsonb,%s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    user_id, memory_id,
                    json.dumps({
                        "memory_id": memory_id,
                        "embedding_family_intent": "google-embedding-2",
                        "vector_dimensions": 768,
                        "requires_catalog_resolution": True,
                    }),
                    f"memory:{memory_id}:embed:v1",
                ),
            )
        return memory_id

    def forget(self, *, user_id: str, memory_id: str, reason: str) -> bool:
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                UPDATE memories SET deleted_at=now(), status='EXPIRED',
                    structured_data=structured_data || %s::jsonb,
                    updated_at=now(), version=version+1
                WHERE memory_id=%s AND user_id=%s AND deleted_at IS NULL
                RETURNING memory_id
                """,
                (json.dumps({"forget_reason": reason[:500]}), memory_id, user_id),
            ).fetchone()
        return row is not None


class PersonalityPolicyStore:
    FIELDS = StructuredProposalValidator._PERSONALITY_FIELDS

    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    def apply_explicit_change(
        self,
        *,
        user_id: str,
        changes: dict[str, Any],
        source_message_ids: list[str],
        no_persist: bool = False,
    ) -> int | None:
        if no_persist:
            return None
        if not changes or set(changes) - self.FIELDS:
            raise ValueError("Unsupported personality change.")
        with self.store.transaction() as connection:
            self.store.ensure_user(connection, user_id)
            current = connection.execute(
                "SELECT * FROM personality_profiles WHERE user_id=%s FOR UPDATE",
                (user_id,),
            ).fetchone()
            version = int(current["version"] if current else 0) + 1
            columns = sorted(changes)
            if current is None:
                connection.execute(
                    "INSERT INTO personality_profiles (user_id) VALUES (%s)", (user_id,)
                )
            assignments = ", ".join(f"{column}=%s" for column in columns)
            values = [
                json.dumps(changes[column]) if column == "preferred_formats" else changes[column]
                for column in columns
            ]
            connection.execute(
                f"UPDATE personality_profiles SET {assignments}, updated_at=now(), version=%s WHERE user_id=%s",
                (*values, version, user_id),
            )
            connection.execute(
                """
                INSERT INTO personality_revisions (
                    user_id, profile_version, changed_fields,
                    source_message_ids, explicit_user_instruction
                ) VALUES (%s,%s,%s::jsonb,%s::jsonb,true)
                """,
                (user_id, version, json.dumps(changes), json.dumps(source_message_ids)),
            )
        return version


class ExternalActionGuard:
    """Irreversible actions cannot be executed from an unconfirmed model proposal."""

    CRITICAL_ACTIONS = {
        "EMAIL_SEND", "BIOMETRIC_ENROLL", "BIOMETRIC_DELETE",
        "PERMANENT_PURGE", "EXTERNAL_PUBLISH",
    }

    @classmethod
    def authorize(cls, action: str, *, confirmed: bool, idempotency_key: str | None) -> None:
        if action in cls.CRITICAL_ACTIONS and not confirmed:
            raise PermissionError("Explicit confirmation is required.")
        if action in cls.CRITICAL_ACTIONS and not idempotency_key:
            raise ValueError("A stable idempotency key is required.")
