"""PostgreSQL repository for Jupiter's durable control plane.

The repository never creates or migrates schema. A call fails closed until
`DATABASE_URL` is configured and the reviewed migration is applied manually.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from uuid import UUID

from config.provider_settings import database_url
from core.provider_contracts import ProviderModel
from core.security import redact_mapping


class ArchitectureStoreUnavailable(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ArchitectureStore:
    """Explicit, transaction-oriented access to authoritative architecture data."""

    @property
    def configured(self) -> bool:
        return bool(database_url())

    def _connect(self):
        if not self.configured:
            raise ArchitectureStoreUnavailable("DATABASE_URL is not configured.")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise ArchitectureStoreUnavailable(
                "PostgreSQL driver is not installed."
            ) from error
        return psycopg.connect(database_url(), row_factory=dict_row)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """One transaction for authoritative multi-step state changes."""

        with self._connect() as connection:
            with connection.transaction():
                yield connection

    def schema_ready(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_id=%s",
                ("0001_jupiter_control_plane",),
            ).fetchone()
        return row is not None

    def ensure_user(self, connection, user_id: str) -> None:
        connection.execute(
            """
            INSERT INTO user_accounts (user_id, external_account_id)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id, user_id),
        )

    def upsert_model(self, model: ProviderModel, checked_at: datetime | None = None) -> None:
        checked_at = checked_at or _utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO dynamic_model_registry (
                    provider, model_id, display_name, family, credential_scope, credential_owner_id,
                    available, free_eligible, free_verified, free_plan_available,
                    reliability_class, routing_allowed,
                    input_modalities, output_modalities, capabilities,
                    supports_text, supports_image, supports_audio, supports_video,
                    supports_pdf, supports_embeddings, supports_live, supports_tts,
                    supports_image_generation, supports_tools, supports_search,
                    supports_reasoning, supports_structured_output, supports_coding,
                    context_window, max_output, last_seen, last_checked,
                    status, metadata_json
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s::jsonb
                )
                ON CONFLICT (provider, model_id, credential_scope, credential_owner_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    family=excluded.family,
                    available=excluded.available,
                    free_eligible=CASE
                        WHEN excluded.metadata_json->>'catalog_free_status'='unknown'
                            THEN dynamic_model_registry.free_eligible
                        ELSE excluded.free_eligible END,
                    free_verified=CASE
                        WHEN excluded.metadata_json->>'catalog_free_status'='unknown'
                            THEN dynamic_model_registry.free_verified
                        ELSE excluded.free_verified END,
                    free_plan_available=CASE
                        WHEN excluded.metadata_json->>'catalog_free_status'='unknown'
                            THEN dynamic_model_registry.free_plan_available
                        ELSE excluded.free_plan_available END,
                    reliability_class=CASE
                        WHEN excluded.metadata_json->>'catalog_free_status'='paid'
                            THEN excluded.reliability_class
                        WHEN excluded.metadata_json->>'catalog_free_status'='free'
                          AND dynamic_model_registry.metadata_json
                              ->'runtime_health_evidence'->>'last_result'='MODEL_NOT_FREE'
                            THEN 'CANDIDATE'
                        WHEN excluded.metadata_json->>'catalog_free_status'='unknown'
                          AND dynamic_model_registry.reliability_class='CANDIDATE'
                          AND dynamic_model_registry.metadata_json ? 'free_verification'
                          AND (
                              excluded.metadata_json
                                  ->'audit_bootstrap'->>'result'='PAID_PLAN_ONLY'
                              OR excluded.metadata_json
                                  ->>'routing_block_reason'='FREE_STATUS_UNVERIFIED'
                          )
                            THEN 'CANDIDATE'
                        WHEN dynamic_model_registry.status='REMOVED' AND excluded.available
                            THEN 'CANDIDATE'
                        WHEN excluded.available
                          AND dynamic_model_registry.metadata_json
                              ->'audit_bootstrap'->>'failure_reason'
                              ='MODEL_ENDPOINT_UNAVAILABLE'
                          AND dynamic_model_registry.metadata_json
                              ->>'catalog_evidence_fingerprint'
                              IS DISTINCT FROM excluded.metadata_json
                              ->>'catalog_evidence_fingerprint'
                            THEN 'CANDIDATE'
                        WHEN excluded.available
                          AND dynamic_model_registry.reliability_class='CANDIDATE'
                          AND dynamic_model_registry.metadata_json ? 'rediscovery_evidence'
                            THEN 'CANDIDATE'
                        WHEN excluded.available
                          AND dynamic_model_registry.metadata_json
                              ->'runtime_health_evidence'->>'last_result'
                              ='NEEDS_CATALOG_REDISCOVERY'
                          AND dynamic_model_registry.metadata_json
                              ->>'catalog_evidence_fingerprint'
                              IS DISTINCT FROM excluded.metadata_json
                              ->>'catalog_evidence_fingerprint'
                            THEN 'CANDIDATE'
                        WHEN dynamic_model_registry.metadata_json ? 'runtime_health_evidence'
                          AND excluded.metadata_json->>'catalog_free_status'<>'paid'
                            THEN dynamic_model_registry.reliability_class
                        ELSE excluded.reliability_class END,
                    routing_allowed=CASE
                        WHEN excluded.metadata_json->>'catalog_free_status'='paid' THEN false
                        WHEN excluded.metadata_json->>'catalog_free_status'='free'
                          AND dynamic_model_registry.metadata_json
                              ->'runtime_health_evidence'->>'last_result'='MODEL_NOT_FREE'
                            THEN false
                        WHEN excluded.metadata_json->>'catalog_free_status'='unknown'
                          AND dynamic_model_registry.reliability_class='CANDIDATE'
                          AND dynamic_model_registry.metadata_json ? 'free_verification'
                          AND (
                              excluded.metadata_json
                                  ->'audit_bootstrap'->>'result'='PAID_PLAN_ONLY'
                              OR excluded.metadata_json
                                  ->>'routing_block_reason'='FREE_STATUS_UNVERIFIED'
                          )
                            THEN false
                        WHEN dynamic_model_registry.status='REMOVED' AND excluded.available THEN false
                        WHEN excluded.available
                          AND dynamic_model_registry.metadata_json
                              ->'audit_bootstrap'->>'failure_reason'
                              ='MODEL_ENDPOINT_UNAVAILABLE'
                          AND dynamic_model_registry.metadata_json
                              ->>'catalog_evidence_fingerprint'
                              IS DISTINCT FROM excluded.metadata_json
                              ->>'catalog_evidence_fingerprint' THEN false
                        WHEN excluded.available
                          AND dynamic_model_registry.reliability_class='CANDIDATE'
                          AND dynamic_model_registry.metadata_json ? 'rediscovery_evidence'
                            THEN false
                        WHEN excluded.available
                          AND dynamic_model_registry.metadata_json
                              ->'runtime_health_evidence'->>'last_result'
                              ='NEEDS_CATALOG_REDISCOVERY'
                          AND dynamic_model_registry.metadata_json
                              ->>'catalog_evidence_fingerprint'
                              IS DISTINCT FROM excluded.metadata_json
                              ->>'catalog_evidence_fingerprint' THEN false
                        WHEN dynamic_model_registry.metadata_json ? 'runtime_health_evidence'
                            THEN dynamic_model_registry.routing_allowed
                        ELSE excluded.routing_allowed END,
                    input_modalities=excluded.input_modalities,
                    output_modalities=excluded.output_modalities,
                    capabilities=excluded.capabilities,
                    supports_text=excluded.supports_text,
                    supports_image=excluded.supports_image,
                    supports_audio=excluded.supports_audio,
                    supports_video=excluded.supports_video,
                    supports_pdf=excluded.supports_pdf,
                    supports_embeddings=excluded.supports_embeddings,
                    supports_live=excluded.supports_live,
                    supports_tts=excluded.supports_tts,
                    supports_image_generation=excluded.supports_image_generation,
                    supports_tools=excluded.supports_tools,
                    supports_search=excluded.supports_search,
                    supports_reasoning=excluded.supports_reasoning,
                    supports_structured_output=excluded.supports_structured_output,
                    supports_coding=excluded.supports_coding,
                    context_window=excluded.context_window,
                    max_output=excluded.max_output,
                    last_seen=excluded.last_seen,
                    last_checked=excluded.last_checked,
                    status=CASE
                        WHEN excluded.metadata_json->>'catalog_free_status'='paid'
                            THEN excluded.status
                        WHEN excluded.metadata_json->>'catalog_free_status'='free'
                          AND dynamic_model_registry.metadata_json
                              ->'runtime_health_evidence'->>'last_result'='MODEL_NOT_FREE'
                            THEN 'CANDIDATE'
                        WHEN excluded.metadata_json->>'catalog_free_status'='unknown'
                          AND dynamic_model_registry.reliability_class='CANDIDATE'
                          AND dynamic_model_registry.metadata_json ? 'free_verification'
                          AND (
                              excluded.metadata_json
                                  ->'audit_bootstrap'->>'result'='PAID_PLAN_ONLY'
                              OR excluded.metadata_json
                                  ->>'routing_block_reason'='FREE_STATUS_UNVERIFIED'
                          )
                            THEN 'CANDIDATE'
                        WHEN dynamic_model_registry.status='REMOVED' AND excluded.available
                            THEN 'CANDIDATE'
                        WHEN excluded.available
                          AND dynamic_model_registry.metadata_json
                              ->'audit_bootstrap'->>'failure_reason'
                              ='MODEL_ENDPOINT_UNAVAILABLE'
                          AND dynamic_model_registry.metadata_json
                              ->>'catalog_evidence_fingerprint'
                              IS DISTINCT FROM excluded.metadata_json
                              ->>'catalog_evidence_fingerprint'
                            THEN 'CANDIDATE'
                        WHEN excluded.available
                          AND dynamic_model_registry.reliability_class='CANDIDATE'
                          AND dynamic_model_registry.metadata_json ? 'rediscovery_evidence'
                            THEN 'CANDIDATE'
                        WHEN excluded.available
                          AND dynamic_model_registry.metadata_json
                              ->'runtime_health_evidence'->>'last_result'
                              ='NEEDS_CATALOG_REDISCOVERY'
                          AND dynamic_model_registry.metadata_json
                              ->>'catalog_evidence_fingerprint'
                              IS DISTINCT FROM excluded.metadata_json
                              ->>'catalog_evidence_fingerprint'
                            THEN 'CANDIDATE'
                        WHEN dynamic_model_registry.metadata_json ? 'runtime_health_evidence'
                          AND excluded.metadata_json->>'catalog_free_status'<>'paid'
                            THEN dynamic_model_registry.status
                        WHEN excluded.metadata_json->>'catalog_free_status'='unknown'
                          AND dynamic_model_registry.free_verified
                          AND excluded.available THEN 'ACTIVE'
                        ELSE excluded.status END,
                    metadata_json=excluded.metadata_json
                        || CASE WHEN dynamic_model_registry.metadata_json ? 'runtime_health_evidence'
                            THEN jsonb_build_object(
                                'runtime_health_evidence',
                                dynamic_model_registry.metadata_json->'runtime_health_evidence'
                            ) ELSE '{}'::jsonb END
                        || CASE
                        WHEN dynamic_model_registry.metadata_json ? 'free_verification'
                            THEN jsonb_build_object(
                                'free_verification',
                                dynamic_model_registry.metadata_json->'free_verification'
                            )
                        ELSE '{}'::jsonb END
                        || CASE
                        WHEN excluded.available
                          AND dynamic_model_registry.metadata_json
                              ->'audit_bootstrap'->>'failure_reason'
                              ='MODEL_ENDPOINT_UNAVAILABLE'
                          AND dynamic_model_registry.metadata_json
                              ->>'catalog_evidence_fingerprint'
                              IS DISTINCT FROM excluded.metadata_json
                              ->>'catalog_evidence_fingerprint'
                            THEN jsonb_build_object(
                                'rediscovery_evidence', jsonb_build_object(
                                    'catalog_fingerprint', excluded.metadata_json
                                        ->>'catalog_evidence_fingerprint',
                                    'observed_at', now()
                                )
                            )
                        WHEN dynamic_model_registry.metadata_json ? 'rediscovery_evidence'
                            THEN jsonb_build_object(
                                'rediscovery_evidence',
                                dynamic_model_registry.metadata_json->'rediscovery_evidence'
                            )
                        ELSE '{}'::jsonb END,
                    version=dynamic_model_registry.version + 1
                """,
                (
                    model.provider,
                    model.model_id,
                    model.display_name,
                    model.family,
                    model.credential_scope.value,
                    model.credential_owner_id,
                    model.available,
                    model.free_eligible,
                    model.free_verified,
                    model.free_plan_available,
                    model.reliability_class.value,
                    model.routing_allowed,
                    json.dumps(sorted(model.input_modalities)),
                    json.dumps(sorted(model.output_modalities)),
                    json.dumps(sorted(model.capabilities)),
                    "text" in model.input_modalities or "text" in model.output_modalities,
                    "image" in model.input_modalities or "image" in model.output_modalities,
                    "audio" in model.input_modalities or "audio" in model.output_modalities,
                    "video" in model.input_modalities or "video" in model.output_modalities,
                    "pdf" in model.input_modalities or "pdf" in model.capabilities,
                    "embeddings" in model.capabilities,
                    "live" in model.capabilities,
                    "tts" in model.capabilities,
                    "image_generation" in model.capabilities,
                    "tools" in model.capabilities,
                    "search" in model.capabilities,
                    "reasoning" in model.capabilities,
                    "structured_output" in model.capabilities,
                    "coding" in model.capabilities,
                    model.context_window,
                    model.max_output,
                    checked_at,
                    checked_at,
                    model.status,
                    json.dumps(redact_mapping(model.metadata)),
                ),
            )

    def bootstrap_audit_health(
        self,
        model: ProviderModel,
        *,
        health_state: str,
        cooldown_seconds: int | None,
        audit_result: str,
        latency_ms: int | None,
    ) -> None:
        """Apply audit evidence once; runtime observations remain authoritative."""

        circuit_until = (
            _utc_now() + timedelta(seconds=max(1, cooldown_seconds))
            if cooldown_seconds else None
        )
        successful = audit_result == "PASS"
        timed_out = audit_result == "TIMEOUT"
        rate_limited = audit_result == "RATE_LIMITED"
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO provider_health (
                    provider, model_id, credential_scope, credential_owner_id,
                    health_state, consecutive_failures, success_count, failure_count,
                    timeout_count, rate_limit_count, circuit_open_until,
                    last_success, last_failure, last_timeout, failure_class, health_score
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (provider, model_id, credential_scope, credential_owner_id)
                DO NOTHING
                """,
                (
                    model.provider, model.model_id, model.credential_scope.value,
                    model.credential_owner_id, health_state,
                    0 if successful else 1,
                    1 if successful else 0,
                    0 if successful else 1,
                    1 if timed_out else 0,
                    1 if rate_limited else 0,
                    circuit_until,
                    _utc_now() if successful else None,
                    None if successful else _utc_now(),
                    _utc_now() if timed_out else None,
                    None if successful else audit_result,
                    0.8 if successful else 0.25,
                ),
            )
            connection.execute(
                """
                UPDATE dynamic_model_registry
                SET latency_avg=COALESCE(latency_avg,%s),
                    success_rate=COALESCE(success_rate,%s)
                WHERE provider=%s AND model_id=%s AND credential_scope=%s
                  AND credential_owner_id=%s
                  AND NOT (metadata_json ? 'runtime_health_evidence')
                """,
                (
                    latency_ms,
                    1.0 if successful else 0.0,
                    model.provider, model.model_id, model.credential_scope.value,
                    model.credential_owner_id,
                ),
            )

    def reconcile_catalog(
        self,
        *,
        provider: str,
        credential_scope: str,
        credential_owner_id: str,
        seen_model_ids: list[str],
    ) -> int:
        """Deactivate prior catalog rows absent from a successful current listing."""

        if not seen_model_ids:
            return 0
        with self.transaction() as connection:
            result = connection.execute(
                """
                UPDATE dynamic_model_registry
                SET available=false, status='REMOVED',
                    reliability_class='REMOVED_OR_INCOMPATIBLE', routing_allowed=false,
                    last_checked=now(), version=version+1
                WHERE provider=%s AND credential_scope=%s AND credential_owner_id=%s
                  AND metadata_json->>'source'='provider_catalog'
                  AND NOT (model_id = ANY(%s))
                """,
                (provider, credential_scope, credential_owner_id, seen_model_ids),
            )
            return int(result.rowcount or 0)

    def route_candidates(
        self,
        user_id: str,
        credential_scopes: list[str],
        required_modalities: set[str],
        required_capabilities: set[str],
        estimated_input_tokens: int | None,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*,
                       h.health_state, h.circuit_open_until, h.health_score,
                       h.success_count, h.failure_count, h.consecutive_failures,
                       h.timeout_count, h.rate_limit_count,
                       q.remaining, q.reset_at, q.cooldown_until
                FROM dynamic_model_registry m
                LEFT JOIN LATERAL (
                    SELECT ph.health_state, ph.circuit_open_until, ph.health_score,
                           ph.success_count, ph.failure_count, ph.consecutive_failures,
                           ph.timeout_count, ph.rate_limit_count
                    FROM provider_health ph
                    WHERE ph.provider=m.provider
                      AND ph.model_id IN (m.model_id, '*')
                      AND ph.credential_scope=m.credential_scope
                      AND ph.credential_owner_id=m.credential_owner_id
                    ORDER BY (ph.health_state='AUTH_FAILED') DESC,
                             (ph.model_id=m.model_id) DESC
                    LIMIT 1
                ) h ON true
                LEFT JOIN LATERAL (
                    SELECT pq.remaining, pq.reset_at, pq.cooldown_until
                    FROM provider_quota_state pq
                    WHERE pq.provider=m.provider
                      AND pq.model_id IN (m.model_id, '*')
                      AND pq.credential_scope=m.credential_scope
                      AND pq.credential_owner_id=m.credential_owner_id
                      AND pq.quota_scope IN (
                          'ROUTING','ACCOUNT_DAILY_FREE_POOL','RPM','TPM','DAILY','PROVIDER_ACCOUNT'
                      )
                    ORDER BY (pq.cooldown_until IS NOT NULL) DESC,
                             pq.cooldown_until DESC NULLS LAST,
                             (pq.model_id=m.model_id) DESC
                    LIMIT 1
                ) q ON true
                WHERE m.credential_scope = ANY(%s)
                  AND (
                    (m.credential_scope='USER_GOOGLE' AND m.credential_owner_id=%s)
                    OR (m.credential_scope<>'USER_GOOGLE' AND m.credential_owner_id='*')
                  )
                  AND m.available=true
                  AND m.free_eligible=true
                  AND m.free_verified=true
                  AND m.free_plan_available=true
                  AND m.routing_allowed=true
                  AND m.reliability_class IN (
                      'VERIFIED_HEALTHY','TEMPORARY_FAILURE','PROBATIONARY_LAST_RESORT'
                  )
                  AND m.status IN ('ACTIVE','COOLDOWN','CANDIDATE')
                  AND (%s::integer IS NULL OR m.context_window IS NULL OR m.context_window >= %s)
                """,
                (credential_scopes, user_id, estimated_input_tokens, estimated_input_tokens),
            ).fetchall()
        candidates = []
        now = _utc_now()
        for row in rows:
            input_modalities = set(row.get("input_modalities") or [])
            capabilities = set(row.get("capabilities") or [])
            if not required_modalities.issubset(input_modalities):
                continue
            if not required_capabilities.issubset(capabilities):
                continue
            if row.get("circuit_open_until") and row["circuit_open_until"] > now:
                continue
            if row.get("health_state") == "AUTH_FAILED":
                continue
            if row.get("cooldown_until") and row["cooldown_until"] > now:
                continue
            if (
                row.get("remaining") is not None
                and row["remaining"] <= 0
                and (
                    row.get("cooldown_until") is None
                    or row["cooldown_until"] > now
                )
            ):
                continue
            candidates.append(dict(row))
        return candidates

    def health_recheck_candidates(
        self,
        *,
        provider: str | None = None,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Return a small safe batch for an explicit background health worker."""

        safe_limit = max(1, min(int(limit), 10))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, h.health_state, h.health_score, h.circuit_open_until,
                       h.consecutive_failures
                FROM dynamic_model_registry m
                LEFT JOIN provider_health h
                  ON h.provider=m.provider AND h.model_id=m.model_id
                 AND h.credential_scope=m.credential_scope
                 AND h.credential_owner_id=m.credential_owner_id
                WHERE (%s::text IS NULL OR m.provider=%s)
                  AND m.available=true
                  AND m.free_eligible=true
                  AND m.free_verified=true
                  AND m.free_plan_available=true
                  AND m.reliability_class IN (
                      'TEMPORARY_FAILURE','PROBATIONARY_LAST_RESORT','CANDIDATE'
                  )
                  AND m.reliability_class NOT IN (
                      'HARD_BLOCKED','REMOVED_OR_INCOMPATIBLE'
                  )
                  AND (h.circuit_open_until IS NULL OR h.circuit_open_until <= now())
                ORDER BY COALESCE(h.health_score,0.5) DESC,
                         COALESCE(h.consecutive_failures,0) ASC,
                         m.last_checked ASC NULLS FIRST
                LIMIT %s
                """,
                (provider, provider, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_attempt(
        self,
        *,
        request_id: str,
        job_id: str | None,
        user_id: str,
        provider: str,
        requested_model: str,
        actual_model: str | None,
        credential_scope: str,
        task_type: str,
        latency_ms: int | None,
        success: bool,
        status_code: int | None,
        error_class: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        fallback_number: int,
        fallback_reason: str | None,
        routing_metadata: dict[str, Any],
    ) -> None:
        with self.transaction() as connection:
            self.ensure_user(connection, user_id)
            connection.execute(
                """
                INSERT INTO provider_attempts (
                    request_id, job_id, user_id, provider, requested_model,
                    actual_model, credential_scope, task_type, latency_ms,
                    success, status_code, error_class, input_tokens,
                    output_tokens, fallback_number, fallback_reason,
                    routing_metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    request_id, job_id, user_id, provider, requested_model,
                    actual_model, credential_scope, task_type, latency_ms,
                    success, status_code, error_class, input_tokens,
                    output_tokens, fallback_number, fallback_reason,
                    json.dumps(redact_mapping(routing_metadata)),
                ),
            )

    def safe_attempts_for_request(self, user_id: str, request_id: str) -> list[dict[str, Any]]:
        """Owner-safe routing metadata; no prompts, secrets, or hidden reasoning."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, provider, requested_model, actual_model,
                       credential_scope, task_type, start_time, latency_ms,
                       success, status_code, error_class, input_tokens,
                       output_tokens, fallback_number, fallback_reason,
                       routing_metadata
                FROM provider_attempts
                WHERE user_id=%s AND request_id=%s
                ORDER BY start_time
                """,
                (user_id, request_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_health(
        self,
        provider: str,
        model_id: str,
        credential_scope: str,
        credential_owner_id: str,
        health_state: str,
        failure_class: str | None = None,
        cooldown_seconds: int | None = None,
        success: bool = False,
    ) -> None:
        circuit_until = (
            _utc_now() + timedelta(seconds=max(1, cooldown_seconds))
            if cooldown_seconds else None
        )
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO provider_health (
                    provider, model_id, credential_scope, credential_owner_id, health_state,
                    consecutive_failures, success_count, failure_count,
                    timeout_count, rate_limit_count, circuit_open_until,
                    last_success, last_failure, last_timeout, failure_class, health_score
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (provider, model_id, credential_scope, credential_owner_id) DO UPDATE SET
                    health_state=excluded.health_state,
                    consecutive_failures=CASE WHEN %s THEN 0
                        ELSE provider_health.consecutive_failures + 1 END,
                    success_count=provider_health.success_count + CASE WHEN %s THEN 1 ELSE 0 END,
                    failure_count=provider_health.failure_count + CASE WHEN %s THEN 0 ELSE 1 END,
                    timeout_count=provider_health.timeout_count + CASE
                        WHEN %s='TIMEOUT' THEN 1 ELSE 0 END,
                    rate_limit_count=provider_health.rate_limit_count + CASE
                        WHEN %s='RATE_LIMIT' THEN 1 ELSE 0 END,
                    circuit_open_until=excluded.circuit_open_until,
                    last_success=CASE WHEN %s THEN now() ELSE provider_health.last_success END,
                    last_failure=CASE WHEN %s THEN provider_health.last_failure ELSE now() END,
                    last_timeout=CASE WHEN %s='TIMEOUT' THEN now()
                        ELSE provider_health.last_timeout END,
                    failure_class=excluded.failure_class,
                    health_score=CASE WHEN %s
                        THEN LEAST(1.0, provider_health.health_score + 0.1)
                        ELSE GREATEST(0.0, provider_health.health_score - 0.15) END,
                    version=provider_health.version + 1
                """,
                (
                    provider, model_id, credential_scope, credential_owner_id, health_state,
                    0 if success else 1,
                    1 if success else 0,
                    0 if success else 1,
                    1 if failure_class == "TIMEOUT" else 0,
                    1 if failure_class == "RATE_LIMIT" else 0,
                    circuit_until,
                    _utc_now() if success else None,
                    None if success else _utc_now(),
                    _utc_now() if failure_class == "TIMEOUT" else None,
                    failure_class,
                    0.6 if success else 0.35,
                    success, success, success, failure_class, failure_class,
                    success, success, failure_class, success,
                ),
            )

    def promote_model_on_success(self, model: ProviderModel) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE dynamic_model_registry
                SET reliability_class='VERIFIED_HEALTHY', status='ACTIVE',
                    routing_allowed=(available AND free_eligible AND free_verified
                                     AND free_plan_available),
                    last_success=now(),
                    metadata_json=metadata_json || jsonb_build_object(
                        'runtime_health_evidence',
                        jsonb_build_object('last_result','PASS','observed_at',now())
                    ),
                    version=version+1
                WHERE provider=%s AND model_id=%s AND credential_scope=%s
                  AND credential_owner_id=%s
                  AND reliability_class NOT IN ('HARD_BLOCKED','REMOVED_OR_INCOMPATIBLE')
                """,
                (
                    model.provider, model.model_id, model.credential_scope.value,
                    model.credential_owner_id,
                ),
            )

    def mark_model_runtime_state(
        self,
        model: ProviderModel,
        *,
        reliability_class: str,
        status: str,
        routing_allowed: bool,
        reason: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE dynamic_model_registry
                SET reliability_class=%s, status=%s, routing_allowed=%s,
                    last_failure=now(),
                    metadata_json=metadata_json || jsonb_build_object(
                        'runtime_health_evidence',
                        jsonb_build_object('last_result',%s,'observed_at',now())
                    ),
                    version=version+1
                WHERE provider=%s AND model_id=%s AND credential_scope=%s
                  AND credential_owner_id=%s
                """,
                (
                    reliability_class, status, routing_allowed, reason,
                    model.provider, model.model_id, model.credential_scope.value,
                    model.credential_owner_id,
                ),
            )

    def mark_model_status(
        self, provider: str, model_id: str, credential_scope: str,
        credential_owner_id: str, status: str
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE dynamic_model_registry
                SET status=%s, available=CASE WHEN %s IN ('REMOVED','DEPRECATED','INCOMPATIBLE')
                    THEN false ELSE available END,
                    last_failure=now(), version=version+1
                WHERE provider=%s AND model_id=%s AND credential_scope=%s
                  AND credential_owner_id=%s
                """,
                (status, status, provider, model_id, credential_scope, credential_owner_id),
            )

    def mark_credential_scope_status(
        self,
        provider: str,
        credential_scope: str,
        credential_owner_id: str,
        status: str,
    ) -> None:
        with self.transaction() as connection:
            if credential_owner_id != "*":
                credential_status = "AUTH_ERROR" if status == "AUTH_FAILED" else "ACTIVE"
                connection.execute(
                    """
                    UPDATE encrypted_user_credentials
                    SET status=%s,
                        failure_count=CASE WHEN %s='AUTH_ERROR'
                            THEN failure_count+1 ELSE failure_count END,
                        last_failure=CASE WHEN %s='AUTH_ERROR'
                            THEN 'provider_rejected_credential' ELSE NULL END,
                        last_verified_at=CASE WHEN %s='ACTIVE' THEN now()
                            ELSE last_verified_at END,
                        updated_at=now()
                    WHERE user_id=%s AND provider=%s AND credential_scope=%s
                      AND deleted_at IS NULL
                    """,
                    (
                        credential_status, credential_status, credential_status,
                        credential_status, credential_owner_id, provider, credential_scope,
                    ),
                )

    def set_free_verification(
        self,
        *,
        provider: str,
        model_id: str,
        credential_scope: str,
        credential_owner_id: str,
        verified: bool,
        evidence: dict[str, Any],
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE dynamic_model_registry
                SET free_eligible=%s, free_verified=%s, free_plan_available=%s,
                    reliability_class=CASE
                        WHEN %s
                          AND reliability_class='HARD_BLOCKED'
                          AND (
                              metadata_json->'audit_bootstrap'->>'result'='PAID_PLAN_ONLY'
                              OR metadata_json->'runtime_health_evidence'
                                  ->>'last_result'='MODEL_NOT_FREE'
                              OR metadata_json->>'routing_block_reason'='FREE_STATUS_UNVERIFIED'
                          )
                            THEN 'CANDIDATE'
                        ELSE reliability_class END,
                    status=CASE
                        WHEN reliability_class IN ('HARD_BLOCKED','REMOVED_OR_INCOMPATIBLE')
                          AND NOT (%s AND metadata_json
                              ->'audit_bootstrap'->>'result'='PAID_PLAN_ONLY')
                          AND NOT (%s AND metadata_json
                              ->'runtime_health_evidence'->>'last_result'='MODEL_NOT_FREE')
                          AND NOT (%s AND metadata_json
                              ->>'routing_block_reason'='FREE_STATUS_UNVERIFIED')
                            THEN status
                        WHEN %s AND available
                          AND reliability_class='VERIFIED_HEALTHY'
                            THEN 'ACTIVE'
                        ELSE 'CANDIDATE' END,
                    routing_allowed=CASE WHEN %s AND available
                                              AND reliability_class='VERIFIED_HEALTHY'
                                         THEN true ELSE false END,
                    metadata_json=metadata_json || %s::jsonb,
                    last_checked=now(), version=version+1
                WHERE provider=%s AND model_id=%s AND credential_scope=%s
                  AND credential_owner_id=%s
                """,
                (
                    verified, verified, verified, verified, verified,
                    verified, verified, verified, verified,
                    json.dumps({"free_verification": redact_mapping(evidence)}),
                    provider, model_id, credential_scope, credential_owner_id,
                ),
            )

    def set_quota_cooldown(
        self,
        provider: str,
        model_id: str,
        credential_scope: str,
        credential_owner_id: str,
        cooldown_seconds: int,
        exhausted: bool,
        quota_scope: str = "ROUTING",
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO provider_quota_state (
                    provider, model_id, credential_scope, credential_owner_id, quota_scope,
                    remaining, last_429, cooldown_until, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,now(),now()+(%s * interval '1 second'),now())
                ON CONFLICT (provider, model_id, credential_scope, credential_owner_id, quota_scope) DO UPDATE SET
                    remaining=CASE WHEN %s THEN 0 ELSE provider_quota_state.remaining END,
                    last_429=now(),
                    cooldown_until=excluded.cooldown_until,
                    updated_at=now()
                """,
                (
                    provider, model_id, credential_scope, credential_owner_id, quota_scope,
                    0 if exhausted else None, cooldown_seconds, exhausted,
                ),
            )

    def create_job(
        self,
        *,
        user_id: str,
        goal: str,
        task_type: str,
        requirements: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            self.ensure_user(connection, user_id)
            row = connection.execute(
                """
                INSERT INTO jobs (user_id, goal, task_type, requirements, idempotency_key)
                VALUES (%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (user_id, idempotency_key) DO UPDATE
                    SET updated_at=jobs.updated_at
                RETURNING *
                """,
                (user_id, goal, task_type, json.dumps(requirements), idempotency_key),
            ).fetchone()
        return dict(row)

    def checkpoint_job(
        self,
        *,
        job_id: str,
        user_id: str,
        checkpoint: dict[str, Any],
        partial_output: str | None,
        status: str,
    ) -> None:
        with self.transaction() as connection:
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next FROM job_checkpoints WHERE job_id=%s",
                (job_id,),
            ).fetchone()["next"]
            connection.execute(
                """
                INSERT INTO job_checkpoints
                    (job_id, user_id, sequence_no, checkpoint, partial_output)
                VALUES (%s,%s,%s,%s::jsonb,%s)
                """,
                (job_id, user_id, sequence, json.dumps(checkpoint), partial_output),
            )
            connection.execute(
                """
                UPDATE jobs SET partial_output=%s, status=%s, updated_at=now(),
                    version=version+1 WHERE job_id=%s AND user_id=%s
                """,
                (partial_output, status, job_id, user_id),
            )

    def enqueue_outbox(
        self,
        *,
        user_id: str | None,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> str:
        with self.transaction() as connection:
            if user_id:
                self.ensure_user(connection, user_id)
            row = connection.execute(
                """
                INSERT INTO durable_outbox (
                    user_id, event_type, aggregate_type, aggregate_id,
                    payload, idempotency_key
                ) VALUES (%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (idempotency_key) DO UPDATE
                    SET idempotency_key=excluded.idempotency_key
                RETURNING outbox_id
                """,
                (
                    user_id, event_type, aggregate_type, aggregate_id,
                    json.dumps(redact_mapping(payload)), idempotency_key,
                ),
            ).fetchone()
        return str(row["outbox_id"])

    def audit(
        self,
        *,
        user_id: str | None,
        actor_id: str | None,
        action: str,
        object_type: str,
        object_id: str,
        safe_metadata: dict[str, Any],
        request_id: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            if user_id:
                self.ensure_user(connection, user_id)
            connection.execute(
                """
                INSERT INTO audit_log (
                    user_id, actor_id, action, object_type, object_id,
                    safe_metadata, request_id
                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)
                """,
                (
                    user_id, actor_id, action, object_type, object_id,
                    json.dumps(redact_mapping(safe_metadata)), request_id,
                ),
            )
