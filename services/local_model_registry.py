"""Atomic file-backed free-only registry for installations without Postgres."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from contextvars import copy_context
from uuid import uuid4
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.provider_contracts import (
    AIRequest,
    CredentialScope,
    FailureClass,
    HealthState,
    NormalizedFailure,
    PrivacyClass,
    ProviderModel,
    ReliabilityClass,
    TaskType,
)
from config.provider_catalog import documented_io, free_plan_listed, free_search_grounding, routing_gate
from services.task_preferences import preference_rank, workload_allowed
from core.request_context import current_request_context


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "model_registry.json"
REFRESH_LOCK_PATH = ROOT / "config" / ".model_registry_refresh.lock"
AUDIT_SCRIPT = ROOT / "scripts" / "model_capability_audit.py"
_THREAD_LOCK = threading.RLock()

_SCOPES = {
    "google": CredentialScope.JUPITER_SHARED_GOOGLE,
    "groq": CredentialScope.JUPITER_SHARED_GROQ,
    "nvidia": CredentialScope.JUPITER_SHARED_NVIDIA,
    "cloudflare": CredentialScope.JUPITER_SHARED_CLOUDFLARE,
    "opencode": CredentialScope.JUPITER_SHARED_OPENCODE,
    "openrouter": CredentialScope.JUPITER_SHARED_OPENROUTER,
    "bytez": CredentialScope.JUPITER_SHARED_BYTEZ,
}

_TASK_ROLES = {
    TaskType.INTENT_CLASSIFICATION: "intent_classification",
    TaskType.CAPABILITY_CLASSIFICATION: "intent_classification",
    TaskType.CASUAL_CHAT: "simple_chat",
    TaskType.COMPLEX_CHAT: "general_chat",
    TaskType.HARD_REASONING: "complex_reasoning",
    TaskType.MATH: "mathematics",
    TaskType.CODING: "coding",
    TaskType.FAST_CODING: "coding",
    TaskType.HUGE_CONTEXT: "complex_reasoning",
    TaskType.FINAL_SYNTHESIS: "complex_reasoning",
    TaskType.MEMORY_EXTRACTION: "memory_extraction",
    TaskType.MEMORY_CURATOR: "memory_extraction",
    TaskType.MEMORY_CANDIDATE_DETECTION: "intent_classification",
    TaskType.MEMORY_TIER_CLASSIFICATION: "memory_extraction",
    TaskType.MEMORY_FACT_EXTRACTION: "memory_extraction",
    TaskType.MEMORY_RELATIONSHIP_EXTRACTION: "memory_extraction",
    TaskType.MEMORY_DUPLICATE_CONSOLIDATION: "memory_extraction",
    TaskType.MEMORY_CONTRADICTION: "complex_reasoning",
    TaskType.MEMORY_RERANKING: "memory_extraction",
    TaskType.MEMORY_P0_JUDGE: "complex_reasoning",
    TaskType.PERSONALITY_EXTRACTION: "intent_classification",
    TaskType.TOPIC_DETECTION: "intent_classification",
    TaskType.SUMMARIZATION: "summarization",
    TaskType.STRUCTURED_EXTRACTION: "intent_classification",
    TaskType.EMAIL_READING: "gmail_summarization",
    TaskType.EMAIL_WRITING: "gmail_summarization",
    TaskType.PDF_EXTRACTION: "pdf_extraction",
    TaskType.PDF_SIMPLE: "pdf_extraction",
    TaskType.PDF_COMPLEX: "pdf_reasoning",
    TaskType.VISION_GENERAL: "vision",
    TaskType.VISION_UI: "vision",
    TaskType.VIDEO_UNDERSTANDING: "vision",
    TaskType.WEB_RESEARCH: "web_research",
    TaskType.QUICK_CURRENT_INFO: "web_research",
    TaskType.SHOPPING_RESEARCH: "web_research",
    TaskType.TRANSLATION: "summarization",
    TaskType.PLANNING: "complex_reasoning",
    TaskType.REVIEWING: "complex_reasoning",
}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _atomic_write(payload: dict[str, Any]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{REGISTRY_PATH.name}.", suffix=".tmp", dir=REGISTRY_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, REGISTRY_PATH)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class LocalModelRegistry:
    """Read-mostly registry with atomic circuit-breaker state updates."""

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema_version": 1, "models": [], "routing": {}, "activation_ready": False}
        if payload.get("schema_version") != 1 or not isinstance(payload.get("models"), list):
            return {"schema_version": 1, "models": [], "routing": {}, "activation_ready": False}
        return payload

    def is_ready(self) -> bool:
        payload = self.load()
        return bool(payload.get("activation_ready") and any(
            row.get("status") == "active"
            and row.get("output_valid") is True
            and any(
                marker in str(row.get("free_status_confidence") or "").casefold()
                for marker in ("official-free", "supplied-free", "positive-free")
            )
            for row in payload.get("models", [])
        ))

    def is_stale(self) -> bool:
        payload = self.load()
        generated = _parse_time(payload.get("last_discovery_at") or payload.get("generated_at"))
        hours = max(1, int(payload.get("refresh_after_hours") or 24))
        return generated is None or generated + timedelta(hours=hours) <= datetime.now(timezone.utc)

    @staticmethod
    def refresh_running() -> bool:
        if not REFRESH_LOCK_PATH.exists():
            return False
        try:
            if datetime.now(timezone.utc).timestamp() - REFRESH_LOCK_PATH.stat().st_mtime > 2 * 60 * 60:
                REFRESH_LOCK_PATH.unlink(missing_ok=True)
                return False
        except OSError:
            return True
        return True

    def start_refresh(self, *, force: bool = False) -> dict[str, Any]:
        if self.refresh_running():
            return {"started": False, "reason": "refresh-already-running"}
        if not force and not self.is_stale():
            return {"started": False, "reason": "registry-is-fresh"}
        try:
            lock = os.open(REFRESH_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return {"started": False, "reason": "refresh-already-running"}
        os.close(lock)
        job_id = uuid4().hex
        from services.model_discovery import collect_catalogs, merge_catalogs, stamp
        try:
            with _THREAD_LOCK:
                _atomic_write({**self.load(), "last_refresh": {
                    "job_id": job_id, "state": "running", "started_at": stamp()}})
        except Exception:
            REFRESH_LOCK_PATH.unlink(missing_ok=True)
            raise
        def refresh():
            try:
                results = collect_catalogs()
                with _THREAD_LOCK:
                    # Reload after the network work so runtime health is never
                    # replaced by a stale snapshot captured before discovery.
                    _atomic_write(merge_catalogs(self.load(), results, job_id=job_id))
            except Exception:
                with _THREAD_LOCK:
                    _atomic_write({**self.load(), "last_refresh": {
                        "job_id": job_id, "state": "failed", "completed_at": stamp()}})
            finally:
                REFRESH_LOCK_PATH.unlink(missing_ok=True)
        try:
            threading.Thread(target=copy_context().run, args=(refresh,),
                             name="jupiter-model-discovery", daemon=True).start()
        except Exception:
            REFRESH_LOCK_PATH.unlink(missing_ok=True)
            with _THREAD_LOCK:
                _atomic_write({**self.load(), "last_refresh": {
                    "job_id": job_id, "state": "failed", "completed_at": stamp()}})
            raise
        return {"started": True, "job_id": job_id}

    def start_provider_refresh(self, provider: str) -> dict[str, Any]:
        """Start one provider's free-only audit for the Health Center Retest control."""

        provider_key = str(provider or "").strip().casefold()
        if provider_key not in _SCOPES:
            return {"started": False, "reason": "unsupported-provider"}
        if provider_key == "bytez":
            return {"started": False, "reason": "bytez-catalog-metadata-only"}
        if self.refresh_running():
            return {"started": False, "reason": "refresh-already-running"}
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [sys.executable, str(AUDIT_SCRIPT), "--providers", provider_key],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        return {"started": True, "pid": process.pid, "provider": provider_key}

    def route(self, request: AIRequest):
        from services.model_routing import RouteCandidate, RouteDecision
        from services.model_discovery import routing_metadata

        self.start_refresh(force=False)
        role = _TASK_ROLES.get(request.task_type)
        if not role:
            return RouteDecision(request.task_type, (), (), (), {"registry": "local", "reason": "specialist-required"})
        payload = self.load()
        configured = payload.get("routing", {}).get(role, {})
        ordered = [configured.get("primary"), configured.get("secondary"), *configured.get("backups", [])]
        order_index = {value: index for index, value in enumerate(value for value in ordered if value)}
        now = datetime.now(timezone.utc)
        candidates = []
        rejected = []
        seen_models: set[tuple[str, str]] = set()
        for row in payload.get("models", []):
            identifier = f"{row.get('provider')}:{row.get('model_id')}"
            roles = set(row.get("roles") or [])
            mapped_rank, _ = preference_rank(request.task_type, str(row.get("provider") or ""), str(row.get("model_id") or ""))
            # Workload preference may refine an already-audited general text
            # model into coding/reasoning; it cannot invent multimodal support.
            text_workload = request.task_type in {
                TaskType.CODING, TaskType.FAST_CODING, TaskType.HARD_REASONING, TaskType.HUGE_CONTEXT,
                # 17 Oct 2026 routing review: MATH / FINAL_SYNTHESIS / REVIEWING had ONE model (only groq
                # gpt-oss-120b carries the mathematics/complex_reasoning role) and no fallback at all.
                TaskType.MATH, TaskType.FINAL_SYNTHESIS, TaskType.REVIEWING,
                TaskType.WEB_RESEARCH, TaskType.QUICK_CURRENT_INFO, TaskType.SHOPPING_RESEARCH,
                TaskType.PDF_COMPLEX,
                # PRIVATE text judgement tasks: only Google may serve them, so an audited Google general_chat
                # text model matched by preference must qualify (otherwise no eligible model exists at all).
                TaskType.MEMORY_P0_JUDGE, TaskType.MEMORY_CONTRADICTION,
                # Laptop-control planning is PRIVATE (screen content) and needs structured output: without this, no
                # Google model carries the complex_reasoning role and the planner could never route (8 Oct 2026).
                TaskType.PLANNING,
                # Image / video / PDF chat (8 Oct 2026): no row carries the vision or pdf_extraction role, so every
                # attachment turn failed with no route. The input-type check below still admits only models whose
                # OFFICIAL page documents that input (catalog model_io).
                TaskType.VISION_GENERAL, TaskType.VISION_UI, TaskType.VIDEO_UNDERSTANDING,
                TaskType.PDF_SIMPLE, TaskType.PDF_EXTRACTION,
            }
            if role not in roles and not (text_workload and mapped_rank is not None and "general_chat" in roles):
                continue
            status = str(row.get("status") or "")
            cooldown = _parse_time(row.get("circuit_open_until"))
            retryable_after_cooldown = status in {"temporarily-disabled", "quota-exhausted"}
            provider = str(row.get("provider") or "")
            discovery = row.get("discovery") or {}
            if (not workload_allowed(request.task_type, provider, str(row.get("model_id") or ""))
                    or discovery.get("available") is False
                    or (discovery.get("catalog_free_status") == "paid"
                        and not free_plan_listed(provider, str(row.get("model_id") or "")))
                    or str(discovery.get("lifecycle_status") or "").upper() in {"REMOVED", "DEPRECATED", "DISABLED"}
                    or row.get("disabled_by_policy") is True or row.get("paid_only") is True):
                continue
            if provider == "bytez" or row.get("specialist_only") is True:
                rejected.append({"provider": provider, "model_id": str(row.get("model_id")), "reason": "specialist-contract-pending"})
                continue
            # Default free-only product policy (config/provider_catalog.json, official sources + check date): only a
            # recurring free allowance is routable; promotional/trial/development-only/paid/unknown terms fail closed,
            # and routes whose terms forbid personal data never receive the user's content.
            policy_allowed, policy_reason = routing_gate(provider, str(row.get("model_id") or ""), request.privacy_class.value)
            if not policy_allowed:
                rejected.append({"provider": provider, "model_id": str(row.get("model_id")), "reason": policy_reason})
                continue
            context = current_request_context()
            user_google_available = bool(
                provider == "google"
                and not context.managed_google_credentials
                and context.credential_resolver
                and context.credential_resolver(CredentialScope.USER_GOOGLE.value)
            )
            # The checked registry audits Jupiter's shared credential. A row
            # with valid output but shared-credential authorization failure is
            # still a valid logical Google model for an authenticated user's
            # own credential. CredentialRouter performs that per-user expansion
            # only after this free/capability gate.
            free_confidence = str(row.get("free_status_confidence") or "").casefold()
            # Older locally generated registry rows predate the explicit
            # evidence fields. An ACTIVE row from that schema was itself the
            # verified-free contract; newer rows must provide positive evidence.
            has_free_evidence_field = "free_status_confidence" in row
            verified_free_evidence = (
                bool(
                    (
                        any(marker in free_confidence for marker in ("official-free", "supplied-free", "positive-free"))
                        or ("positive" in free_confidence and "free" in free_confidence)
                    )
                    and not any(marker in free_confidence for marker in ("unknown", "unconfirmed", "paid", "zero"))
                ) if has_free_evidence_field else status in {
                    "active", "backup", "temporarily-disabled", "quota-exhausted"
                }
            )
            output_valid = row.get("output_valid") is True if has_free_evidence_field else row.get("output_valid") is not False
            verified_active_route = bool(
                status in {"active", "backup"}
                and output_valid
                and verified_free_evidence
            )
            verified_retryable_route = bool(
                retryable_after_cooldown and cooldown and cooldown <= now
                and output_valid and verified_free_evidence
            )
            user_google_logical_route = bool(
                user_google_available
                and status == "unauthorized"
                and row.get("output_valid") is True
                and verified_free_evidence
                and row.get("model_id")
            )
            if not verified_active_route and not user_google_logical_route and not verified_retryable_route:
                continue
            scope = _SCOPES.get(provider)
            if scope is None:
                continue
            model_key = (provider, str(row.get("model_id") or ""))
            if model_key in seen_models:
                continue
            seen_models.add(model_key)
            if (request.privacy_class in {PrivacyClass.BIOMETRIC, PrivacyClass.SECRET}
                    or request.privacy_class in {PrivacyClass.PRIVATE, PrivacyClass.SENSITIVE} and provider != "google"):
                rejected.append({"provider": provider, "model_id": str(row.get("model_id")), "reason": "privacy-policy"})
                continue
            if cooldown and cooldown > now:
                rejected.append({"provider": provider, "model_id": str(row.get("model_id")), "reason": "circuit-open"})
                continue
            metadata = routing_metadata(row)
            documented = documented_io(provider, model_key[1])
            if documented:
                inputs = frozenset(metadata["input_modalities"]) | frozenset(documented.get("inputs") or ())
                capabilities = set(metadata["capabilities"])
                if {"image", "video"} & inputs:
                    capabilities.add("vision")
                if "pdf" in inputs:
                    capabilities.add("pdf")
                metadata = {**metadata, "input_modalities": inputs, "capabilities": frozenset(capabilities),
                            "context_window": metadata.get("context_window") or documented.get("input_token_limit")}
            if "text" not in metadata["output_modalities"]:
                continue
            if request.task_type in {TaskType.WEB_RESEARCH, TaskType.QUICK_CURRENT_INFO, TaskType.SHOPPING_RESEARCH} and (
                    "web_search" not in metadata["capabilities"]
                    or not free_search_grounding(provider, model_key[1])):
                # A model card listing Search is not a free grounding entitlement: on the free API only the models in
                # tool_policies.google_search_grounding ground answers (3.x Flash: "Not available", 2026-10-07).
                rejected.append({"provider": provider, "model_id": model_key[1], "reason": "no-free-search-grounding"})
                continue
            if (not request.required_modalities.issubset(metadata["input_modalities"])
                    or not request.required_capabilities.issubset(metadata["capabilities"])
                    or (metadata["context_window"] and (request.estimated_input_tokens or 0) + (request.max_output or 0) > metadata["context_window"])
                    or (not metadata["context_window"] and (request.estimated_input_tokens or 0) > 4096)):
                rejected.append({"provider": provider, "model_id": model_key[1], "reason": "unsupported-capability-or-context"})
                continue
            model = ProviderModel(
                provider=provider,
                model_id=str(row.get("model_id") or ""),
                display_name=str(row.get("requested_model_id") or row.get("model_id") or ""),
                credential_scope=scope,
                available=True,
                free_eligible=True,
                free_verified=True,
                free_plan_available=True,
                reliability_class=ReliabilityClass.VERIFIED_HEALTHY,
                routing_allowed=True,
                **metadata,
                status="ACTIVE",
                metadata={
                    "source": "local_verified_registry",
                    "roles": list(row.get("roles") or []),
                    "shared_registry_status": status,
                    "user_credential_logical_route": user_google_logical_route,
                    "discovery_access": payload.get("discovery_access", {}),
                },
            )
            mapped_rank, mapped_label = preference_rank(
                request.task_type,
                provider,
                model.model_id,
                model.display_name,
                request.estimated_input_tokens,
            )
            rank = mapped_rank if mapped_rank is not None else 100 + order_index.get(
                identifier, len(order_index) + len(candidates)
            )
            # Quality rank is never inverted by a cheap model's unused quota.
            # Every OpenRouter free model shares the same late account pool.
            fallback_stage = 1000 if provider == "openrouter" else rank + 1
            if provider == "openrouter":
                rank = 500 if model.model_id == "openrouter/free" else 501 + rank
            candidates.append(RouteCandidate(
                model=model,
                score=1000.0-rank,
                reason=(mapped_label or ("verified local backup registry" if status == "backup" else "verified local registry")),
                fallback_stage=fallback_stage,
            ))
        candidates.sort(key=lambda item: item.score, reverse=True)
        considered = tuple({"provider": item.model.provider, "model_id": item.model.model_id,
                            "score": item.score, "source": "verified local registry"} for item in candidates)
        return RouteDecision(
            task_type=request.task_type,
            candidates=tuple(candidates[:9]),
            considered=considered,
            rejected=tuple(rejected),
            explanation={"registry": "local", "free_only": True, "task_role": role,
                         "fallback": "sequential", "hedging": False},
        )

    def _update_result(self, model: ProviderModel, failure: NormalizedFailure | None) -> None:
        with _THREAD_LOCK:
            payload = self.load()
            changed = False
            for row in payload.get("models", []):
                if row.get("provider") != model.provider or row.get("model_id") != model.model_id:
                    continue
                now = datetime.now(timezone.utc)
                if failure is None:
                    row.update({"status": "active", "failure_count": 0, "circuit_open_until": None,
                                "normalized_error": None, "last_successful_time": now.isoformat().replace("+00:00", "Z")})
                else:
                    failures = int(row.get("failure_count") or 0) + 1
                    row["failure_count"] = failures
                    row["normalized_error"] = failure.failure_class.value.casefold().replace("_", "-")
                    if failure.auth_error:
                        row["status"] = "unauthorized"
                    elif failure.failure_class == FailureClass.MODEL_REMOVED:
                        row["status"] = "invalid-model"
                    elif failure.quota_exhausted or failure.failure_class == FailureClass.RATE_LIMIT:
                        row["status"] = "quota-exhausted"
                    else:
                        row["status"] = "temporarily-disabled"
                    if failures >= 3 or row["status"] in {"quota-exhausted", "temporarily-disabled"}:
                        seconds = max(int(failure.cooldown_seconds or 0), min(3600, max(60, (2 ** min(failures, 6)) * 30)))
                        row["circuit_open_until"] = (now + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
                row["last_checked"] = now.isoformat().replace("+00:00", "Z")
                changed = True
                break
            if changed:
                payload["activation_ready"] = any(row.get("status") == "active" for row in payload.get("models", []))
                _atomic_write(payload)

    def success(self, model: ProviderModel) -> None:
        if model.credential_scope.value.startswith('USER_'):
            return
        self._update_result(model, None)

    def failure(self, model: ProviderModel, failure: NormalizedFailure) -> None:
        if (failure.auth_error or failure.failure_class == FailureClass.AUTHENTICATION
                or failure.quota_exhausted or failure.failure_class in {FailureClass.RATE_LIMIT, FailureClass.QUOTA_EXHAUSTED}):
            # Credential/quota eligibility is not logical-model health. The
            # governor owns its scope and reset; do not add a second circuit.
            return
        if model.credential_scope.value.startswith('USER_'):
            return
        # Authentication is credential-specific. The logical Google model may
        # still be healthy through another managed credential, so keep model
        # health separate from managed-key health.
        if model.provider == "google" or failure.unsupported or failure.failure_class == FailureClass.BAD_REQUEST:
            # QuotaGovernor/CredentialRouter already record this exact
            # credential/model scope. Never poison other Google credentials
            # or other capabilities through the shared logical catalog row.
            return
        self._update_result(model, failure)

    def safe_status(self) -> dict[str, Any]:
        payload = self.load()
        counts: dict[str, int] = {}
        for row in payload.get("models", []):
            status = str(row.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return {"generated_at": payload.get("generated_at"), "stale": self.is_stale(),
                "refresh_running": self.refresh_running(), "activation_ready": self.is_ready(),
                "counts": counts, "routing": payload.get("routing", {}),
                "last_refresh": payload.get("last_refresh")}

    def safe_health_report(self) -> dict[str, Any]:
        """Return a secret-free factual catalog/routing snapshot for Owner UI."""

        payload = self.load()
        models: list[dict[str, Any]] = []
        eligible: list[dict[str, Any]] = []
        ineligible: list[dict[str, Any]] = []
        for row in payload.get("models", []):
            status = str(row.get("status") or "unknown")
            confidence = str(row.get("free_status_confidence") or "").casefold()
            free_verified = bool(
                any(marker in confidence for marker in ("official-free", "supplied-free", "positive-free"))
                and not any(marker in confidence for marker in ("unknown", "unconfirmed", "paid", "zero"))
            )
            routing_eligible = bool(
                status in {"active", "backup"}
                and row.get("output_valid") is True
                and free_verified
                and list(row.get("roles") or [])
                and row.get("specialist_only") is not True
            )
            if row.get("circuit_open_until"):
                health = "Cooldown"
            elif routing_eligible:
                health = "Healthy"
            elif status in {"active", "backup"}:
                health = "Catalog Visible / Not Routing Verified"
            elif status == "quota-exhausted":
                health = "Quota Limited"
            elif status == "unauthorized":
                health = "Auth Error"
            elif status in {"invalid-model", "removed"}:
                health = "Model Missing"
            elif status in {"deprecated", "decommissioned"}:
                health = "Model Deprecated"
            elif status in {"unsupported", "unknown-pricing"}:
                health = "Unsupported" if status == "unsupported" else "Unknown / Needs Verification"
            else:
                health = "Unknown / Needs Verification"
            item = {
                "provider": str(row.get("provider") or "unknown"),
                "model_id": str(row.get("model_id") or row.get("requested_model_id") or "unknown"),
                "capability": str(row.get("capability") or "unknown"),
                "credential_scope": _SCOPES.get(str(row.get("provider") or ""), "unknown").value
                    if str(row.get("provider") or "") in _SCOPES else "specialist",
                "status": status,
                "health": health,
                "roles": list(row.get("roles") or []),
                "cooldown_until": row.get("circuit_open_until"),
                "last_checked": row.get("last_checked"),
                "last_successful_time": row.get("last_successful_time"),
                "normalized_error": row.get("normalized_error"),
                "catalog_visible": (row.get("discovery") or {}).get("available"),
                "last_discovered_at": row.get("last_discovered_at"),
                "discovered_capabilities": (row.get("discovery") or {}).get("capabilities", []),
                "output_valid": row.get("output_valid") is True,
                "free_verified": free_verified,
                "routing_eligible": routing_eligible,
            }
            models.append(item)
            (eligible if routing_eligible else ineligible).append(item)
        catalogs = {}
        for provider, value in (payload.get("catalogs") or {}).items():
            source = value if isinstance(value, dict) else {}
            catalogs[str(provider)] = {
                key: source.get(key)
                for key in (
                    "http_result", "error", "count", "state", "specialist_only",
                    "free_account_eligible", "routing_activation",
                )
                if source.get(key) is not None
            }
        return {
            "generated_at": payload.get("generated_at"),
            "refresh_after_hours": payload.get("refresh_after_hours") or 24,
            "stale": self.is_stale(),
            "refresh_running": self.refresh_running(),
            "activation_ready": self.is_ready(),
            "catalogs": catalogs,
            "routing_assignments": payload.get("routing", {}),
            "eligible_models": eligible,
            "ineligible_models": ineligible,
            "models": models,
            "catalog_changes": payload.get("catalog_changes") or [],
            "last_refresh": payload.get("last_refresh"),
        }
