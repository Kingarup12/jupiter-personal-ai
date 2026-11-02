"""Deterministic policy for Gmail, reminders, proactive work, and fair use."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from services.architecture_store import ArchitectureStore
from services.continuity_engine import ExternalActionGuard


class CreationRole(StrEnum):
    PLANNER = "PLANNER"
    WORKER = "WORKER"
    REVIEWER = "REVIEWER"
    EXECUTOR = "EXECUTOR"


class GmailScope(StrEnum):
    READONLY = "https://www.googleapis.com/auth/gmail.readonly"
    COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
    SEND = "https://www.googleapis.com/auth/gmail.send"
    MODIFY = "https://www.googleapis.com/auth/gmail.modify"


@dataclass(frozen=True)
class EmailDraftPlan:
    recipient_id: str
    recipient_address: str
    subject: str
    body: str
    confirmation_required: bool = True


class GmailPolicy:
    """OAuth and side-effect policy; Google AI keys are never Gmail credentials."""

    @staticmethod
    def required_scopes(*, read: bool, draft: bool, send: bool, modify: bool = False) -> tuple[str, ...]:
        scopes = []
        if read:
            scopes.append(GmailScope.READONLY.value)
        if draft:
            scopes.append(GmailScope.COMPOSE.value)
        if send:
            scopes.append(GmailScope.SEND.value)
        if modify:
            scopes.append(GmailScope.MODIFY.value)
        return tuple(scopes)

    @staticmethod
    def resolve_recipient(matches: list[dict[str, str]]) -> dict[str, str]:
        if len(matches) != 1:
            raise PermissionError("Recipient clarification is required; do not auto-select.")
        return matches[0]

    @staticmethod
    def authorize_send(plan: EmailDraftPlan, *, confirmed: bool, idempotency_key: str) -> None:
        ExternalActionGuard.authorize(
            "EMAIL_SEND", confirmed=confirmed, idempotency_key=idempotency_key
        )


class ReminderRepository:
    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    def create(
        self,
        *,
        user_id: str,
        meaning: str,
        schedule: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> str:
        key = idempotency_key or hashlib.sha256(
            f"{user_id}\0{meaning}\0{json.dumps(schedule, sort_keys=True)}".encode("utf-8")
        ).hexdigest()
        with self.store.transaction() as connection:
            self.store.ensure_user(connection, user_id)
            row = connection.execute(
                """
                INSERT INTO reminders (user_id, meaning, schedule_json, idempotency_key)
                VALUES (%s,%s,%s::jsonb,%s)
                ON CONFLICT (user_id, idempotency_key) DO UPDATE
                    SET idempotency_key=excluded.idempotency_key
                RETURNING reminder_id
                """,
                (user_id, meaning, json.dumps(schedule), key),
            ).fetchone()
            reminder_id = str(row["reminder_id"])
            connection.execute(
                """
                INSERT INTO durable_outbox (
                    user_id, event_type, aggregate_type, aggregate_id,
                    payload, idempotency_key
                ) VALUES (%s,'REMINDER_SCHEDULE','reminder',%s,%s::jsonb,%s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    user_id, reminder_id,
                    json.dumps({"reminder_id": reminder_id, "schedule": schedule}),
                    f"reminder:{reminder_id}:schedule:v1",
                ),
            )
        return reminder_id


class ProactivePolicy:
    PRIORITY = {
        "CRITICAL_ALERT": 0,
        "WEATHER_WARNING": 1,
        "DEADLINE": 2,
        "BIRTHDAY": 3,
        "OPEN_LOOP": 4,
        "MEMORY_CLARIFICATION": 5,
    }

    @classmethod
    def may_interrupt(
        cls,
        *,
        armed: bool,
        response_generating: bool,
        user_mid_task: bool,
        urgent_conversation: bool,
        natural_pause: bool,
        useful: bool,
        recently_proactive: bool,
    ) -> bool:
        return all((
            armed,
            not response_generating,
            not user_mid_task,
            not urgent_conversation,
            natural_pause,
            useful,
            not recently_proactive,
        ))

    @classmethod
    def rank(cls, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: cls.PRIORITY.get(item.get("type"), 99))


class FairUsePolicy:
    """Shared free capacity allocation, never billing or user charges."""

    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    def record(
        self,
        *,
        user_id: str,
        provider: str,
        model_id: str,
        credential_scope: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        audio_seconds: int = 0,
    ) -> None:
        with self.store.transaction() as connection:
            self.store.ensure_user(connection, user_id)
            connection.execute(
                """
                INSERT INTO shared_usage_ledger (
                    user_id, provider, model_id, credential_scope,
                    input_tokens, output_tokens, audio_seconds
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    user_id, provider, model_id, credential_scope,
                    max(0, input_tokens), max(0, output_tokens), max(0, audio_seconds),
                ),
            )

    def user_share_last_day(self, user_id: str, credential_scope: str) -> float:
        with self.store._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN user_id=%s THEN request_count ELSE 0 END),0)::float
                    / GREATEST(COALESCE(SUM(request_count),0),1) AS share
                FROM shared_usage_ledger
                WHERE credential_scope=%s AND recorded_at >= now() - interval '1 day'
                """,
                (user_id, credential_scope),
            ).fetchone()
        return float(row["share"])


class DiscoverySchedule:
    """Prepare catalog/benchmark jobs; never execute them here."""

    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    def enqueue_refresh(self, provider: str, credential_scope: str, reason: str) -> str:
        date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.store.enqueue_outbox(
            user_id=None,
            event_type="PROVIDER_CATALOG_REFRESH",
            aggregate_type="provider",
            aggregate_id=f"{provider}:{credential_scope}",
            payload={
                "provider": provider,
                "credential_scope": credential_scope,
                "reason": reason,
                "benchmark": False,
            },
            idempotency_key=f"catalog:{provider}:{credential_scope}:{date_key}:{reason}",
        )

    def enqueue_benchmark_candidate(
        self, provider: str, model_id: str, credential_scope: str, benchmark_version: str
    ) -> str:
        dimensions = [
            "casual_chat", "hinglish", "hindi", "english", "reasoning", "math",
            "coding", "long_context", "structured_json", "memory_extraction",
            "summarization", "vision", "ocr", "latency", "reliability", "tool_use",
        ]
        return self.store.enqueue_outbox(
            user_id=None,
            event_type="MODEL_BENCHMARK_CANDIDATE",
            aggregate_type="model",
            aggregate_id=f"{provider}:{model_id}:{credential_scope}",
            payload={
                "provider": provider,
                "model_id": model_id,
                "credential_scope": credential_scope,
                "benchmark_version": benchmark_version,
                "dimensions": dimensions,
                "execute_automatically": False,
            },
            idempotency_key=(
                f"benchmark:{provider}:{model_id}:{credential_scope}:{benchmark_version}"
            ),
        )
