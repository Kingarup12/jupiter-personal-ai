"""Provider-independent foreground execution with durable failure metadata."""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from contextvars import copy_context
from dataclasses import replace
from typing import Iterator
from uuid import uuid4

from config.provider_settings import database_url, multi_provider_enabled
from core.provider_adapters import provider_adapters
from core.provider_contracts import (
    AIRequest,
    AIResponse,
    AIStreamEvent,
    Attachment,
    FailureClass,
    NormalizedFailure,
    ProviderFailure,
    PrivacyClass,
    TaskType,
)
from core.request_context import current_request_context
from services.architecture_store import ArchitectureStore
from services.model_routing import FreeOnlyRouter, QuotaAndCircuitManager, TaskClassifier
from services.model_discovery import credential_catalog_allows
from services.credential_router import CredentialRouter
from services.quota_governor import quota_governor
from services.search_grounding import requires_grounding
from services.task_orchestrator import TaskOrchestrator


class MultiProviderDisabled(RuntimeError):
    pass


def _empty_response_failure(candidate, last_event=None) -> NormalizedFailure:
    finish = str(getattr(last_event, "finish_reason", "") or "").upper() or "NONE"
    return NormalizedFailure(
        FailureClass.UNKNOWN,
        reason_code="EMPTY_RESPONSE",
        safe_message=f"{candidate.model.provider} returned no answer text (finish={finish[:24]}).",
    )


def _reject_empty_generation(candidate, response) -> None:
    if (str(getattr(response, "content", "") or "").strip() or getattr(response, "structured_output", None)
            or getattr(response, "tool_calls", ())):
        return
    raise ProviderFailure(_empty_response_failure(candidate, response))


class AllFreeRoutesFailed(RuntimeError):
    """Every eligible free route failed. Carries each attempt's failure class (never provider messages or
    credentials) and whether the request needed provider-grounded web search, so the chat can say WHY."""

    def __init__(self, message: str = "FAILED_ALL_FREE_ROUTES", *, failures=(), web_search: bool = False):
        super().__init__(message)
        self.failures = tuple(str(failure) for failure in failures)
        self.web_search = bool(web_search)


class SpecialistRequired(RuntimeError):
    pass


_LEGACY_TASK_MAP = {
    "MAIN_CHAT": TaskType.COMPLEX_CHAT,
    "EXECUTIVE": TaskType.PLANNING,
    "EMOTION": TaskType.STRUCTURED_EXTRACTION,
    "PERSONALITY": TaskType.PERSONALITY_EXTRACTION,
    "CURIOSITY": TaskType.CASUAL_CHAT,
    "RELATIONSHIP": TaskType.MEMORY_RELATIONSHIP_EXTRACTION,
    "MEMORY_ANALYZER": TaskType.MEMORY_EXTRACTION,
    "DUPLICATE": TaskType.MEMORY_DUPLICATE_CONSOLIDATION,
    "CONTRADICTION": TaskType.MEMORY_CONTRADICTION,
    "VALIDATOR": TaskType.REVIEWING,
    "REWRITER": TaskType.REVIEWING,
    "VOICE_CLEANUP": TaskType.AUDIO_TRANSCRIPTION,
    "TTS": TaskType.TTS,
    "IMAGE_PROMPT": TaskType.STRUCTURED_EXTRACTION,
}


class AIGateway:
    MAX_TOTAL_PROVIDER_ATTEMPTS = 12
    MAX_RETRY_SAME_MODEL = 1
    STREAM_HEARTBEAT_SECONDS = 12.0
    STREAM_FIRST_TOKEN_SECONDS = 40.0
    STREAM_INACTIVITY_SECONDS = 45.0
    STREAM_OVERALL_SECONDS = 180.0

    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or (ArchitectureStore() if database_url() else None)
        if self.store is None:
            from services.local_model_registry import LocalModelRegistry
            self.router = LocalModelRegistry()
            self.quota = self.router
        else:
            self.router = FreeOnlyRouter(self.store)
            self.quota = QuotaAndCircuitManager(self.store)
        self.adapters = provider_adapters()
        self.credentials = CredentialRouter()
        self.governor = quota_governor()

    @staticmethod
    def _adapter_key(candidate) -> tuple[str, str]:
        return candidate.model.provider, candidate.model.credential_scope.value

    def generate(self, request: AIRequest) -> AIResponse:
        if not multi_provider_enabled():
            raise MultiProviderDisabled("Multi-provider routing has not been activated.")
        if request.task_type == TaskType.WEATHER:
            raise SpecialistRequired("OpenWeather facts are required before language generation.")
        if request.task_type in {
            TaskType.AUDIO_TRANSCRIPTION,
            TaskType.AUDIO_DIFFICULT,
            TaskType.TTS,
            TaskType.IMAGE_GENERATION,
            TaskType.EMBEDDING,
            TaskType.OCR,
            TaskType.LIVE_CONVERSATION,
        }:
            raise SpecialistRequired(
                "This task requires its verified specialist execution path."
            )
        request = self._ensure_job(request)
        decision = self.router.route(request)
        if requires_grounding(request) and not decision.candidates:
            return self._web_search_answer(request)
        candidates = self.governor.order_candidates(
            request,
            tuple(candidate for candidate in self.credentials.order_candidates(request, decision.candidates)
                  if credential_catalog_allows(candidate.model, request)),
        )
        failures: list[str] = []
        total_attempts = 0
        previous_candidate = None
        previous_failure = None
        blocked_credentials: set[tuple[str, str]] = set()
        blocked_google_quota_groups: set[str] = set()
        for fallback_number, candidate in enumerate(candidates):
            if total_attempts >= self.MAX_TOTAL_PROVIDER_ATTEMPTS:
                break
            credential_key = self.credentials.credential_key(candidate)
            quota_group = self.credentials.quota_group(candidate)
            if credential_key in blocked_credentials:
                continue
            if quota_group and quota_group in blocked_google_quota_groups:
                continue
            if (
                self.credentials.same_logical_model(previous_candidate, candidate)
                and previous_failure is not None
                and not self.credentials.same_model_credential_fallback_allowed(previous_failure)
            ):
                continue
            adapter = self.adapters.get(self._adapter_key(candidate))
            if adapter is None:
                failures.append("adapter_missing")
                continue
            same_model_retry_budget = (
                self.MAX_RETRY_SAME_MODEL if candidate.fallback_stage <= 2
                and candidate.model.provider != "openrouter" else 0
            )
            for retry_number in range(same_model_retry_budget + 1):
                if total_attempts >= self.MAX_TOTAL_PROVIDER_ATTEMPTS:
                    break
                admission = self.governor.reserve(
                    candidate.model,
                    request.estimated_input_tokens or 0,
                    priority=request.execution_priority,
                )
                if not admission.allowed:
                    failures.append("QUOTA_GOVERNOR_EXHAUSTED")
                    break
                total_attempts += 1
                routing_metadata = {
                    **decision.explanation,
                    "same_model_retry": retry_number,
                    "fallback_stage": candidate.fallback_stage,
                    "total_attempt_number": total_attempts,
                }
                try:
                    response = adapter.generate(request, candidate.model)
                    _reject_empty_generation(candidate, response)
                    response = replace(response, fallback_number=fallback_number)
                    self.governor.success(
                        candidate.model,
                        input_tokens=response.input_usage or 0,
                        output_tokens=response.output_usage or 0,
                        audio_seconds=response.audio_usage or 0,
                        admission=admission,
                        actual_shared_units=response.shared_quota_units,
                    )
                    self.credentials.record_success(candidate)
                    self.quota.success(candidate.model)
                    self._record(
                        request, candidate, response, routing_metadata,
                        fallback_number, None, True, None, None,
                    )
                    if request.job_id and self.store is not None:
                        self.store.checkpoint_job(
                            job_id=request.job_id,
                            user_id=request.user_id,
                            checkpoint={
                                "completed_steps": ["foreground_generation"],
                                "remaining_steps": [],
                                "actual_model": response.actual_model,
                            },
                            partial_output=response.content,
                            status="COMPLETED",
                        )
                    return response
                except ProviderFailure as error:
                    failure = error.failure
                    previous_candidate = candidate
                    previous_failure = failure
                    failures.append(failure.failure_class.value)
                    self._record(
                        request, candidate, None, routing_metadata,
                        fallback_number, failures[-1], False,
                        failure.status_code, failure.failure_class.value,
                        failure.safe_message, failure.reason_code,
                    )
                    safe_retry = (
                        retry_number == 0
                        and retry_number < same_model_retry_budget
                        and failure.retryable
                        and failure.failure_class in {FailureClass.SERVER, FailureClass.TIMEOUT}
                    )
                    if safe_retry:
                        self.governor.release(candidate.model)
                        continue
                    self.credentials.record_failure(candidate, failure)
                    if failure.auth_error or failure.failure_class == FailureClass.AUTHENTICATION:
                        blocked_credentials.add(credential_key)
                    if quota_group and str(failure.quota_scope or "").upper() in {"PROJECT", "ACCOUNT"}:
                        blocked_google_quota_groups.add(quota_group)
                    self.governor.failure(candidate.model, failure)
                    self.quota.failure(candidate.model, failure)
                    break
        if request.job_id and self.store is not None:
            self.store.checkpoint_job(
                job_id=request.job_id,
                user_id=request.user_id,
                checkpoint={"failures": failures, "remaining_steps": ["retry_free_route"]},
                partial_output=None,
                status="FAILED_ALL_FREE_ROUTES",
            )
        raise AllFreeRoutesFailed("FAILED_ALL_FREE_ROUTES", failures=failures, web_search=requires_grounding(request))

    # ── zero-cost web search (Tavily free plan) when no free grounded model exists ─────────────────────────────
    @staticmethod
    def _web_search_results(request: AIRequest) -> list[dict[str, str]]:
        """Search through the fail-closed free-quota guard. Any block is an honest 'search unavailable': there is
        never a paid fallback and the question is never answered from memory as if it were live."""
        from core.logger import Logger
        from services.web_search import SEARCH_PRIVACY_ALLOWED, TavilySearch, WebSearchBlocked, latest_user_text
        reason = ""
        results: list[dict[str, str]] = []
        if request.privacy_class.value not in SEARCH_PRIVACY_ALLOWED:
            reason = "privacy"
        else:
            try:
                results = TavilySearch().search(latest_user_text(request))
                reason = "" if results else "no_results"
            except WebSearchBlocked as blocked:
                reason = blocked.reason
        Logger.info(f"web_search provider=tavily outcome={reason or 'ok'} results={len(results)}")
        if reason:
            error = AllFreeRoutesFailed("FAILED_ALL_FREE_ROUTES", failures=("UNSUPPORTED",), web_search=True)
            error.search_reason = reason
            raise error
        return results

    def _web_search_answer(self, request: AIRequest) -> AIResponse:
        from services.web_search import answer_request, cite
        results = self._web_search_results(request)
        response = self.generate(answer_request(request, results))
        content, evidence = cite(response.content, results)
        return replace(response, content=content, structured_output={"grounding": evidence})

    def _web_search_stream(self, request: AIRequest) -> Iterator[AIStreamEvent]:
        from services.web_search import answer_request, cite
        results = self._web_search_results(request)
        last = None
        for event in self.stream(answer_request(request, results)):
            if event.event_type == "delta":
                last = event
            yield event
        listing = cite("", results)[0]
        yield AIStreamEvent(text=listing, event_type="delta", provider=getattr(last, "provider", ""),
                            requested_model=getattr(last, "requested_model", ""),
                            actual_model=getattr(last, "actual_model", ""))

    def stream(self, request: AIRequest) -> Iterator[AIStreamEvent]:
        if not multi_provider_enabled():
            raise MultiProviderDisabled("Multi-provider routing has not been activated.")
        if request.task_type == TaskType.WEATHER:
            raise SpecialistRequired("OpenWeather facts are required before language generation.")
        if request.task_type in {
            TaskType.AUDIO_TRANSCRIPTION,
            TaskType.AUDIO_DIFFICULT,
            TaskType.TTS,
            TaskType.IMAGE_GENERATION,
            TaskType.EMBEDDING,
            TaskType.OCR,
            TaskType.LIVE_CONVERSATION,
        }:
            raise SpecialistRequired(
                "This task requires its verified specialist execution path."
            )
        request = self._ensure_job(request)
        decision = self.router.route(request)
        if requires_grounding(request) and not decision.candidates:
            yield from self._web_search_stream(request)
            return
        candidates = self.governor.order_candidates(
            request,
            tuple(candidate for candidate in self.credentials.order_candidates(request, decision.candidates)
                  if credential_catalog_allows(candidate.model, request)),
        )
        partial = ""
        failures: list[str] = []
        total_attempts = 0
        previous_candidate = None
        previous_failure = None
        blocked_credentials: set[tuple[str, str]] = set()
        blocked_google_quota_groups: set[str] = set()
        for fallback_number, candidate in enumerate(candidates):
            if total_attempts >= self.MAX_TOTAL_PROVIDER_ATTEMPTS:
                break
            credential_key = self.credentials.credential_key(candidate)
            quota_group = self.credentials.quota_group(candidate)
            if credential_key in blocked_credentials:
                continue
            if quota_group and quota_group in blocked_google_quota_groups:
                continue
            if (
                self.credentials.same_logical_model(previous_candidate, candidate)
                and previous_failure is not None
                and not self.credentials.same_model_credential_fallback_allowed(previous_failure)
            ):
                continue
            adapter = self.adapters.get(self._adapter_key(candidate))
            if adapter is None:
                continue
            continued_request = request
            if partial:
                continued_request = replace(
                    request,
                    messages=[
                        *request.messages,
                        {
                            "role": "system",
                            "content": (
                                "A prior verified-free worker produced the partial output below. "
                                "Continue from its end without repeating completed text.\n\n" + partial
                            ),
                        },
                    ],
                )
            same_model_retry_budget = (
                self.MAX_RETRY_SAME_MODEL if candidate.fallback_stage <= 2
                and candidate.model.provider != "openrouter" else 0
            )
            for retry_number in range(same_model_retry_budget + 1):
                if total_attempts >= self.MAX_TOTAL_PROVIDER_ATTEMPTS:
                    break
                admission = self.governor.reserve(
                    candidate.model,
                    request.estimated_input_tokens or 0,
                    priority=request.execution_priority,
                )
                if not admission.allowed:
                    failures.append("QUOTA_GOVERNOR_EXHAUSTED")
                    break
                total_attempts += 1
                emitted = False
                last_event: AIStreamEvent | None = None
                routing_metadata = {
                    **decision.explanation,
                    "same_model_retry": retry_number,
                    "fallback_stage": candidate.fallback_stage,
                    "total_attempt_number": total_attempts,
                }
                try:
                    for event in self._watch_provider_stream(
                        adapter.stream(continued_request, candidate.model)
                    ):
                        if event.event_type == "heartbeat" or str(event.event_type).startswith("stage:"):
                            yield event
                            continue
                        last_event = event
                        if event.text:
                            # Whitespace-only output is not an answer (same rule as the non-streaming path): it
                            # must fall back to the next eligible model, not surface as "unusable response".
                            if event.text.strip():
                                emitted = True
                            partial += event.text
                        yield event
                    if not emitted:
                        # A stream that completes without any answer text (e.g. a reasoning model that
                        # spent its completion on thinking, or a safety/recitation stop) is not a
                        # success: fall back to the next eligible model instead of surfacing
                        # "unusable response" to the user.
                        raise ProviderFailure(_empty_response_failure(candidate, last_event))
                    usage = last_event.usage if last_event and last_event.usage else {}
                    self.governor.success(
                        candidate.model,
                        input_tokens=usage.get("input", usage.get("prompt_tokens", 0)) or 0,
                        output_tokens=usage.get("output", usage.get("completion_tokens", 0)) or 0,
                        admission=admission,
                        actual_shared_units=self.governor.actual_shared_units({"usage": usage}) if candidate.model.provider == "cloudflare" else None,
                    )
                    self.credentials.record_success(candidate)
                    self.quota.success(candidate.model)
                    stream_response = AIResponse(
                        content="",
                        provider=(last_event.provider if last_event else candidate.model.provider),
                        requested_model=candidate.model.model_id,
                        actual_model=(
                            last_event.actual_model
                            if last_event and last_event.actual_model
                            else candidate.model.model_id
                        ),
                        input_usage=usage.get("input", usage.get("prompt_tokens")),
                        output_usage=usage.get("output", usage.get("completion_tokens")),
                    )
                    self._record(
                        request, candidate, stream_response, routing_metadata,
                        fallback_number, failures[-1] if failures else None,
                        True, None, None,
                    )
                    if request.job_id and self.store is not None:
                        self.store.checkpoint_job(
                            job_id=request.job_id,
                            user_id=request.user_id,
                            checkpoint={
                                "completed_steps": ["foreground_stream"],
                                "remaining_steps": [],
                                "output_length": len(partial),
                            },
                            partial_output=partial,
                            status="COMPLETED",
                        )
                    return
                except ProviderFailure as error:
                    previous_candidate = candidate
                    previous_failure = error.failure
                    failures.append(error.failure.failure_class.value)
                    self._record(
                        request, candidate, None, routing_metadata,
                        fallback_number, failures[-1], False,
                        error.failure.status_code, error.failure.failure_class.value,
                        error.failure.safe_message, error.failure.reason_code,
                    )
                    safe_retry = (
                        not emitted
                        and retry_number == 0
                        and retry_number < same_model_retry_budget
                        and error.failure.retryable
                        and error.failure.failure_class in {FailureClass.SERVER, FailureClass.TIMEOUT}
                    )
                    if safe_retry:
                        self.governor.release(candidate.model)
                        continue
                    self.credentials.record_failure(candidate, error.failure)
                    if error.failure.auth_error or error.failure.failure_class == FailureClass.AUTHENTICATION:
                        blocked_credentials.add(credential_key)
                    if quota_group and str(error.failure.quota_scope or "").upper() in {"PROJECT", "ACCOUNT"}:
                        blocked_google_quota_groups.add(quota_group)
                    self.governor.failure(candidate.model, error.failure)
                    self.quota.failure(candidate.model, error.failure)
                    if request.job_id and emitted and self.store is not None:
                        self.store.checkpoint_job(
                            job_id=request.job_id,
                            user_id=request.user_id,
                            checkpoint={
                                "objective": "Continue the current user request",
                                "completed_output_length": len(partial),
                                "failed_provider": candidate.model.provider,
                                "remaining": "Continue without repeating the checkpointed output.",
                            },
                            partial_output=partial,
                            status="RUNNING",
                        )
                    break
        if request.job_id and self.store is not None:
            self.store.checkpoint_job(
                job_id=request.job_id,
                user_id=request.user_id,
                checkpoint={"failures": failures, "remaining_steps": ["retry_free_route"]},
                partial_output=partial or None,
                status="FAILED_ALL_FREE_ROUTES",
            )
        raise AllFreeRoutesFailed("FAILED_ALL_FREE_ROUTES", failures=failures, web_search=requires_grounding(request))

    def _watch_provider_stream(self, provider_stream) -> Iterator[AIStreamEvent]:
        """Bound a blocking provider iterator while keeping the client connection alive."""

        events: queue.Queue = queue.Queue()
        stop_requested = threading.Event()
        context = copy_context()

        def consume() -> None:
            try:
                for event in provider_stream:
                    if stop_requested.is_set():
                        break
                    events.put(("event", event))
                events.put(("done", None))
            except BaseException as error:
                events.put(("error", error))

        worker = threading.Thread(
            target=lambda: context.run(consume),
            name="jupiter-provider-stream",
            daemon=True,
        )
        worker.start()
        started_at = time.monotonic()
        last_activity_at = started_at
        first_text_seen = False
        last_heartbeat_at = started_at
        while True:
            now = time.monotonic()
            overall_remaining = self.STREAM_OVERALL_SECONDS - (now - started_at)
            activity_limit = (
                self.STREAM_INACTIVITY_SECONDS if first_text_seen else self.STREAM_FIRST_TOKEN_SECONDS
            )
            activity_remaining = activity_limit - (now - last_activity_at)
            if overall_remaining <= 0 or activity_remaining <= 0:
                stop_requested.set()
                phase = "overall" if overall_remaining <= 0 else ("inactivity" if first_text_seen else "first_token")
                raise ProviderFailure(NormalizedFailure(
                    FailureClass.TIMEOUT,
                    status_code=504,
                    retryable=True,
                    reason_code=f"STREAM_{phase.upper()}_TIMEOUT",
                    safe_message="The provider stream stopped making progress.",
                ))
            wait_for = min(
                0.5,
                overall_remaining,
                activity_remaining,
                max(0.01, self.STREAM_HEARTBEAT_SECONDS - (now - last_heartbeat_at)),
            )
            try:
                kind, value = events.get(timeout=wait_for)
            except queue.Empty:
                now = time.monotonic()
                if now - last_heartbeat_at >= self.STREAM_HEARTBEAT_SECONDS:
                    last_heartbeat_at = now
                    yield AIStreamEvent(event_type="heartbeat")
                continue
            if kind == "done":
                return
            if kind == "error":
                raise value
            if value.text:
                last_activity_at = time.monotonic()
                first_text_seen = True
            yield value

    def _ensure_job(self, request: AIRequest) -> AIRequest:
        if self.store is None or request.job_id or request.no_persist or request.task_type == TaskType.CASUAL_CHAT:
            return request
        row = self.store.create_job(
            user_id=request.user_id,
            goal="Complete the authenticated user's current request",
            task_type=request.task_type.value,
            requirements={
                "required_modalities": sorted(request.required_modalities),
                "required_capabilities": sorted(request.required_capabilities),
                "request_id": request.request_id,
            },
            idempotency_key=f"request:{request.request_id}",
        )
        return replace(request, job_id=str(row["job_id"]))

    def _record(
        self, request, candidate, response, routing_metadata, fallback_number,
        fallback_reason, success, status_code, error_class,
        safe_message=None, reason_code=None,
    ) -> None:
        try:
            from services.api_health_center import api_health_center

            api_health_center().record_attempt(
                user_id=request.user_id,
                provider=candidate.model.provider,
                requested_model=candidate.model.model_id,
                actual_model=response.actual_model if response else None,
                task_type=request.task_type.value,
                latency_ms=response.latency_ms if response else None,
                success=success,
                status_code=status_code,
                error_class=error_class,
                fallback_number=fallback_number,
                reason_code=reason_code,
                safe_message=safe_message,
                details={
                    "fallback_reason": fallback_reason,
                    "fallback_stage": routing_metadata.get("fallback_stage"),
                    "same_model_retry": routing_metadata.get("same_model_retry"),
                },
            )
        except Exception:
            # Health telemetry must never prevent a user response or fallback.
            pass
        try:
            from services.api_health_center import api_health_center

            context = current_request_context()
            priority = str(getattr(request, "execution_priority", "interactive")).strip().casefold()
            generation_mode = context.generation_mode or ("text_chat" if priority == "interactive" else "background")
            scope = getattr(candidate.model.credential_scope, "value", str(candidate.model.credential_scope))
            # Labels only: managed Google ids ("provider-7-managed"), "user-google", or the provider scope name.
            credential_label = (
                "user-google" if scope == "USER_GOOGLE"
                else str(candidate.model.credential_owner_id or scope) if scope == "JUPITER_SHARED_GOOGLE"
                else scope
            )
            api_health_center().record_route_event(
                user_id=request.user_id,
                task_type=request.task_type.value,
                generation_mode=generation_mode,
                provider=candidate.model.provider,
                exact_model_id=(response.actual_model if response and response.actual_model else candidate.model.model_id),
                credential_label=credential_label,
                route_role="primary" if not fallback_number else "fallback",
                success=success,
                fallback_reason=fallback_reason or (context.live_fallback_reason or None),
                native_live=False,
                skip_reason=None if success else (reason_code or error_class),
            )
        except Exception:
            pass
        if success and response is not None:
            try:
                from services.monthly_token_usage import MonthlyTokenUsageStore

                priority = str(getattr(request, "execution_priority", "interactive")).strip().casefold()
                source_class = "interactive" if priority == "interactive" else "background"
                maintenance_priorities = {
                    "maintenance", "system", "owner_maintenance",
                    "catalog_refresh", "health_research",
                }
                accounting_user_id = (
                    "system:maintenance"
                    if priority in maintenance_priorities
                    else (request.user_id or "system:maintenance")
                )
                MonthlyTokenUsageStore().record(
                    event_id=(
                        f"{request.request_id}:{fallback_number}:"
                        f"{candidate.model.provider}:{response.actual_model}:"
                        f"{request.task_type.value}"
                    ),
                    user_id=accounting_user_id,
                    provider=candidate.model.provider,
                    model_id=response.actual_model,
                    input_tokens=response.input_usage,
                    output_tokens=response.output_usage,
                    source_class=source_class,
                )
            except Exception:
                # Accounting telemetry must never prevent a response/fallback.
                pass
        if self.store is None:
            return
        self.store.record_attempt(
            request_id=request.request_id,
            job_id=request.job_id,
            user_id=request.user_id,
            provider=candidate.model.provider,
            requested_model=candidate.model.model_id,
            actual_model=response.actual_model if response else None,
            credential_scope=candidate.model.credential_scope.value,
            task_type=request.task_type.value,
            latency_ms=response.latency_ms if response else None,
            success=success,
            status_code=status_code,
            error_class=error_class,
            input_tokens=response.input_usage if response else None,
            output_tokens=response.output_usage if response else None,
            fallback_number=fallback_number,
            fallback_reason=fallback_reason,
            routing_metadata=routing_metadata,
        )
        if success and response is not None:
            from services.integration_policy import FairUsePolicy

            FairUsePolicy(self.store).record(
                user_id=request.user_id,
                provider=candidate.model.provider,
                model_id=response.actual_model,
                credential_scope=candidate.model.credential_scope.value,
                input_tokens=response.input_usage or 0,
                output_tokens=response.output_usage or 0,
                audio_seconds=response.audio_usage or 0,
            )


def legacy_request(task, messages, *, stream: bool) -> AIRequest:
    context = current_request_context()
    legacy_name = getattr(task, "name", str(task))
    task_type = _LEGACY_TASK_MAP.get(legacy_name, TaskType.COMPLEX_CHAT)
    normalized_messages = []
    attachments = []
    attachment_mimes = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            normalized_messages.append(message)
            continue
        text_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(str(part.get("text") or ""))
            elif part.get("type") == "binary" and isinstance(part.get("data"), bytes):
                mime_type = str(part.get("mime_type") or "application/octet-stream")
                raw = part["data"]
                digest = hashlib.sha256(raw).hexdigest()
                attachments.append(Attachment(
                    file_id=digest,
                    mime_type=mime_type,
                    content=raw,
                    sha256=digest,
                ))
                attachment_mimes.add(mime_type)
        normalized_messages.append({**message, "content": "\n".join(text_parts)})
    orchestration = TaskOrchestrator.plan(normalized_messages, attachment_mimes)
    if legacy_name == "MAIN_CHAT":
        task_type = orchestration.primary_task
        if context.low_latency_conversation and task_type in {TaskType.CASUAL_CHAT, TaskType.COMPLEX_CHAT}:
            task_type = TaskType.CASUAL_CHAT
        modalities = orchestration.required_modalities
        capabilities = orchestration.required_capabilities
    else:
        modalities = frozenset({"text"})
        capabilities = {
            TaskType.STRUCTURED_EXTRACTION: frozenset({"structured_output"}),
            TaskType.MEMORY_EXTRACTION: frozenset({"structured_output"}),
            TaskType.MEMORY_CURATOR: frozenset(),
            TaskType.REVIEWING: frozenset(),
            TaskType.AUDIO_TRANSCRIPTION: frozenset({"audio_transcription"}),
            TaskType.TTS: frozenset({"tts"}),
        }.get(task_type, frozenset())
    estimated_characters = 0
    for message in normalized_messages:
        content = message.get("content")
        if isinstance(content, str):
            estimated_characters += len(content)
        elif isinstance(content, list):
            estimated_characters += sum(
                len(str(part.get("text") or ""))
                for part in content if isinstance(part, dict) and part.get("type") == "text"
            )
    privacy_class = PrivacyClass(context.privacy_class)
    if task_type in {
        TaskType.MEMORY_EXTRACTION,
        TaskType.MEMORY_CURATOR,
        TaskType.MEMORY_CANDIDATE_DETECTION,
        TaskType.MEMORY_TIER_CLASSIFICATION,
        TaskType.MEMORY_FACT_EXTRACTION,
        TaskType.MEMORY_RELATIONSHIP_EXTRACTION,
        TaskType.MEMORY_DUPLICATE_CONSOLIDATION,
        TaskType.MEMORY_CONTRADICTION,
        TaskType.MEMORY_RERANKING,
        TaskType.MEMORY_P0_JUDGE,
        TaskType.PERSONALITY_EXTRACTION,
        TaskType.EMAIL_READING,
    }:
        privacy_class = PrivacyClass.PRIVATE
    return AIRequest(
        request_id=uuid4().hex,
        user_id=context.user_id or "local-default",
        conversation_id=context.conversation_id,
        job_id=context.job_id,
        task_type=task_type,
        messages=normalized_messages,
        attachments=tuple(attachments),
        required_modalities=modalities,
        required_capabilities=capabilities,
        stream=stream,
        privacy_class=privacy_class,
        no_persist=context.no_persist,
        estimated_input_tokens=max(1, estimated_characters // 4),
    )
