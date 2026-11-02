"""Task-first, free-only model registry, quota, and routing policy."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from core.provider_adapters import provider_adapters
from core.request_context import JupiterRequestContext, request_context
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
from services.architecture_store import ArchitectureStore
from services.freshness import is_fresh_information_request
from services.task_preferences import preference_rank, workload_allowed


_CODING = re.compile(r"(?i)\b(code|coding|python|javascript|typescript|kotlin|java|bug|repo|api|sql|html|css)\b")
_MATH = re.compile(r"(?i)\b(calculate|equation|algebra|geometry|probability|derivative|integral|math)\b")
_REASONING = re.compile(r"(?i)\b(prove|reason|analy[sz]e deeply|compare tradeoffs|step by step|complex)\b")
_SUMMARY = re.compile(r"(?i)\b(summarize|summary|tl;dr|condense)\b")
_WEATHER = re.compile(r"(?i)\b(weather|forecast|rain|temperature|umbrella|outside)\b")
_SHOPPING = re.compile(r"(?i)\b(buy|purchase|shopping|recommend|best)\b.{0,80}\b(phone|laptop|tv|camera|headphones|earbuds|product|appliance|watch|tablet)\b")
_WEB_SEARCH = re.compile(
    r"(?i)\b(?:search|look\s+up|find)\s+(?:on\s+)?(?:the\s+)?(?:web|internet|online)\b"
    r"|\b(?:web|internet)\s+search\b"
)


@dataclass(frozen=True)
class TaskRequirements:
    task_type: TaskType
    modalities: frozenset[str] = frozenset({"text"})
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RouteCandidate:
    model: ProviderModel
    score: float
    reason: str
    fallback_stage: int = 2


@dataclass(frozen=True)
class RouteDecision:
    task_type: TaskType
    candidates: tuple[RouteCandidate, ...]
    considered: tuple[dict[str, Any], ...]
    rejected: tuple[dict[str, str], ...]
    explanation: dict[str, Any]


class TaskClassifier:
    """Deterministic first pass; an LLM may later refine only ambiguous cases."""

    @staticmethod
    def classify(messages: list[dict[str, Any]], attachment_mimes: set[str] | None = None) -> TaskRequirements:
        latest = next(
            (str(message.get("content") or "") for message in reversed(messages)
             if message.get("role") == "user"),
            "",
        )
        mimes = attachment_mimes or set()
        if any(mime == "application/pdf" for mime in mimes):
            return TaskRequirements(TaskType.PDF_COMPLEX, frozenset({"text", "pdf"}), frozenset({"pdf"}))
        if any(mime.startswith("image/") for mime in mimes):
            return TaskRequirements(TaskType.VISION_GENERAL, frozenset({"text", "image"}), frozenset({"vision"}))
        if _WEATHER.search(latest):
            return TaskRequirements(TaskType.WEATHER)
        if _WEB_SEARCH.search(latest):
            return TaskRequirements(
                TaskType.WEB_RESEARCH,
                frozenset({"text"}),
                frozenset({"web_search"}),
            )
        if _SHOPPING.search(latest):
            return TaskRequirements(
                TaskType.WEB_RESEARCH,
                frozenset({"text"}),
                frozenset({"web_search"}),
            )
        # Current external information outranks topic keywords: "tell me about <product> new <release>" is a
        # web question even when the product name contains "code" (real WT 29-Sep: routed FAST_CODING, no search).
        if is_fresh_information_request(latest):
            return TaskRequirements(
                TaskType.WEB_RESEARCH,
                frozenset({"text"}),
                frozenset({"web_search"}),
            )
        if _CODING.search(latest):
            return TaskRequirements(TaskType.CODING)
        if _MATH.search(latest):
            return TaskRequirements(TaskType.MATH)
        if _REASONING.search(latest):
            return TaskRequirements(TaskType.HARD_REASONING)
        if _SUMMARY.search(latest):
            return TaskRequirements(TaskType.SUMMARIZATION)
        if len(latest.split()) <= 12:
            return TaskRequirements(TaskType.CASUAL_CHAT)
        # Ordinary conversation is not automatically difficult at 13 words.
        return TaskRequirements(TaskType.COMPLEX_CHAT if len(latest) > 1200 else TaskType.CASUAL_CHAT)


_QUALITY_KEY = {
    TaskType.INTENT_CLASSIFICATION: "lightweight",
    TaskType.CAPABILITY_CLASSIFICATION: "lightweight",
    TaskType.CASUAL_CHAT: "chat",
    TaskType.COMPLEX_CHAT: "chat",
    TaskType.HARD_REASONING: "reasoning",
    TaskType.MATH: "reasoning",
    TaskType.CODING: "coding",
    TaskType.FAST_CODING: "fast_coding",
    TaskType.HUGE_CONTEXT: "reasoning",
    TaskType.FINAL_SYNTHESIS: "reasoning",
    TaskType.VISION_GENERAL: "vision",
    TaskType.VISION_UI: "vision",
    TaskType.VIDEO_UNDERSTANDING: "vision",
    TaskType.OCR: "vision",
    TaskType.MEMORY_EXTRACTION: "memory",
    TaskType.MEMORY_CURATOR: "memory",
    TaskType.MEMORY_CANDIDATE_DETECTION: "lightweight",
    TaskType.MEMORY_TIER_CLASSIFICATION: "memory",
    TaskType.MEMORY_FACT_EXTRACTION: "memory",
    TaskType.MEMORY_RELATIONSHIP_EXTRACTION: "memory",
    TaskType.MEMORY_DUPLICATE_CONSOLIDATION: "memory",
    TaskType.MEMORY_CONTRADICTION: "reasoning",
    TaskType.MEMORY_RERANKING: "memory",
    TaskType.MEMORY_P0_JUDGE: "reasoning",
    TaskType.PERSONALITY_EXTRACTION: "personality",
    TaskType.TOPIC_DETECTION: "lightweight",
    TaskType.SUMMARIZATION: "summarization",
    TaskType.STRUCTURED_EXTRACTION: "structured",
    TaskType.AUDIO_TRANSCRIPTION: "audio_transcription",
    TaskType.AUDIO_DIFFICULT: "audio_difficult",
    TaskType.LIVE_CONVERSATION: "live",
    TaskType.TTS: "tts",
    TaskType.IMAGE_GENERATION: "image_generation",
    TaskType.EMBEDDING: "embeddings",
    TaskType.WEB_RESEARCH: "research",
    TaskType.QUICK_CURRENT_INFO: "research",
    TaskType.SHOPPING_RESEARCH: "research",
    TaskType.TRANSLATION: "summarization",
    TaskType.PDF_EXTRACTION: "pdf",
    TaskType.PDF_SIMPLE: "pdf",
    TaskType.PDF_COMPLEX: "pdf",
    TaskType.EMAIL_READING: "summarization",
    TaskType.EMAIL_WRITING: "summarization",
    TaskType.PLANNING: "reasoning",
    TaskType.REVIEWING: "reviewing",
}


class ModelRegistry:
    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    def refresh_provider(
        self,
        provider: str,
        credential_scope: str,
        *,
        user_id: str | None = None,
        credential_resolver=None,
    ) -> int:
        """Explicit discovery entrypoint. Never called by import/startup."""

        adapter = provider_adapters().get((provider, credential_scope))
        if adapter is None:
            raise ValueError("Unknown provider/credential-scope pair.")
        if credential_scope == CredentialScope.USER_GOOGLE.value:
            if not user_id or credential_resolver is None:
                raise ValueError("USER_GOOGLE discovery requires an authenticated resolver.")
            with request_context(JupiterRequestContext(
                user_id=user_id, credential_resolver=credential_resolver
            )):
                models = [
                    ProviderModel(**{
                        **model.__dict__,
                        "credential_owner_id": user_id,
                    })
                    for model in adapter.list_models()
                ]
        else:
            models = adapter.list_models()
        from config.model_seeds import preference_metadata
        from config.model_audit import capability_evidence, classify_evidence, evidence_for

        discovered: list[ProviderModel] = []
        for model in models:
            catalog_fingerprint = hashlib.sha256(json.dumps(
                model.metadata,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")).hexdigest()
            preference = preference_metadata(model.provider, model.model_id, model.display_name)
            audit = evidence_for(model.provider, model.model_id)
            audit_classification = classify_evidence(audit)
            audited_capability = capability_evidence(
                str((audit or {}).get("capability") or ""),
                str((audit or {}).get("failure_reason") or ""),
            )
            audit_has_capability = bool(
                audited_capability["input_modalities"]
                or audited_capability["output_modalities"]
                or audited_capability["capabilities"]
            )
            catalog_modalities_explicit = bool(
                model.metadata.get("catalog_modalities_explicit")
            )
            audit_free = bool((audit or {}).get("free_verified"))
            current_catalog_free = model.metadata.get("catalog_free_status") == "free"
            current_catalog_paid = model.metadata.get("catalog_free_status") == "paid"
            hard_paid = (
                current_catalog_paid
                or (
                    (audit or {}).get("result") == "PAID_PLAN_ONLY"
                    and not current_catalog_free
                )
            )
            if current_catalog_paid:
                audit_classification = {
                    "reliability_class": ReliabilityClass.HARD_BLOCKED.value,
                    "status": "INCOMPATIBLE",
                    "routing_allowed": False,
                    "initial_health": HealthState.UNAVAILABLE.value,
                    "cooldown_seconds": None,
                    "block_reason": "CURRENT_CATALOG_PAID",
                }
            elif (audit or {}).get("result") == "PAID_PLAN_ONLY" and current_catalog_free:
                audit_classification = classify_evidence(None)
                audit_classification["block_reason"] = "FREE_STATUS_CHANGED_AWAITING_HEALTH_CHECK"
            elif audit is None and not current_catalog_free:
                audit_classification = {
                    "reliability_class": ReliabilityClass.HARD_BLOCKED.value,
                    "status": "INCOMPATIBLE",
                    "routing_allowed": False,
                    "initial_health": HealthState.UNAVAILABLE.value,
                    "cooldown_seconds": None,
                    "block_reason": "FREE_STATUS_UNVERIFIED",
                }
            if not model.available:
                audit_classification = {
                    "reliability_class": ReliabilityClass.REMOVED_OR_INCOMPATIBLE.value,
                    "status": model.status if model.status in {"DEPRECATED", "REMOVED"} else "INCOMPATIBLE",
                    "routing_allowed": False,
                    "initial_health": HealthState.UNAVAILABLE.value,
                    "cooldown_seconds": None,
                    "block_reason": "CURRENT_PROVIDER_CATALOG_LIFECYCLE",
                }
            free_verified = False if hard_paid else model.free_verified or audit_free
            free_eligible = False if hard_paid else model.free_eligible or audit_free
            free_plan_available = False if hard_paid else model.free_plan_available or audit_free
            routing_allowed = bool(
                audit_classification["routing_allowed"]
                and model.available
                and free_verified
                and free_eligible
                and free_plan_available
                and not (
                    audit_classification["reliability_class"]
                    == ReliabilityClass.PROBATIONARY_LAST_RESORT.value
                    and not audit_has_capability
                )
            )
            specialist_contract_pending = bool(model.metadata.get("specialist_only"))
            if specialist_contract_pending:
                routing_allowed = False
            annotated = replace(
                model,
                family=(
                    preference.get("family")
                    or (str(model.metadata.get("family")) if model.metadata.get("family") else None)
                ),
                free_eligible=free_eligible,
                free_verified=free_verified,
                free_plan_available=free_plan_available,
                reliability_class=ReliabilityClass(audit_classification["reliability_class"]),
                routing_allowed=routing_allowed,
                input_modalities=(
                    audited_capability["input_modalities"]
                    if audit_has_capability and not catalog_modalities_explicit
                    else model.input_modalities
                ),
                output_modalities=(
                    audited_capability["output_modalities"]
                    if audit_has_capability and not catalog_modalities_explicit
                    else model.output_modalities
                ),
                capabilities=frozenset(
                    set(model.capabilities) | set(audited_capability["capabilities"])
                ),
                status=audit_classification["status"],
                metadata={
                    **model.metadata,
                    "source": "provider_catalog",
                    "catalog_evidence_fingerprint": catalog_fingerprint,
                    "preference_roles": preference["preference_roles"],
                    "preference_weight": preference["preference_weight"],
                    "audit_bootstrap": ({
                        "result": audit.get("result"),
                        "http_status": audit.get("http_status"),
                        "latency_ms": audit.get("latency_ms"),
                        "failure_reason": audit.get("failure_reason"),
                        "capability": audit.get("capability"),
                    } if audit else None),
                    "routing_block_reason": (
                        "SPECIALIST_CONTRACT_PENDING"
                        if specialist_contract_pending
                        else audit_classification["block_reason"]
                    ),
                },
            )
            self.store.upsert_model(annotated)
            if audit:
                bootstrap_result = (
                    "PAID_PLAN_ONLY" if current_catalog_paid
                    else "MODEL_REMOVED" if not model.available
                    else str(audit.get("result") or "")
                )
                self.store.bootstrap_audit_health(
                    annotated,
                    health_state=audit_classification["initial_health"],
                    cooldown_seconds=audit_classification["cooldown_seconds"],
                    audit_result=bootstrap_result,
                    latency_ms=audit.get("latency_ms"),
                )
            discovered.append(annotated)
        owner_id = user_id if credential_scope == CredentialScope.USER_GOOGLE.value else "*"
        if discovered:
            self.store.reconcile_catalog(
                provider=provider,
                credential_scope=credential_scope,
                credential_owner_id=owner_id,
                seen_model_ids=[model.model_id for model in discovered],
            )
        self.store.update_health(
            provider,
            "*",
            credential_scope,
            owner_id,
            HealthState.HEALTHY.value,
            success=True,
        )
        self.store.mark_credential_scope_status(
            provider, credential_scope, owner_id, "ACTIVE"
        )
        return len(models)

    def prepare_seed_candidates(self) -> int:
        """Return preference-rule count; guessed model IDs are never persisted."""

        from config.model_seeds import MODEL_FAMILY_PREFERENCES

        return len(MODEL_FAMILY_PREFERENCES)

    def recovery_candidates(
        self,
        *,
        provider: str | None = None,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Explicit background entrypoint; never invoked by foreground routing."""

        return self.store.health_recheck_candidates(provider=provider, limit=limit)

    def record_free_verification(
        self,
        *,
        provider: str,
        model_id: str,
        credential_scope: str,
        evidence_source: str,
        credential_owner_id: str = "*",
        input_price: float | None = None,
        output_price: float | None = None,
        free_plan_accessible: bool | None = None,
    ) -> None:
        allowed_sources = {
            "provider_pricing_metadata",
            "explicit_free_model_designation",
            "verified_free_plan_access",
        }
        if evidence_source not in allowed_sources:
            raise ValueError("Unknown evidence cannot verify a free route.")
        if (
            credential_scope == CredentialScope.USER_GOOGLE.value
            and credential_owner_id == "*"
        ):
            raise ValueError("USER_GOOGLE free verification requires its authenticated owner.")
        price_verified = input_price == 0 and output_price == 0
        designation_verified = evidence_source == "explicit_free_model_designation"
        plan_verified = (
            evidence_source == "verified_free_plan_access" and free_plan_accessible is True
        )
        verified = price_verified or designation_verified or plan_verified
        self.store.set_free_verification(
            provider=provider,
            model_id=model_id,
            credential_scope=credential_scope,
            credential_owner_id=credential_owner_id,
            verified=verified,
            evidence={
                "source": evidence_source,
                "input_price": input_price,
                "output_price": output_price,
                "free_plan_accessible": free_plan_accessible,
            },
        )


class FreeOnlyRouter:
    STAGE_CANDIDATE_LIMITS = {1: 5, 2: 4, 3: 1, 4: 1, 5: 1}
    MAX_PROBATIONARY_CANDIDATES = 2

    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    @staticmethod
    def _scope_priority(task_type: TaskType) -> list[str]:
        google = [
            CredentialScope.USER_GOOGLE.value,
            CredentialScope.JUPITER_SHARED_GOOGLE.value,
        ]
        if task_type in {TaskType.AUDIO_TRANSCRIPTION, TaskType.AUDIO_DIFFICULT}:
            return [
                CredentialScope.JUPITER_SHARED_GROQ.value,
                CredentialScope.JUPITER_SHARED_CLOUDFLARE.value,
                CredentialScope.USER_GOOGLE.value,
                CredentialScope.JUPITER_SHARED_GOOGLE.value,
                CredentialScope.JUPITER_SHARED_NVIDIA.value,
                CredentialScope.JUPITER_SHARED_OPENROUTER.value,
                CredentialScope.JUPITER_SHARED_OPENCODE.value,
            ]
        if task_type == TaskType.EMBEDDING:
            return google
        if task_type == TaskType.IMAGE_GENERATION:
            return [
                CredentialScope.JUPITER_SHARED_CLOUDFLARE.value,
                *google,
                CredentialScope.JUPITER_SHARED_NVIDIA.value,
                CredentialScope.JUPITER_SHARED_OPENROUTER.value,
                CredentialScope.JUPITER_SHARED_OPENCODE.value,
                CredentialScope.JUPITER_SHARED_GROQ.value,
            ]
        if task_type == TaskType.TTS:
            return google + [
                CredentialScope.JUPITER_SHARED_CLOUDFLARE.value,
                CredentialScope.JUPITER_SHARED_GROQ.value,
                CredentialScope.JUPITER_SHARED_NVIDIA.value,
                CredentialScope.JUPITER_SHARED_OPENROUTER.value,
                CredentialScope.JUPITER_SHARED_OPENCODE.value,
            ]
        if task_type in {
            TaskType.HARD_REASONING,
            TaskType.MATH,
            TaskType.CODING,
            TaskType.FAST_CODING,
            TaskType.COMPLEX_CHAT,
            TaskType.PLANNING,
            TaskType.REVIEWING,
        }:
            remaining = [
                CredentialScope.JUPITER_SHARED_GROQ.value,
                CredentialScope.JUPITER_SHARED_NVIDIA.value,
                CredentialScope.JUPITER_SHARED_OPENCODE.value,
            ]
        else:
            remaining = [
                CredentialScope.JUPITER_SHARED_GROQ.value,
                CredentialScope.JUPITER_SHARED_OPENCODE.value,
                CredentialScope.JUPITER_SHARED_NVIDIA.value,
            ]
        # General model choice is task/quality/quota driven. Google credentials
        # are not a priority bonus merely because two legitimate credential
        # sources may exist after a Google model is selected.
        return remaining + google + [
            CredentialScope.JUPITER_SHARED_OPENROUTER.value,
            CredentialScope.JUPITER_SHARED_CLOUDFLARE.value,
        ]

    def route(self, request: AIRequest) -> RouteDecision:
        priority_order = self._scope_priority(request.task_type)
        rows = self.store.route_candidates(
            request.user_id,
            priority_order,
            set(request.required_modalities),
            set(request.required_capabilities),
            request.estimated_input_tokens,
        )
        quality_key = _QUALITY_KEY.get(request.task_type, "chat")
        scope_rank = {scope: index for index, scope in enumerate(priority_order)}
        considered: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        staged: dict[int, list[RouteCandidate]] = {1: [], 2: [], 3: [], 4: [], 5: []}
        from config.provider_catalog import free_search_grounding, routing_gate
        for row in rows:
            if request.task_type in {TaskType.WEB_RESEARCH, TaskType.QUICK_CURRENT_INFO, TaskType.SHOPPING_RESEARCH} and (
                    "web_search" not in set(row.get("capabilities") or [])
                    or not free_search_grounding(str(row.get("provider") or ""), str(row.get("model_id") or ""))):
                continue
            if not workload_allowed(request.task_type, str(row.get("provider") or ""), str(row.get("model_id") or "")):
                continue
            # Same default free-only product policy as the local registry (one gate, two stores).
            policy_allowed, policy_reason = routing_gate(
                str(row.get("provider") or ""), str(row.get("model_id") or ""), request.privacy_class.value)
            if not policy_allowed:
                rejected.append({"provider": row["provider"], "model": row["model_id"], "reason": policy_reason})
                continue
            if request.task_type == TaskType.EMBEDDING and "embeddings" not in set(
                row.get("capabilities") or []
            ):
                continue
            # Defense in depth: database constraints are not the only free gate.
            if not (
                row.get("free_eligible") is True
                and row.get("free_verified") is True
                and row.get("free_plan_available") is True
            ):
                continue
            scope = row["credential_scope"]
            if request.privacy_class in {PrivacyClass.BIOMETRIC, PrivacyClass.SECRET}:
                rejected.append({
                    "provider": row["provider"],
                    "model": row["model_id"],
                    "reason": "specialist_or_local_processing_required",
                })
                continue
            if (
                request.privacy_class == PrivacyClass.SENSITIVE
                and scope != CredentialScope.USER_GOOGLE.value
            ):
                rejected.append({
                    "provider": row["provider"],
                    "model": row["model_id"],
                    "reason": "user_scoped_credential_required",
                })
                continue
            if (
                request.privacy_class == PrivacyClass.PRIVATE
                and scope not in {
                    CredentialScope.USER_GOOGLE.value,
                    CredentialScope.JUPITER_SHARED_GOOGLE.value,
                }
            ):
                rejected.append({
                    "provider": row["provider"],
                    "model": row["model_id"],
                    "reason": "trusted_private_route_required",
                })
                continue
            scores = row.get("quality_scores") or {}
            quality = float(scores.get(quality_key, 0.5) or 0.5)
            reliability = float(
                0.5 if row.get("success_rate") is None else row["success_rate"]
            )
            health_score = float(
                0.5 if row.get("health_score") is None else row["health_score"]
            )
            latency = float(row.get("latency_avg") or 2500)
            priority = 0
            metadata = row.get("metadata_json") or {}
            preference_roles = set(metadata.get("preference_roles") or [])
            preference_bonus = (
                min(float(metadata.get("preference_weight") or 0), 40)
                if quality_key in preference_roles or request.task_type.value.casefold() in preference_roles
                else 0
            )
            map_rank, map_label = preference_rank(
                request.task_type,
                str(row.get("provider") or ""),
                str(row.get("model_id") or ""),
                str(row.get("display_name") or ""),
                request.estimated_input_tokens,
            )
            if map_rank is not None:
                preference_bonus += max(12.0, 70.0 - map_rank * 9.0)
            privacy_bonus = (
                8
                if request.privacy_class == PrivacyClass.PRIVATE
                and scope == CredentialScope.USER_GOOGLE.value
                else 0
            )
            score = (
                quality * 45 + reliability * 20 + health_score * 15
                + priority * 5 + privacy_bonus + preference_bonus
                - min(latency / 1000, 15)
                - min(float(row.get("consecutive_failures") or 0) * 5, 20)
            )
            reliability_class = ReliabilityClass(
                row.get("reliability_class") or ReliabilityClass.CANDIDATE.value
            )
            model = ProviderModel(
                provider=row["provider"],
                model_id=row["model_id"],
                display_name=row["display_name"],
                credential_scope=CredentialScope(row["credential_scope"]),
                family=row.get("family"),
                credential_owner_id=row.get("credential_owner_id") or "*",
                available=True,
                free_eligible=True,
                free_verified=True,
                free_plan_available=True,
                reliability_class=reliability_class,
                routing_allowed=bool(row.get("routing_allowed")),
                input_modalities=frozenset(row.get("input_modalities") or []),
                output_modalities=frozenset(row.get("output_modalities") or []),
                capabilities=frozenset(row.get("capabilities") or []),
                context_window=row.get("context_window"),
                max_output=row.get("max_output"),
                status=row.get("status") or "ACTIVE",
                metadata=metadata,
            )
            # Bytez catalog candidates are deliberately not generic routes.
            # Their task contracts (video, detection, segmentation, document
            # QA, etc.) are wired only by a later specialist integration pass.
            if model.provider == "bytez" or metadata.get("specialist_only"):
                rejected.append({
                    "provider": model.provider,
                    "model": model.model_id,
                    "reason": "specialist_contract_pending",
                })
                continue
            considered.append({
                "provider": model.provider,
                "model": model.model_id,
                "credential_scope": model.credential_scope.value,
                "score": round(score, 3),
                "reliability_class": reliability_class.value,
                "preference_rank": map_rank,
                "preference_label": map_label,
            })
            if model.provider == "openrouter":
                stage = 5
                if model.model_id == "openrouter/free":
                    score += 1000  # The dynamic free router is the late default, not Laguna.
            elif reliability_class == ReliabilityClass.VERIFIED_HEALTHY:
                stage = 1 if map_rank is not None or preference_bonus > 0 else 2
            elif reliability_class == ReliabilityClass.TEMPORARY_FAILURE:
                stage = 3
            elif reliability_class == ReliabilityClass.PROBATIONARY_LAST_RESORT:
                stage = 4
            else:
                continue
            staged[stage].append(RouteCandidate(
                model=model,
                score=score,
                reason=(
                    f"Stage {stage}: compatible verified-free {quality_key} route; "
                    f"reliability={reliability_class.value}; score={score:.3f}"
                ),
                fallback_stage=stage,
            ))

        ordered: list[RouteCandidate] = []
        used_logical_models: set[tuple[str, str]] = set()
        for stage in range(1, 6):
            ranked = sorted(staged[stage], key=lambda candidate: candidate.score, reverse=True)
            diverse: list[RouteCandidate] = []
            deferred: list[RouteCandidate] = []
            used_providers: set[str] = set()
            for candidate in ranked:
                logical_key = (candidate.model.provider, candidate.model.model_id)
                if logical_key in used_logical_models:
                    continue
                # Preserve quality/task ranking. Provider diversity does not
                # justify skipping an independent same-provider model bucket.
                diverse.append(candidate)
                used_providers.add(candidate.model.provider)
                used_logical_models.add(logical_key)
            stage_candidates = diverse + deferred
            limit = self.STAGE_CANDIDATE_LIMITS[stage]
            if stage == 4:
                limit = min(limit, self.MAX_PROBATIONARY_CANDIDATES)
            ordered.extend(stage_candidates[:limit])
        candidates = tuple(ordered)
        explanation = {
            "task_type": request.task_type.value,
            "required_capabilities": sorted(request.required_capabilities),
            "required_modalities": sorted(request.required_modalities),
            "candidates_considered": considered,
            "rejected_routes": rejected,
            "fallback_stages": {
                str(stage): len(staged[stage]) for stage in range(1, 6)
            },
            "selected_route": (
                {"provider": candidates[0].model.provider, "model": candidates[0].model.model_id}
                if candidates else None
            ),
            "selection_reason": (
                candidates[0].reason if candidates else "No compatible verified-free healthy route."
            ),
        }
        return RouteDecision(
            task_type=request.task_type,
            candidates=candidates,
            considered=tuple(considered),
            rejected=tuple(rejected),
            explanation=explanation,
        )


class QuotaAndCircuitManager:
    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    def success(self, model: ProviderModel) -> None:
        self.store.update_health(
            model.provider,
            model.model_id,
            model.credential_scope.value,
            model.credential_owner_id,
            HealthState.HEALTHY.value,
            success=True,
        )
        self.store.promote_model_on_success(model)

    def failure(self, model: ProviderModel, failure: NormalizedFailure) -> None:
        auth_failed = failure.auth_error or failure.failure_class == FailureClass.AUTHENTICATION
        if not auth_failed and (failure.quota_exhausted or failure.failure_class in {
            FailureClass.RATE_LIMIT, FailureClass.QUOTA_EXHAUSTED,
        }):
            # AIGateway already records this through QuotaGovernor, using the
            # exact credential/project/model or shared pool. Do not overwrite
            # that scope or count quota pressure as a technical health failure.
            return
        health = HealthState.DEGRADED
        cooldown = failure.cooldown_seconds
        if auth_failed:
            health = HealthState.AUTH_FAILED
            self.store.mark_credential_scope_status(
                model.provider,
                model.credential_scope.value,
                model.credential_owner_id,
                "AUTH_FAILED",
            )
            self.store.update_health(
                model.provider,
                "*",
                model.credential_scope.value,
                model.credential_owner_id,
                HealthState.AUTH_FAILED.value,
                failure_class=failure.failure_class.value,
                success=False,
            )
            return
        elif failure.failure_class in {FailureClass.SERVER, FailureClass.TIMEOUT}:
            health = HealthState.UNAVAILABLE
            cooldown = cooldown or 30
        if failure.failure_class == FailureClass.MODEL_REMOVED:
            self.store.mark_model_runtime_state(
                model,
                reliability_class=ReliabilityClass.REMOVED_OR_INCOMPATIBLE.value,
                status="INCOMPATIBLE",
                routing_allowed=False,
                reason="NEEDS_CATALOG_REDISCOVERY",
            )
            self.store.enqueue_outbox(
                user_id=None,
                event_type="PROVIDER_CATALOG_REFRESH",
                aggregate_type="provider",
                aggregate_id=f"{model.provider}:{model.credential_scope.value}",
                payload={"reason": "model_removed", "provider": model.provider},
                idempotency_key=(
                    f"catalog:{model.provider}:{model.credential_scope.value}:model_removed:"
                    f"{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H')}"
                ),
            )
        elif failure.reason_code == "MODEL_NOT_FREE":
            self.store.set_free_verification(
                provider=model.provider,
                model_id=model.model_id,
                credential_scope=model.credential_scope.value,
                credential_owner_id=model.credential_owner_id,
                verified=False,
                evidence={"source": "provider_paid_plan_rejection"},
            )
            self.store.mark_model_runtime_state(
                model,
                reliability_class=ReliabilityClass.HARD_BLOCKED.value,
                status="INCOMPATIBLE",
                routing_allowed=False,
                reason="MODEL_NOT_FREE",
            )
        elif failure.reason_code in {
            "TERMS_REQUIRED", "LICENSING_REQUIRED", "PERMISSION_DENIED", "API_DISABLED"
        }:
            self.store.mark_model_runtime_state(
                model,
                reliability_class=ReliabilityClass.HARD_BLOCKED.value,
                status="INCOMPATIBLE",
                routing_allowed=False,
                reason=failure.reason_code,
            )
        elif failure.failure_class == FailureClass.UNSUPPORTED or failure.unsupported:
            self.store.mark_model_runtime_state(
                model,
                reliability_class=ReliabilityClass.REMOVED_OR_INCOMPATIBLE.value,
                status="INCOMPATIBLE",
                routing_allowed=False,
                reason="INCOMPATIBLE_ENDPOINT",
            )
        elif failure.failure_class in {
            FailureClass.SERVER, FailureClass.TIMEOUT
        }:
            self.store.mark_model_runtime_state(
                model,
                reliability_class=ReliabilityClass.TEMPORARY_FAILURE.value,
                status="COOLDOWN",
                routing_allowed=True,
                reason=failure.failure_class.value,
            )
        elif model.reliability_class == ReliabilityClass.PROBATIONARY_LAST_RESORT:
            cooldown = cooldown or 300
            health = HealthState.DEGRADED
            self.store.mark_model_runtime_state(
                model,
                reliability_class=ReliabilityClass.TEMPORARY_FAILURE.value,
                status="COOLDOWN",
                routing_allowed=True,
                reason="PROBATIONARY_ATTEMPT_FAILED",
            )
        self.store.update_health(
            model.provider,
            model.model_id,
            model.credential_scope.value,
            model.credential_owner_id,
            health.value,
            failure_class=failure.failure_class.value,
            cooldown_seconds=cooldown,
            success=False,
        )
