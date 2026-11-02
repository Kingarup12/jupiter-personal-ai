"""Credential selection after the task and logical model are selected."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from config.credential_policy import (
    managed_google_policies,
    managed_google_policy,
    order_managed_google_candidates,
    safe_google_credential_statuses,
)
from core.provider_contracts import (
    AIRequest,
    CredentialScope,
    FailureClass,
    NormalizedFailure,
    PrivacyClass,
    TaskType,
)
from core.request_context import current_request_context, resolve_request_credential, request_google_quota_group
from services.model_routing import RouteCandidate


class CredentialRouter:
    """Expand logical Google models into allowed credential attempts.

    Model order is preserved.  Only Google receives a same-model credential
    fallback, and the gateway decides whether a failure is eligible to advance
    from the user credential to Jupiter's managed credential.
    """

    def __init__(self, path: str | Path | None = None):
        root = Path(__file__).resolve().parents[1]
        data_root = Path(os.getenv("JUPITER_DATA_DIR", "").strip() or root / "data")
        self.path = Path(path) if path else data_root / "credential_health.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS google_credential_health (
                    credential_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    failure_class TEXT,
                    reason_code TEXT,
                    last_success REAL,
                    last_failure REAL,
                    cooldown_until REAL,
                    updated_at REAL NOT NULL
                )
                """
            )

    @staticmethod
    def _workload_rank(credential_id: str, task: TaskType) -> int:
        """Informational specialization label only (reported in metadata). Ordering uses the structural
        credential priority among eligible credentials; this rank never reorders credentials."""
        assignments = {
            "provider-4-managed": {TaskType.CODING, TaskType.HARD_REASONING, TaskType.MATH, TaskType.PLANNING},
            "provider-3-managed": {TaskType.VISION_GENERAL, TaskType.VISION_UI, TaskType.VIDEO_UNDERSTANDING},
            "provider-5-managed": {TaskType.MEMORY_CONTRADICTION, TaskType.MEMORY_CURATOR, TaskType.MEMORY_P0_JUDGE},
            "provider-6-managed": {TaskType.HUGE_CONTEXT, TaskType.PDF_COMPLEX, TaskType.WEB_RESEARCH, TaskType.REVIEWING},
            "provider-11-managed": {TaskType.COMPLEX_CHAT, TaskType.FINAL_SYNTHESIS, TaskType.EMAIL_READING, TaskType.EMAIL_WRITING},
            "provider-7-managed": {TaskType.INTENT_CLASSIFICATION, TaskType.CAPABILITY_CLASSIFICATION, TaskType.STRUCTURED_EXTRACTION,
                             TaskType.TOPIC_DETECTION, TaskType.PERSONALITY_EXTRACTION, TaskType.MEMORY_EXTRACTION,
                             TaskType.MEMORY_FACT_EXTRACTION, TaskType.MEMORY_CANDIDATE_DETECTION, TaskType.CASUAL_CHAT},
        }
        if credential_id == "jupiter-user-google":
            return -1  # Available only for the existing entitled account policy.
        return 0 if task in assignments.get(credential_id, set()) else 2 if credential_id == "maintainer-managed" else 1

    @staticmethod
    def _user_google_available() -> bool:
        return bool(resolve_request_credential(CredentialScope.USER_GOOGLE.value))

    def order_candidates(
        self, request: AIRequest, candidates: tuple[RouteCandidate, ...]
    ) -> tuple[RouteCandidate, ...]:
        context = current_request_context()
        uses_jupiter_user_google = bool(context.managed_google_credentials)
        # Entitlement permits an additional fallback; it never supersedes the
        # authenticated user's own saved key or a different request identity.
        user_available = context.user_id == request.user_id and self._user_google_available()
        user_key = resolve_request_credential(CredentialScope.USER_GOOGLE.value) if user_available else None
        ordered: list[RouteCandidate] = []
        if request.privacy_class in {PrivacyClass.BIOMETRIC, PrivacyClass.SECRET}:
            return ()
        seen_logical: set[tuple[str, str]] = set()
        unique: list[RouteCandidate] = []
        for candidate in candidates:
            logical_key = (candidate.model.provider, candidate.model.model_id)
            if logical_key not in seen_logical:
                seen_logical.add(logical_key)
                unique.append(candidate)
        # USER-FIRST (user decision 2026-10-07): the current user's own credential is tried on EVERY compatible Google
        # model, in model order, before any developer credential on any model. Model choice and credential choice stay
        # separate: this only reorders the credential expansion; it never adds models or retries.
        if user_available:
            for candidate in unique:
                if candidate.model.provider == "google":
                    ordered.append(self._user_google_candidate(request, candidate))
        # Optional user keys extend USER-FIRST selection without changing the
        # existing logical-model or managed-account ordering. Never create new
        # models, bypass privacy/free gates, or resolve another user's key.
        if context.user_id == request.user_id and request.privacy_class not in {PrivacyClass.PRIVATE, PrivacyClass.SENSITIVE}:
            from services.optional_account_providers import MODEL_USER_SCOPES
            for candidate in unique:
                scope = MODEL_USER_SCOPES.get(candidate.model.provider)
                if scope and context.credential_resolver and context.credential_resolver(scope):
                    ordered.append(replace(candidate, model=replace(
                        candidate.model, credential_scope=CredentialScope(scope), credential_owner_id=request.user_id,
                        metadata={**candidate.model.metadata, 'credential_id': 'user-' + candidate.model.provider,
                                  'credential_label': 'User credential', 'credential_priority': 1, 'paid_usage_allowed': False},
                    ), reason=f'{candidate.reason}; credential=user {candidate.model.provider}'))
        for candidate in unique:
            model = candidate.model
            if model.provider != "google":
                if request.privacy_class not in {PrivacyClass.PRIVATE, PrivacyClass.SENSITIVE}:
                    ordered.append(candidate)
                continue
            # Sensitive data is never silently promoted to a managed credential
            # after exhaustion/failure of the user's own key.
            if request.privacy_class == PrivacyClass.SENSITIVE and user_available:
                continue
            capability = next(iter(sorted(request.required_capabilities)), request.task_type.value.casefold())
            paid_only = bool(model.metadata.get("paid_only"))
            policies = managed_google_policies(
                model.model_id,
                capability,
                paid_only=paid_only,
                include_jupiter_user_google=uses_jupiter_user_google,
            )
            for policy in sorted(policies, key=lambda policy: policy.priority):
                if not self._policy_available(policy.credential_id):
                    continue
                if user_key and policy.secret() == user_key:
                    continue  # Same key was already attempted as this account's own.
                ordered.append(replace(
                    candidate,
                    model=replace(
                        model,
                        credential_scope=CredentialScope.JUPITER_SHARED_GOOGLE,
                        credential_owner_id=policy.credential_id,
                        metadata={
                            **model.metadata,
                            "credential_id": policy.credential_id,
                            "credential_label": policy.label,
                            "credential_priority": policy.priority,
                            "credential_workload_rank": self._workload_rank(policy.credential_id, request.task_type),
                            "google_quota_group": policy.quota_group_key(),
                            "google_quota_group_label": policy.quota_group_label(),
                            "paid_usage_allowed": bool(policy.paid_usage_allowed),
                            "denied_model_count": len(policy.denied_models),
                        },
                    ),
                    reason=f"{candidate.reason}; credential={policy.label}",
                ))
        return order_managed_google_candidates(ordered)

    @staticmethod
    def _user_google_candidate(request: AIRequest, candidate: RouteCandidate) -> RouteCandidate:
        model = candidate.model
        return replace(
            candidate,
            model=replace(
                model,
                credential_scope=CredentialScope.USER_GOOGLE,
                credential_owner_id=request.user_id,
                metadata={
                    **model.metadata,
                    "credential_id": "user-google",
                    "credential_label": "User credential",
                    "credential_priority": 1,
                    "google_quota_group": request_google_quota_group(),
                    "paid_usage_allowed": False,
                },
            ),
            reason=f"{candidate.reason}; credential=user Google",
        )

    # A stale AUTH_INVALID (e.g. a key that was replaced/repaired in .env) must not stay dead forever: after this
    # window ONE probe attempt is admitted; success resets health (record_success), failure re-arms the window.
    AUTH_REVALIDATE_AFTER_SECONDS = 6 * 60 * 60

    def _policy_available(self, credential_id: str) -> bool:
        now = time.time()
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT status, cooldown_until, last_failure FROM google_credential_health WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
        if not row:
            return True
        status, cooldown_until = str(row[0] or ""), float(row[1] or 0)
        if status == "AUTH_INVALID":
            return bool(row[2]) and now - float(row[2]) >= self.AUTH_REVALIDATE_AFTER_SECONDS
        if status in {"AUTH_REVOKED", "DISABLED"}:
            return False
        # Legacy per-model quota records are not credential-wide outages.
        if status in {"QUOTA_EXHAUSTED", "RATE_LIMITED"}:
            return True
        return not cooldown_until or cooldown_until <= now

    @staticmethod
    def same_model_credential_fallback_allowed(failure: NormalizedFailure) -> bool:
        if (
            failure.failure_class in {
                FailureClass.BAD_REQUEST,
                FailureClass.MODEL_REMOVED,
            }
        ):
            return False
        return True

    def record_failure(self, candidate: RouteCandidate, failure: NormalizedFailure) -> None:
        if candidate.model.credential_scope.value.startswith('USER_') and candidate.model.provider != 'google':
            recorder = current_request_context().credential_status_recorder
            if recorder and (failure.auth_error or failure.failure_class == FailureClass.AUTHENTICATION):
                recorder(candidate.model.credential_scope.value, 'AUTH_FAILED')
            return
        if candidate.model.provider != "google":
            return
        auth_failed = failure.auth_error or failure.failure_class == FailureClass.AUTHENTICATION
        quota_failed = failure.quota_exhausted or failure.failure_class in {
            FailureClass.QUOTA_EXHAUSTED,
            FailureClass.RATE_LIMIT,
        }
        if quota_failed and not auth_failed:
            # QuotaGovernor owns model/project and explicitly shared pools.
            # Never detach a valid user key because one Gemini model is full.
            return
        if candidate.model.credential_scope == CredentialScope.USER_GOOGLE:
            recorder = current_request_context().credential_status_recorder
            if recorder and (auth_failed or quota_failed):
                recorder(
                    CredentialScope.USER_GOOGLE.value,
                    "AUTH_FAILED" if auth_failed else "QUOTA_EXHAUSTED",
                )
            return
        credential_id = candidate.model.credential_owner_id
        if managed_google_policy(credential_id) is None:
            return
        now = time.time()
        status = (
            "AUTH_INVALID" if auth_failed
            else "QUOTA_EXHAUSTED" if failure.quota_exhausted
            else "RATE_LIMITED" if failure.failure_class == FailureClass.RATE_LIMIT
            else "DEGRADED"
        )
        cooldown = None
        if quota_failed:
            cooldown = now + max(60, int(failure.cooldown_seconds or 60))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                INSERT INTO google_credential_health (
                    credential_id, status, failure_class, reason_code,
                    last_failure, cooldown_until, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(credential_id) DO UPDATE SET
                    status=excluded.status,
                    failure_class=excluded.failure_class,
                    reason_code=excluded.reason_code,
                    last_failure=excluded.last_failure,
                    cooldown_until=excluded.cooldown_until,
                    updated_at=excluded.updated_at
                """,
                (
                    credential_id,
                    status,
                    failure.failure_class.value,
                    failure.reason_code,
                    now,
                    cooldown,
                    now,
                ),
            )

    def reset_credential_health(self, credential_id: str) -> bool:
        """Reset managed credential health state to HEALTHY after repair or validation."""
        if managed_google_policy(credential_id) is None:
            return False
        now = time.time()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                INSERT INTO google_credential_health (
                    credential_id, status, last_success, updated_at
                ) VALUES (?, 'HEALTHY', ?, ?)
                ON CONFLICT(credential_id) DO UPDATE SET
                    status='HEALTHY', failure_class=NULL, reason_code=NULL,
                    last_success=excluded.last_success, cooldown_until=NULL,
                    updated_at=excluded.updated_at
                """,
                (credential_id, now, now),
            )
        return True

    def record_success(self, candidate: RouteCandidate) -> None:
        model = candidate.model
        if model.credential_scope.value.startswith('USER_') and model.provider != 'google':
            recorder = current_request_context().credential_status_recorder
            if recorder:
                recorder(model.credential_scope.value, 'ACTIVE')
            return
        if model.provider != "google":
            return
        if model.credential_scope == CredentialScope.USER_GOOGLE:
            recorder = current_request_context().credential_status_recorder
            if recorder:
                recorder(CredentialScope.USER_GOOGLE.value, "ACTIVE")
            return
        self.reset_credential_health(model.credential_owner_id)

    @staticmethod
    def credential_key(candidate: RouteCandidate) -> tuple[str, str]:
        return (
            candidate.model.credential_scope.value,
            candidate.model.credential_owner_id,
        )

    @staticmethod
    def quota_group(candidate: RouteCandidate) -> str:
        group = str(candidate.model.metadata.get("google_quota_group") or "")
        return f"{group}\0{candidate.model.model_id}" if group else ""

    def safe_snapshot(self) -> list[dict[str, object]]:
        now = time.time()
        with closing(sqlite3.connect(self.path)) as connection:
            rows = {
                str(row[0]): {
                    "health": str(row[1]),
                    "failure_class": row[2],
                    "reason_code": row[3],
                    "last_success": row[4],
                    "last_failure": row[5],
                    "cooldown_until": row[6] if float(row[6] or 0) > now else None,
                }
                for row in connection.execute(
                    "SELECT credential_id, status, failure_class, reason_code, "
                    "last_success, last_failure, cooldown_until FROM google_credential_health"
                ).fetchall()
            }
        return [
            {**item, **rows.get(str(item["credential_id"]), {"health": "NOT_CHECKED"})}
            for item in safe_google_credential_statuses()
        ]

    @staticmethod
    def same_logical_model(left: RouteCandidate | None, right: RouteCandidate) -> bool:
        return bool(
            left
            and left.model.provider == right.model.provider
            and left.model.model_id == right.model.model_id
            and CredentialRouter.credential_key(left) != CredentialRouter.credential_key(right)
        )
