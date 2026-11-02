"""Backend-only AI provider adapters.

No discovery or provider request runs at import/startup.  Every adapter is
called only by an explicit foreground request or durable discovery job.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import suppress
from email.utils import parsedate_to_datetime
from dataclasses import replace
from functools import lru_cache
from typing import Any, Iterator

import httpx

from core.provider_contracts import (
    AIProvider,
    AIRequest,
    AIResponse,
    AIStreamEvent,
    CredentialScope,
    FailureClass,
    NormalizedFailure,
    PrivacyClass,
    ProviderFailure,
    ProviderModel,
)
from core.request_context import resolve_request_credential
from core.security import redact


def _quota_failure_scope(detail: str, *, google: bool = False) -> str | None:
    """Scope is ownership, not the RPM/TPM/RPD time window."""
    if any(value in detail for value in ("account-wide", "account quota", "daily free requests", "neurons", "credit balance", "credits exhausted")):
        return "ACCOUNT"
    if any(value in detail for value in ("credential-wide", "api key rate limit", "api key quota")):
        return "CREDENTIAL"
    if any(value in detail for value in ("project-wide", "project disabled", "billing disabled")):
        return "PROJECT"
    if "model" in detail:
        return "PROJECT_MODEL" if google or "project" in detail else "MODEL"
    if any(value in detail for value in ("project", "consumer quota", "quota project")):
        return "PROJECT"
    # A bare Google 429 retains the requested project/model bucket. Never
    # infer whole-credential exhaustion from a time-window label such as TPM.
    return "PROJECT_MODEL" if google else None


def _failure_retry_after(error: Exception, detail: str = "") -> int | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value:
        try:
            return max(0, int(float(value)))
        except (ValueError, TypeError, OverflowError):
            try:
                return max(0, int(parsedate_to_datetime(str(value)).timestamp() - time.time()))
            except (ValueError, TypeError, OverflowError):
                pass
    # Gemini SDK errors expose google.rpc.RetryInfo as retryDelay, or a
    # human-readable retry duration. Inspect locally; never log error payloads.
    match = re.search(r"retry(?:delay|_delay)['\"\s:]+([\d.]+)s|retry in\s+([\d.]+)s", detail)
    if match:
        try:
            return max(1, int(float(next(value for value in match.groups() if value))) + 1)
        except (ValueError, OverflowError):
            pass
    return None


_SCOPE_ENV = {
    CredentialScope.JUPITER_SHARED_GOOGLE: "JUPITER_GOOGLE_API_KEY",
    CredentialScope.JUPITER_SHARED_GROQ: "GROQ_API_KEY",
    CredentialScope.JUPITER_SHARED_NVIDIA: "NVIDIA_API_KEY",
    CredentialScope.JUPITER_SHARED_CLOUDFLARE: "CLOUDFLARE_API_TOKEN",
    CredentialScope.JUPITER_SHARED_OPENCODE: "OPENCODE_ZEN_API_KEY",
    CredentialScope.JUPITER_SHARED_OPENROUTER: "OPENROUTER_API_KEY",
    CredentialScope.JUPITER_SHARED_BYTEZ: "BYTEZ_API_KEY",
}


@lru_cache(maxsize=12)
def _shared_http_client(base_url: str, timeout_seconds: float) -> httpx.Client:
    """Thread-safe persistent connection pool keyed by provider endpoint."""

    return httpx.Client(
        base_url=base_url.rstrip("/"),
        trust_env=False,
        timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=12, keepalive_expiry=45.0),
    )


_GOOGLE_CLIENT_LIMIT = 16
_GOOGLE_CLIENTS: "OrderedDict[tuple[int, str], Any]" = OrderedDict()
_GOOGLE_CLIENTS_LOCK = threading.Lock()


def _google_client(api_key: str):
    """One reusable google-genai client per credential. Building one costs ~0.9 s (measured 30-Sep: a single chat turn
    that walked 8 credentials spent 8.2 s in 9 constructions). Keyed by a digest, never the key; LRU-bounded."""
    from google import genai

    key = (id(genai.Client), hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest())
    with _GOOGLE_CLIENTS_LOCK:
        cached = _GOOGLE_CLIENTS.get(key)
        if cached is not None:
            _GOOGLE_CLIENTS.move_to_end(key)
            return cached
    client = genai.Client(api_key=api_key)
    with _GOOGLE_CLIENTS_LOCK:
        cached = _GOOGLE_CLIENTS.get(key)
        if cached is not None:
            with suppress(Exception):
                client.close()
            return cached
        _GOOGLE_CLIENTS[key] = client
        while len(_GOOGLE_CLIENTS) > _GOOGLE_CLIENT_LIMIT:
            _, evicted = _GOOGLE_CLIENTS.popitem(last=False)
            with suppress(Exception):
                evicted.close()
    return client


def _credential(scope: CredentialScope, model: ProviderModel | None = None) -> str:
    request_secret = resolve_request_credential(scope.value)
    secret = request_secret
    if not secret and scope == CredentialScope.JUPITER_SHARED_GOOGLE:
        from config.credential_policy import managed_google_policies, managed_google_secret

        if model is not None and model.credential_owner_id not in {"", "*"}:
            capability = next(iter(model.capabilities), "")
            secret = managed_google_secret(
                model.credential_owner_id,
                model.model_id,
                capability,
            )
        else:
            policy = next(iter(managed_google_policies("")), None)
            secret = policy.secret() if policy else None
    if not secret and scope != CredentialScope.JUPITER_SHARED_GOOGLE:
        # Provider -> accounts -> credentials (config/provider_catalog.json, env NAMES only). A pinned credential is
        # honoured exactly; unpinned models use the first configured credential by priority.
        from config.provider_catalog import credential_secret

        owner = model.credential_owner_id if model is not None else ""
        secret = credential_secret(scope.value, owner) or ""
        if not secret and owner in {"", "*"}:
            secret = os.getenv(_SCOPE_ENV.get(scope, ""), "").strip()   # legacy single-key configuration
    if not secret:
        raise ProviderFailure(
            NormalizedFailure(
                FailureClass.AUTHENTICATION,
                auth_error=True,
                safe_message=f"Credential scope {scope.value} is not configured.",
            )
        )
    return secret


def _observe_rate_limit_headers(provider: str, response: httpx.Response, model: ProviderModel) -> None:
    """Forward only supported provider quota metadata to the local governor."""

    if str(provider or "").casefold() != "groq":
        return
    try:
        from services.quota_governor import quota_governor
        quota_governor().observe_rate_limit_headers(model, response.headers)
    except Exception:
        # Quota telemetry must never turn a valid foreground response into a
        # provider failure. Configured limits remain the conservative fallback.
        return


def _zero_price(value: Any) -> bool:
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _free_flags(metadata: dict[str, Any], model_id: str) -> tuple[bool, bool, bool]:
    """UNKNOWN is never treated as free; explicit pricing/designation is needed."""

    pricing = metadata.get("pricing") if isinstance(metadata.get("pricing"), dict) else {}
    input_price = pricing.get("prompt", pricing.get("input"))
    output_price = pricing.get("completion", pricing.get("output"))
    pricing_known = bool(pricing) and input_price is not None and output_price is not None
    priced_free = (
        pricing_known
        and _zero_price(input_price)
        and _zero_price(output_price)
    )
    explicit_free = bool(metadata.get("free") is True or model_id.endswith(":free"))
    # Explicit current prices override a name/designation if the two disagree.
    verified = priced_free if pricing_known else explicit_free
    return verified, verified, verified


def _usage(payload: Any) -> tuple[int | None, int | None]:
    source = payload if isinstance(payload, dict) else {}
    prompt = source.get("prompt_tokens", source.get("input_tokens"))
    completion = source.get("completion_tokens", source.get("output_tokens"))
    return (
        int(prompt) if isinstance(prompt, (int, float)) else None,
        int(completion) if isinstance(completion, (int, float)) else None,
    )


def _content_text(value: Any) -> str:
    """Extract user-visible text from known OpenAI-compatible content shapes."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_content_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if not isinstance(value, dict):
        return ""
    item_type = str(value.get("type") or "").casefold()
    if item_type and item_type not in {
        "text", "output_text", "message", "content", "summary_text",
        "reasoning", "reasoning_text",
    }:
        return ""
    for key in ("text", "output_text", "content", "value"):
        text = _content_text(value.get(key))
        if text:
            return text
    return ""


def _stream_content_text(value: Any) -> str:
    """Extract a streamed delta without destroying token-boundary whitespace.

    Final provider messages are normalized by ``_content_text``.  Streamed
    deltas are different: a leading space, trailing newline, or indentation is
    part of the protocol and must survive verbatim so adjacent chunks assemble
    into the same Markdown/text as the provider's final response.
    """

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_stream_content_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    item_type = str(value.get("type") or "").casefold()
    if item_type and item_type not in {
        "text", "output_text", "message", "content", "summary_text",
        "reasoning", "reasoning_text",
    }:
        return ""
    for key in ("text", "output_text", "content", "value"):
        if key not in value:
            continue
        text = _stream_content_text(value.get(key))
        if text != "":
            return text
    return ""


def _reasoning_text(message: dict[str, Any]) -> str:
    """Last-resort textual output used only when no final-content field exists."""

    for key in ("reasoning", "reasoning_content"):
        text = _content_text(message.get(key))
        if text:
            return text
    details = message.get("reasoning_details")
    if isinstance(details, list):
        parts: list[str] = []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            text = _content_text(detail.get("text")) or _content_text(detail.get("summary"))
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts).strip()
    return ""


def _response_text(payload: Any, *, include_reasoning_fallback: bool = True) -> str:
    """Return final text from Chat Completions or Responses-compatible JSON."""

    if not isinstance(payload, dict):
        return ""
    direct = _content_text(payload.get("output_text"))
    if direct:
        return direct
    reasoning_candidates: list[str] = []
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            final = _content_text(message.get("content")) or _content_text(choice.get("text"))
            if final:
                return final
            reasoning = _reasoning_text(message)
            if reasoning:
                reasoning_candidates.append(reasoning)
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").casefold() == "message":
                final = _content_text(item.get("content"))
                if final:
                    return final
            elif str(item.get("type") or "").casefold() in {"output_text", "text"}:
                final = _content_text(item)
                if final:
                    return final
            elif str(item.get("type") or "").casefold() == "reasoning":
                reasoning = _content_text(item.get("summary")) or _content_text(item.get("text"))
                if reasoning:
                    reasoning_candidates.append(reasoning)
    if include_reasoning_fallback and reasoning_candidates:
        return "\n".join(reasoning_candidates).strip()
    return ""


def _stream_text(payload: Any) -> tuple[str, str]:
    """Return (final_text, reasoning_text) for chat and Responses SSE events."""

    if not isinstance(payload, dict):
        return "", ""
    event_type = str(payload.get("type") or "").casefold()
    if event_type in {"response.output_text.delta", "response.content_part.delta"}:
        return _stream_content_text(payload.get("delta")), ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        final = _stream_content_text(delta.get("content"))
        if final == "":
            final = _stream_content_text(delta.get("output_text"))
        reasoning = _reasoning_text(delta)
        return final, reasoning
    return _stream_content_text(payload.get("output_text")), ""


def _string_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return []


def _capabilities(metadata: dict[str, Any], input_modalities: list[str], output_modalities: list[str]) -> set[str]:
    """Translate explicit catalog metadata; never guess support from a model name."""

    raw_capabilities = metadata.get("capabilities")
    values = set(_string_values(raw_capabilities)) if not isinstance(raw_capabilities, dict) else {
        str(key) for key, enabled in raw_capabilities.items() if enabled is True
    }
    supported = set(_string_values(metadata.get("supported_parameters")))
    if "tools" in supported or "tool_choice" in supported:
        values.add("tools")
    if {"response_format", "structured_output", "structured_outputs"} & supported:
        values.add("structured_output")
    if "image" in input_modalities:
        values.add("vision")
    if "image" in output_modalities:
        values.add("image_generation")
    if "embedding" in output_modalities or "embeddings" in output_modalities:
        values.add("embeddings")
    return values


def _catalog_lifecycle(metadata: dict[str, Any]) -> tuple[bool, str | None]:
    """Honor explicit provider lifecycle metadata without guessing from an ID."""

    lifecycle = str(
        metadata.get("lifecycle_status")
        or metadata.get("status")
        or metadata.get("state")
        or ""
    ).strip().casefold().replace("-", "_")
    if metadata.get("deprecated") is True or lifecycle in {
        "deprecated", "decommissioned", "retired", "sunset", "shutting_down"
    }:
        return False, "DEPRECATED"
    if metadata.get("active") is False or metadata.get("available") is False or lifecycle in {
        "removed", "deleted", "disabled", "unavailable"
    }:
        return False, "REMOVED"
    return True, None


def _messages(request: AIRequest, *, allow_binary: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    system_sections = [
        request.system_context,
        request.personality_brief,
        request.memory_capsule,
    ]
    system_content = "\n\n".join(section for section in system_sections if section.strip())
    if system_content:
        result.append({"role": "system", "content": system_content})
    result.extend(request.messages)
    if request.attachments:
        if not allow_binary:
            raise ProviderFailure(
                NormalizedFailure(
                    FailureClass.UNSUPPORTED,
                    unsupported=True,
                    safe_message="Selected route does not support request attachments.",
                )
            )
        parts: list[dict[str, Any]] = []
        for attachment in request.attachments:
            if attachment.text is not None:
                parts.append({"type": "text", "text": attachment.text})
            elif attachment.content is not None and attachment.mime_type.startswith("image/"):
                encoded = base64.b64encode(attachment.content).decode("ascii")
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{attachment.mime_type};base64,{encoded}"},
                })
            else:
                raise ProviderFailure(
                    NormalizedFailure(
                        FailureClass.UNSUPPORTED,
                        unsupported=True,
                        safe_message="Selected route cannot encode this attachment type.",
                    )
                )
        if parts:
            result.append({"role": "user", "content": parts})
    return result


class OpenAICompatibleProvider(AIProvider):
    """Common implementation for providers exposing chat-completions semantics."""

    name = "openai_compatible"
    credential_scope = CredentialScope.JUPITER_SHARED_OPENROUTER
    base_url = ""
    timeout_seconds = 45.0

    def _base_url(self) -> str:
        return self.base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {_credential(self.credential_scope)}"}

    def _catalog_url(self) -> str:
        return f"{self._base_url()}/models"

    def _chat_url(self) -> str:
        return f"{self._base_url()}/chat/completions"

    def _catalog_rows(self, payload: Any) -> list[dict[str, Any]]:
        rows = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _catalog_model(self, row: dict[str, Any]) -> ProviderModel | None:
        model_id = str(row.get("id") or row.get("name") or "").strip()
        if not model_id:
            return None
        eligible, verified, plan = _free_flags(row, model_id)
        pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        pricing_known = bool(pricing) and (
            pricing.get("prompt", pricing.get("input")) is not None
            and pricing.get("completion", pricing.get("output")) is not None
        )
        free_status = "free" if verified else "paid" if pricing_known else "unknown"
        architecture = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
        modalities_explicit = bool(
            architecture.get("input_modalities")
            or architecture.get("output_modalities")
            or row.get("input_modalities")
            or row.get("output_modalities")
        )
        input_modalities = _string_values(
            architecture.get("input_modalities") or row.get("input_modalities")
        ) or ["text"]
        output_modalities = _string_values(
            architecture.get("output_modalities") or row.get("output_modalities")
        ) or ["text"]
        capabilities = _capabilities(row, input_modalities, output_modalities)
        available, lifecycle_status = _catalog_lifecycle(row)
        return ProviderModel(
            provider=self.name,
            model_id=model_id,
            display_name=str(row.get("name") or model_id),
            credential_scope=self.credential_scope,
            available=available,
            free_eligible=eligible,
            free_verified=verified,
            free_plan_available=plan,
            input_modalities=frozenset(input_modalities),
            output_modalities=frozenset(output_modalities),
            capabilities=frozenset(capabilities),
            context_window=row.get("context_length") if isinstance(row.get("context_length"), int) else None,
            max_output=row.get("max_output") if isinstance(row.get("max_output"), int) else None,
            status=lifecycle_status or ("ACTIVE" if verified else "CANDIDATE"),
            metadata={
                **row,
                "catalog_free_status": free_status,
                "catalog_modalities_explicit": modalities_explicit,
            },
        )

    def list_models(self) -> list[ProviderModel]:
        try:
            client = _shared_http_client(self._base_url(), self.timeout_seconds)
            response = client.get(self._catalog_url(), headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure(self.normalize_error(error)) from None
        models: list[ProviderModel] = []
        for row in self._catalog_rows(payload):
            model = self._catalog_model(row)
            if model is not None:
                models.append(model)
        return models

    def _payload(self, request: AIRequest, model: ProviderModel, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model.model_id,
            "messages": _messages(request, allow_binary="image" in model.input_modalities),
            "stream": stream,
        }
        if request.max_output:
            payload["max_tokens"] = request.max_output
        if request.tools and "tools" in model.capabilities:
            payload["tools"] = list(request.tools)
        if request.structured_output_schema and "structured_output" in model.capabilities:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": request.structured_output_schema,
            }
        return payload

    def generate(self, request: AIRequest, model: ProviderModel) -> AIResponse:
        started = time.monotonic()
        try:
            client = _shared_http_client(self._base_url(), self.timeout_seconds)
            response = client.post(
                self._chat_url(),
                headers={**self._headers(), "Content-Type": "application/json"},
                json=self._payload(request, model, False),
            )
            _observe_rate_limit_headers(self.name, response, model)
            response.raise_for_status()
            payload = response.json()
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = _response_text(payload)
            if not content:
                raise ProviderFailure(
                    NormalizedFailure(
                        FailureClass.UNKNOWN,
                        reason_code="EMPTY_RESPONSE",
                        safe_message=f"{self.name} returned no usable response text.",
                    )
                )
            actual_model = str(payload.get("model") or model.model_id)
            input_usage, output_usage = _usage(payload.get("usage") or {})
            from services.quota_governor import QuotaGovernor
            return AIResponse(
                content=content,
                provider=self.name,
                requested_model=model.model_id,
                actual_model=actual_model,
                finish_reason=choice.get("finish_reason"),
                input_usage=input_usage,
                output_usage=output_usage,
                latency_ms=int((time.monotonic() - started) * 1000),
                tool_calls=tuple(message.get("tool_calls") or ()),
                provider_request_id=response.headers.get("x-request-id") or payload.get("id"),
                shared_quota_units=QuotaGovernor.actual_shared_units(payload) if self.name == "cloudflare" else None,
            )
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure(self.normalize_error(error)) from None

    def stream(self, request: AIRequest, model: ProviderModel) -> Iterator[AIStreamEvent]:
        try:
            client = _shared_http_client(self._base_url(), self.timeout_seconds)
            with client.stream(
                "POST",
                self._chat_url(),
                headers={**self._headers(), "Content-Type": "application/json"},
                json=self._payload(request, model, True),
            ) as response:
                _observe_rate_limit_headers(self.name, response, model)
                # httpx does not buffer a streamed error response. Read its
                # small diagnostic body before normalization so one rejected
                # model can be classified and the free fallback chain can
                # continue instead of terminating with ResponseNotRead.
                if response.is_error:
                    response.read()
                response.raise_for_status()
                emitted_final = False
                reasoning_parts: list[str] = []
                last_actual_model = model.model_id
                last_finish_reason = None
                last_usage = None
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    payload = json.loads(raw)
                    choice = (payload.get("choices") or [{}])[0]
                    final_text, reasoning_text = _stream_text(payload)
                    if reasoning_text:
                        reasoning_parts.append(reasoning_text)
                    last_actual_model = str(payload.get("model") or last_actual_model)
                    last_finish_reason = choice.get("finish_reason") or last_finish_reason
                    last_usage = payload.get("usage") or last_usage
                    if not final_text:
                        continue
                    emitted_final = True
                    yield AIStreamEvent(
                        text=final_text,
                        provider=self.name,
                        requested_model=model.model_id,
                        actual_model=last_actual_model,
                        finish_reason=choice.get("finish_reason"),
                        usage=payload.get("usage"),
                    )
                if not emitted_final and reasoning_parts:
                    fallback = "\n".join(part for part in reasoning_parts if part).strip()
                    if fallback:
                        emitted_final = True
                        yield AIStreamEvent(
                            text=fallback,
                            provider=self.name,
                            requested_model=model.model_id,
                            actual_model=last_actual_model,
                            finish_reason=last_finish_reason,
                            usage=last_usage,
                        )
                if not emitted_final:
                    raise ProviderFailure(
                        NormalizedFailure(
                            FailureClass.UNKNOWN,
                            reason_code="EMPTY_RESPONSE",
                            safe_message=f"{self.name} returned no usable streamed text.",
                        )
                    )
                # Usage often arrives in a final empty delta; don't discard it.
                yield AIStreamEvent(event_type="usage", provider=self.name,
                                    requested_model=model.model_id, actual_model=last_actual_model,
                                    finish_reason=last_finish_reason, usage=last_usage)
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure(self.normalize_error(error)) from None

    def normalize_error(self, error: Exception) -> NormalizedFailure:
        response = getattr(error, "response", None)
        status = getattr(error, "status_code", None)
        detail = str(error).casefold()
        if response is not None:
            status = getattr(response, "status_code", status)
            try:
                detail = str(response.text)[:4000].casefold()
            except httpx.ResponseNotRead:
                pass
        if not isinstance(status, int):
            status = None
        retry_after = _failure_retry_after(error, detail)
        if isinstance(error, (httpx.TimeoutException, TimeoutError)):
            return NormalizedFailure(
                FailureClass.TIMEOUT, retryable=True, safe_message="Provider timed out."
            )
        mapping = {
            400: (FailureClass.BAD_REQUEST, False),
            401: (FailureClass.AUTHENTICATION, False),
            403: (FailureClass.PERMISSION, False),
            404: (FailureClass.MODEL_REMOVED, False),
            410: (FailureClass.MODEL_REMOVED, False),
            429: (FailureClass.RATE_LIMIT, True),
            500: (FailureClass.SERVER, True),
            502: (FailureClass.SERVER, True),
            503: (FailureClass.SERVER, True),
        }
        failure_class, retryable = mapping.get(status, (FailureClass.UNKNOWN, False))
        if status is not None and 500 <= status <= 599:
            failure_class, retryable = FailureClass.SERVER, True
        reason_code = None
        quota_scope = None
        quota_exhausted = False
        if status == 429:
            if any(marker in detail for marker in ("resource_exhausted", "quota exceeded", "quota exhausted")):
                failure_class = FailureClass.QUOTA_EXHAUSTED
                quota_exhausted = True
                reason_code = "QUOTA_EXHAUSTED"
            elif any(marker in detail for marker in ("capacity", "overloaded", "temporarily unavailable")):
                failure_class = FailureClass.SERVER
                reason_code = "PROVIDER_CAPACITY"
            else:
                failure_class = FailureClass.RATE_LIMIT
                reason_code = "RATE_LIMITED"
            quota_scope = _quota_failure_scope(detail)
        if any(marker in detail for marker in ("terms acceptance", "requires terms", "accept the terms")):
            failure_class, retryable = FailureClass.PERMISSION, False
            reason_code = "TERMS_REQUIRED"
        elif any(marker in detail for marker in ("license required", "licensing required")):
            failure_class, retryable = FailureClass.PERMISSION, False
            reason_code = "LICENSING_REQUIRED"
        elif status == 403:
            if any(marker in detail for marker in ("model agreement", "submit the permission form")):
                reason_code = "TERMS_REQUIRED"
            elif any(marker in detail for marker in ("api has not been used", "api disabled", "not enabled")):
                reason_code = "API_DISABLED"
            elif any(marker in detail for marker in ("paid plan", "billing", "purchase", "credits required")):
                reason_code = "MODEL_NOT_FREE"
            else:
                reason_code = "PERMISSION_DENIED"
        return NormalizedFailure(
            failure_class,
            status_code=status,
            retryable=retryable,
            cooldown_seconds=retry_after,
            quota_exhausted=quota_exhausted,
            auth_error=status == 401,
            unsupported=status == 400 and any(
                marker in detail for marker in ("unsupported", "not supported", "does not support")
            ),
            reason_code=reason_code,
            quota_scope=quota_scope,
            safe_message=redact(f"{self.name} request failed ({status or 'network'}).", 240),
        )


class GroqProvider(OpenAICompatibleProvider):
    name = "groq"
    credential_scope = CredentialScope.JUPITER_SHARED_GROQ
    base_url = "https://api.groq.com/openai/v1"
    timeout_seconds = 35.0


class NvidiaProvider(OpenAICompatibleProvider):
    name = "nvidia"
    credential_scope = CredentialScope.JUPITER_SHARED_NVIDIA
    base_url = "https://integrate.api.nvidia.com/v1"
    timeout_seconds = 70.0


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"
    credential_scope = CredentialScope.JUPITER_SHARED_OPENROUTER
    base_url = "https://openrouter.ai/api/v1"
    timeout_seconds = 5.0

    def _catalog_url(self) -> str:
        # Authenticated catalog respects this account's provider/model preferences.
        return f"{self._base_url()}/models/user"

    def _headers(self) -> dict[str, str]:
        return {
            **super()._headers(),
            "HTTP-Referer": os.getenv("JUPITER_PUBLIC_URL", "https://jupiter.example.invalid"),
            "X-OpenRouter-Title": "Jupiter",
        }


class OpenCodeZenProvider(OpenAICompatibleProvider):
    name = "opencode"
    credential_scope = CredentialScope.JUPITER_SHARED_OPENCODE
    base_url = "https://opencode.ai/zen/v1"
    timeout_seconds = 55.0

    def _base_url(self) -> str:
        return os.getenv("OPENCODE_ZEN_BASE_URL", self.base_url).strip().rstrip("/")

    def _catalog_model(self, row: dict[str, Any]) -> ProviderModel | None:
        model = super()._catalog_model(row)
        if model is None:
            return None
        pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        if not pricing and isinstance(row.get("cost"), dict):
            pricing = row["cost"]
        priced_free = bool(pricing) and _zero_price(
            pricing.get("prompt", pricing.get("input"))
        ) and _zero_price(pricing.get("completion", pricing.get("output")))
        availability = str(row.get("status") or "active").casefold()
        available = row.get("available") is not False and availability not in {
            "disabled", "deprecated", "removed", "unavailable",
        }
        return replace(
            model,
            available=available,
            free_eligible=available and priced_free,
            free_verified=available and priced_free,
            free_plan_available=available and priced_free,
            status="ACTIVE" if available and priced_free else "CANDIDATE",
            metadata={
                **row,
                "pricing": pricing,
                "catalog_free_status": "free" if priced_free else "paid" if pricing else "unknown",
            },
        )


class BytezProvider(OpenAICompatibleProvider):
    """Task-aware Bytez adapter kept out of ordinary chat routing.

    Bytez exposes one task-specific API rather than an OpenAI chat endpoint.
    Catalog membership (including a ``*-free`` meter) does not prove usable
    remaining account credit, so every discovered model remains a specialist
    candidate until a later, explicitly authorised health check.
    """

    name = "bytez"
    credential_scope = CredentialScope.JUPITER_SHARED_BYTEZ
    base_url = "https://api.bytez.com"
    timeout_seconds = 75.0

    _KNOWN_CLOSED_PREFIXES = (
        "anthropic/", "openai/", "google/", "xai/", "cohere/", "amazon/", "microsoft/",
    )
    _INPUT_BY_TASK = {
        "automatic-speech-recognition": {"audio"},
        "audio-classification": {"audio"},
        "audio-text-to-text": {"audio"},
        "audio-to-audio": {"audio"},
        "depth-estimation": {"image"},
        "document-question-answering": {"document"},
        "image-classification": {"image"},
        "image-segmentation": {"image"},
        "image-text-to-text": {"image", "text"},
        "image-to-text": {"image"},
        "object-detection": {"image"},
        "visual-question-answering": {"image", "text"},
        "video-classification": {"video"},
        "video-text-to-text": {"video", "text"},
        "zero-shot-image-classification": {"image"},
        "zero-shot-object-detection": {"image", "text"},
    }
    _OUTPUT_BY_TASK = {
        "text-to-audio": {"audio"},
        "text-to-image": {"image"},
        "text-to-video": {"video"},
        "text-to-speech": {"audio"},
    }

    def _headers(self) -> dict[str, str]:
        # Bytez deliberately uses the raw key, not a Bearer token.
        return {"Authorization": _credential(self.credential_scope)}

    def _catalog_url(self) -> str:
        return f"{self._base_url()}/models/v2/list/models"

    def _catalog_rows(self, payload: Any) -> list[dict[str, Any]]:
        rows = payload.get("output", []) if isinstance(payload, dict) else []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    @classmethod
    def _task_name(cls, row: dict[str, Any]) -> str:
        return str(row.get("task") or "").strip().casefold().replace("_", "-").replace(" ", "-")

    @classmethod
    def _closed_or_paid(cls, row: dict[str, Any], model_id: str, meter: str) -> bool:
        metadata_values = " ".join(
            str(row.get(key) or "") for key in ("availability", "access", "plan", "provider", "license", "status")
        ).casefold()
        explicit_closed = any(row.get(key) is True for key in ("closed", "isClosed", "paidOnly", "requiresPayment"))
        known_closed = model_id.casefold().startswith(cls._KNOWN_CLOSED_PREFIXES)
        paid_meter = any(marker in meter for marker in ("-paid", "-pro", "enterprise", "closed"))
        return explicit_closed or known_closed or paid_meter or any(
            marker in metadata_values for marker in ("paid-only", "paid only", "closed-source", "closed source")
        )

    def _catalog_model(self, row: dict[str, Any]) -> ProviderModel | None:
        model_id = str(row.get("modelId") or row.get("model_id") or "").strip()
        if not model_id:
            return None
        task = self._task_name(row)
        meter = str(row.get("meter") or "").strip()
        available, lifecycle_status = _catalog_lifecycle(row)
        closed_or_paid = self._closed_or_paid(row, model_id, meter.casefold())
        free_meter = meter.casefold().endswith("-free")
        free_candidate = available and free_meter and not closed_or_paid
        input_modalities = self._INPUT_BY_TASK.get(task, {"text"})
        output_modalities = self._OUTPUT_BY_TASK.get(task, {"text"})
        return ProviderModel(
            provider=self.name,
            model_id=model_id,
            display_name=model_id,
            credential_scope=self.credential_scope,
            available=available,
            free_eligible=free_candidate,
            # A listed free meter is account eligibility metadata, not a
            # health or remaining-credit guarantee.
            free_verified=False,
            free_plan_available=free_candidate,
            routing_allowed=False,
            input_modalities=frozenset(input_modalities),
            output_modalities=frozenset(output_modalities),
            capabilities=frozenset({task} if task else set()),
            status=lifecycle_status or "SPECIALIST_CATALOG_CANDIDATE",
            metadata={
                "bytez_task": task,
                "params": row.get("params"),
                "meter": meter or None,
                "meter_price": row.get("meterPrice", row.get("meter_price")),
                "ram_required": row.get("ramRequired", row.get("ram_required")),
                "catalog_free_status": (
                    "free-account-meter-awaiting-health" if free_candidate
                    else "paid-or-closed" if closed_or_paid
                    else "not-free-meter"
                ),
                "catalog_modalities_explicit": True,
                "specialist_only": True,
                "privacy_allowed": ["PUBLIC"],
                "free_verified": False,
                "closed_or_paid": closed_or_paid,
            },
        )

    def _run_url(self, model: ProviderModel) -> str:
        return f"{self._base_url()}/models/v2/{model.model_id}"

    @staticmethod
    def _last_user_text(request: AIRequest) -> str:
        for message in reversed(request.messages):
            if str(message.get("role") or "").casefold() == "user":
                content = message.get("content")
                if isinstance(content, str):
                    return content
        return ""

    def _payload(self, request: AIRequest, model: ProviderModel, *, stream: bool) -> dict[str, Any]:
        """Build only documented, task-shaped Bytez input fields."""

        task = str(model.metadata.get("bytez_task") or "").casefold()
        user_text = self._last_user_text(request)
        payload: dict[str, Any] = {"stream": stream}
        if task in {"chat", "conversational", "text-generation"}:
            payload["messages"] = _messages(request, allow_binary=False)
        elif task in {"question-answering", "document-question-answering"}:
            payload["question"] = user_text
            text_attachment = next((item.text for item in request.attachments if item.text), None)
            payload["context"] = text_attachment or request.memory_capsule
        else:
            payload["text"] = user_text
        if request.max_output:
            payload["params"] = {"max_new_tokens": request.max_output}
        if request.attachments:
            attachment = request.attachments[0]
            if attachment.content is not None:
                payload["base64"] = base64.b64encode(attachment.content).decode("ascii")
            elif attachment.text is not None and task not in {"question-answering", "document-question-answering"}:
                payload["text"] = attachment.text
            else:
                raise ProviderFailure(NormalizedFailure(
                    FailureClass.UNSUPPORTED,
                    unsupported=True,
                    safe_message="The Bytez task requires a supported user-supplied attachment.",
                ))
        return payload

    @staticmethod
    def _output_text(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        output = payload.get("output")
        if isinstance(output, str):
            return output.strip()
        if output is not None:
            # Specialist structured output is retained without trying to
            # reinterpret detection/segmentation JSON as ordinary chat text.
            return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
        return ""

    def generate(self, request: AIRequest, model: ProviderModel) -> AIResponse:
        started = time.monotonic()
        try:
            if request.privacy_class != PrivacyClass.PUBLIC:
                raise ProviderFailure(NormalizedFailure(
                    FailureClass.PERMISSION,
                    reason_code="BYTEZ_PUBLIC_ONLY",
                    safe_message="Bytez is approved only for public, user-supplied specialist input.",
                ))
            client = _shared_http_client(self._base_url(), self.timeout_seconds)
            response = client.post(
                self._run_url(model),
                headers={**self._headers(), "Content-Type": "application/json"},
                json=self._payload(request, model, stream=False),
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error"):
                raise ProviderFailure(NormalizedFailure(
                    FailureClass.UNKNOWN,
                    reason_code="BYTEZ_MODEL_ERROR",
                    safe_message="Bytez could not complete this specialist request.",
                ))
            content = self._output_text(payload)
            if not content:
                raise ProviderFailure(NormalizedFailure(
                    FailureClass.UNKNOWN,
                    reason_code="EMPTY_RESPONSE",
                    safe_message="Bytez returned no usable specialist output.",
                ))
            return AIResponse(
                content=content,
                provider=self.name,
                requested_model=model.model_id,
                actual_model=model.model_id,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure(self.normalize_error(error)) from None

    def stream(self, request: AIRequest, model: ProviderModel) -> Iterator[AIStreamEvent]:
        # Bytez supports task-specific streaming formats.  Until a selected
        # specialist has passed the later per-task contract check, expose a
        # single final event rather than claiming incompatible SSE semantics.
        response = self.generate(request, model)
        yield AIStreamEvent(
            text=response.content,
            provider=self.name,
            requested_model=model.model_id,
            actual_model=model.model_id,
            finish_reason="stop",
        )


class CloudflareProvider(OpenAICompatibleProvider):
    name = "cloudflare"
    credential_scope = CredentialScope.JUPITER_SHARED_CLOUDFLARE
    timeout_seconds = 55.0

    def _base_url(self) -> str:
        account_id = (
            resolve_request_credential('USER_CLOUDFLARE_ACCOUNT_ID') or ''
            if self.credential_scope == CredentialScope.USER_CLOUDFLARE
            else os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        )
        if not account_id:
            raise ProviderFailure(
                NormalizedFailure(
                    FailureClass.AUTHENTICATION,
                    auth_error=True,
                    safe_message="Cloudflare account scope is not configured.",
                )
            )
        return f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"

    def _catalog_url(self) -> str:
        account_id = (
            resolve_request_credential('USER_CLOUDFLARE_ACCOUNT_ID') or ''
            if self.credential_scope == CredentialScope.USER_CLOUDFLARE
            else os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        )
        if not account_id:
            return self._base_url()
        return (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            "/ai/models/search?per_page=1000"
        )

    def _catalog_rows(self, payload: Any) -> list[dict[str, Any]]:
        result = payload.get("result", []) if isinstance(payload, dict) else []
        if isinstance(result, dict):
            result = result.get("models", result.get("data", []))
        return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []

    def _catalog_model(self, row: dict[str, Any]) -> ProviderModel | None:
        # Workers AI search rows use an opaque catalog UUID in `id`; `name` is
        # the exact callable @cf/... identifier accepted by inference APIs.
        model_id = str(row.get("name") or row.get("id") or "").strip()
        if not model_id:
            return None
        task_value = row.get("task")
        if isinstance(task_value, dict):
            task_value = task_value.get("name") or task_value.get("id")
        task = str(task_value or "").casefold().replace("_", "-")
        input_modalities: set[str] = {"text"}
        output_modalities: set[str] = {"text"}
        capabilities: set[str] = set()
        if task in {"text-to-image", "text to image"}:
            output_modalities = {"image"}
            capabilities.add("image_generation")
        elif task in {"automatic-speech-recognition", "automatic speech recognition", "speech-recognition"}:
            input_modalities = {"audio"}
            capabilities.add("audio_transcription")
        elif task in {"text-embeddings", "text embeddings", "embedding"}:
            output_modalities = {"embedding"}
            capabilities.add("embeddings")
        elif task in {"text-to-speech", "text to speech"}:
            output_modalities = {"audio"}
            capabilities.add("tts")
        elif task in {"image-classification", "image classification", "object-detection", "object detection"}:
            input_modalities = {"image"}
            capabilities.add("vision")
        available, lifecycle_status = _catalog_lifecycle(row)
        # Workers AI pricing is neuron-based and the free allocation is shared
        # at account level. Catalog membership alone cannot prove remaining free access.
        return ProviderModel(
            provider=self.name,
            model_id=model_id,
            display_name=str(row.get("display_name") or row.get("name") or model_id),
            credential_scope=self.credential_scope,
            available=available,
            free_eligible=False,
            free_verified=False,
            free_plan_available=False,
            input_modalities=frozenset(input_modalities),
            output_modalities=frozenset(output_modalities),
            capabilities=frozenset(capabilities),
            status=lifecycle_status or "CANDIDATE",
            metadata={
                **row,
                "catalog_id": row.get("id"),
                "catalog_task": task,
                "catalog_free_status": "unknown",
                "catalog_modalities_explicit": True,
            },
        )


class GoogleProvider(AIProvider):
    name = "google"

    def __init__(self, credential_scope: CredentialScope):
        if credential_scope not in {
            CredentialScope.USER_GOOGLE,
            CredentialScope.JUPITER_SHARED_GOOGLE,
        }:
            raise ValueError("Google adapter requires a Google credential scope.")
        self.credential_scope = credential_scope

    def list_models(self) -> list[ProviderModel]:
        try:
            models = []
            page_token: str | None = None
            seen_pages = set()
            seen_models = set()
            with httpx.Client(trust_env=False, timeout=35.0) as client:
                while True:
                    params = {
                        "pageSize": 1000,
                    }
                    if page_token:
                        params["pageToken"] = page_token
                    response = client.get(
                        "https://generativelanguage.googleapis.com/v1beta/models",
                        params=params,
                        headers={"x-goog-api-key": _credential(self.credential_scope)},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    for item in payload.get("models", []) if isinstance(payload, dict) else []:
                        if not isinstance(item, dict):
                            continue
                        resource_name = str(item.get("name") or "").strip()
                        model_id = resource_name.removeprefix("models/")
                        if not model_id or model_id in seen_models:
                            continue
                        seen_models.add(model_id)
                        methods = set(_string_values(item.get("supportedGenerationMethods")))
                        input_modalities = [
                            value.casefold() for value in _string_values(item.get("inputModalities"))
                        ]
                        output_modalities = [
                            value.casefold() for value in _string_values(item.get("outputModalities"))
                        ]
                        capabilities: set[str] = set()
                        modalities_explicit = bool(input_modalities or output_modalities)
                        if "embedContent" in methods or "batchEmbedContents" in methods:
                            capabilities.add("embeddings")
                            output_modalities = output_modalities or ["embedding"]
                        if "bidiGenerateContent" in methods:
                            capabilities.add("live")
                        if "image" in input_modalities:
                            capabilities.add("vision")
                        if "image" in output_modalities:
                            capabilities.add("image_generation")
                        if "audio" in output_modalities:
                            capabilities.add("tts")
                        if not input_modalities and "generateContent" in methods:
                            input_modalities = ["text"]
                        if not output_modalities and "generateContent" in methods:
                            output_modalities = ["text"]
                        available, lifecycle_status = _catalog_lifecycle(item)
                # Google catalog access does not by itself prove free-tier price.
                        models.append(ProviderModel(
                            provider=self.name,
                            model_id=model_id,
                            display_name=str(item.get("displayName") or model_id),
                            credential_scope=self.credential_scope,
                            available=available,
                            free_eligible=False,
                            free_verified=False,
                            free_plan_available=False,
                            input_modalities=frozenset(input_modalities),
                            output_modalities=frozenset(output_modalities),
                            capabilities=frozenset(capabilities),
                            context_window=item.get("inputTokenLimit") if isinstance(item.get("inputTokenLimit"), int) else None,
                            max_output=item.get("outputTokenLimit") if isinstance(item.get("outputTokenLimit"), int) else None,
                            status=lifecycle_status or "CANDIDATE",
                            metadata={
                                "resource_name": resource_name,
                                "supported_generation_methods": sorted(methods),
                                "catalog_free_status": "unknown",
                                "catalog_modalities_explicit": modalities_explicit,
                                "thinking": item.get("thinking") if isinstance(item.get("thinking"), bool) else None,
                            },
                        ))
                    page_token = str(payload.get("nextPageToken") or "") if isinstance(payload, dict) else ""
                    if not page_token:
                        break
                    if page_token in seen_pages:
                        raise ValueError("Repeated catalog page token.")
                    seen_pages.add(page_token)
            return models
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure(self.normalize_error(error)) from None

    @staticmethod
    def _contents(request: AIRequest):
        from google.genai import types

        contents = []
        system_parts = [request.system_context, request.personality_brief, request.memory_capsule]
        system_instruction = "\n\n".join(item for item in system_parts if item.strip())
        for message in request.messages:
            role = "model" if message.get("role") == "assistant" else message.get("role")
            if role == "system":
                system_instruction = "\n\n".join(
                    item for item in (system_instruction, str(message.get("content") or "")) if item
                )
                continue
            contents.append({"role": role, "parts": [{"text": str(message.get("content") or "")}]})
        if request.attachments:
            parts = []
            for attachment in request.attachments:
                if attachment.text is not None:
                    parts.append(types.Part.from_text(text=attachment.text))
                elif attachment.content is not None:
                    parts.append(types.Part.from_bytes(
                        data=attachment.content, mime_type=attachment.mime_type
                    ))
            if parts:
                contents.append({"role": "user", "parts": parts})
        return system_instruction, contents

    def generate(self, request: AIRequest, model: ProviderModel) -> AIResponse:
        try:
            from google import genai

            started = time.monotonic()
            system_instruction, contents = self._contents(request)
            config: dict[str, Any] = {}
            if system_instruction:
                config["system_instruction"] = system_instruction
            if request.max_output:
                config["max_output_tokens"] = request.max_output
            from services.search_grounding import requires_grounding, grounded_content
            grounded = requires_grounding(request)
            if grounded:
                config["tools"] = [{"google_search": {}}]
            elif request.structured_output_schema:
                config["response_mime_type"] = "application/json"
                config["response_json_schema"] = request.structured_output_schema
            client = _google_client(_credential(self.credential_scope, model))
            response = client.models.generate_content(
                model=model.model_id, contents=contents, config=config
            )
            usage = getattr(response, "usage_metadata", None)
            content = str(getattr(response, "text", "") or "")
            evidence = None
            if grounded:
                content, evidence = grounded_content(response)
            return AIResponse(
                content=content,
                structured_output={"grounding": evidence} if evidence else None,
                provider=self.name,
                requested_model=model.model_id,
                actual_model=str(getattr(response, "model_version", None) or model.model_id),
                input_usage=getattr(usage, "prompt_token_count", None),
                output_usage=getattr(usage, "candidates_token_count", None),
                latency_ms=int((time.monotonic() - started) * 1000),
                provider_request_id=getattr(response, "response_id", None),
            )
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure(self.normalize_error(error)) from None

    def stream(self, request: AIRequest, model: ProviderModel) -> Iterator[AIStreamEvent]:
        try:
            from google import genai

            from services.search_grounding import requires_grounding
            if requires_grounding(request):
                # Publish only after retrieval evidence arrives. Ordinary chat
                # remains incremental; unsupported web answers never leak first.
                yield AIStreamEvent(event_type="stage:searching_web", provider=self.name,
                                    requested_model=model.model_id, actual_model=model.model_id)
                result = self.generate(request, model)
                yield AIStreamEvent(text=result.content, provider=result.provider,
                                    requested_model=result.requested_model, actual_model=result.actual_model,
                                    usage={"input": result.input_usage or 0, "output": result.output_usage or 0})
                return

            system_instruction, contents = self._contents(request)
            config = {"system_instruction": system_instruction} if system_instruction else {}
            client = _google_client(_credential(self.credential_scope, model))
            emitted_text = False
            finish_reason = None
            for chunk in client.models.generate_content_stream(
                model=model.model_id, contents=contents, config=config
            ):
                usage = getattr(chunk, "usage_metadata", None)
                chunk_text = str(getattr(chunk, "text", "") or "")
                emitted_text = emitted_text or bool(chunk_text.strip())
                candidates = getattr(chunk, "candidates", None) or []
                if candidates and getattr(candidates[0], "finish_reason", None):
                    finish_reason = str(getattr(candidates[0].finish_reason, "name", candidates[0].finish_reason))
                yield AIStreamEvent(
                    text=chunk_text,
                    finish_reason=finish_reason,
                    provider=self.name,
                    requested_model=model.model_id,
                    actual_model=str(getattr(chunk, "model_version", None) or model.model_id),
                    usage={
                        "input": getattr(usage, "prompt_token_count", 0) or 0,
                        "output": getattr(usage, "candidates_token_count", 0) or 0,
                    } if usage else None,
                )
            if not emitted_text:
                # Thinking-only or safety/recitation stop: typed failure so the gateway falls back.
                raise ProviderFailure(NormalizedFailure(
                    FailureClass.UNKNOWN,
                    reason_code="EMPTY_RESPONSE",
                    safe_message=f"google returned no answer text (finish={str(finish_reason or 'NONE')[:24]}).",
                ))
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure(self.normalize_error(error)) from None

    def normalize_error(self, error: Exception) -> NormalizedFailure:
        detail = str(error).lower()
        status = getattr(error, "code", None)
        if not isinstance(status, int):
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            status = next((code for code in (400, 401, 403, 404, 410, 429, 500, 502, 503, 504)
                           if str(code) in detail), None)
        if "timeout" in detail or "deadline" in detail:
            return NormalizedFailure(FailureClass.TIMEOUT, retryable=True, safe_message="Google timed out.")
        failure_class = {
            400: FailureClass.BAD_REQUEST,
            401: FailureClass.AUTHENTICATION,
            403: FailureClass.PERMISSION,
            404: FailureClass.MODEL_REMOVED,
            410: FailureClass.MODEL_REMOVED,
            429: FailureClass.RATE_LIMIT,
            500: FailureClass.SERVER,
            502: FailureClass.SERVER,
            503: FailureClass.SERVER,
        }.get(status, FailureClass.UNKNOWN)
        if status is not None and 500 <= status <= 599:
            failure_class = FailureClass.SERVER
        # Live WebSocket failures can carry Google's symbolic status without
        # an HTTP code (the WebSocket close code is not an HTTP status).
        if (status is None or status >= 1000) and "resource_exhausted" in detail:
            status, failure_class = 429, FailureClass.QUOTA_EXHAUSTED
        elif (status is None or status >= 1000) and any(
            marker in detail for marker in ("rate_limited", "rate_limit_exceeded", "rate limit exceeded", "quota exceeded", "quota exhausted")
        ):
            status, failure_class = 429, FailureClass.RATE_LIMIT
        elif (status is None or status >= 1000) and ("model not found" in detail or "model is no longer available" in detail):
            status, failure_class = 404, FailureClass.MODEL_REMOVED
        quota_exhausted = status == 429 and any(
            marker in detail for marker in ("resource_exhausted", "quota exceeded", "quota exhausted")
        )
        quota_scope = _quota_failure_scope(detail, google=True) if status == 429 else None
        retry_after = _failure_retry_after(error, detail)
        reason_code = "QUOTA_EXHAUSTED" if quota_exhausted else "RATE_LIMITED" if status == 429 else None
        if quota_exhausted:
            failure_class = FailureClass.QUOTA_EXHAUSTED
        elif status == 429 and any(marker in detail for marker in ("capacity", "overloaded")):
            failure_class = FailureClass.SERVER
            reason_code = "PROVIDER_CAPACITY"
        if any(marker in detail for marker in ("terms acceptance", "requires terms", "accept the terms")):
            failure_class = FailureClass.PERMISSION
            reason_code = "TERMS_REQUIRED"
        elif any(marker in detail for marker in ("license required", "licensing required")):
            failure_class = FailureClass.PERMISSION
            reason_code = "LICENSING_REQUIRED"
        elif status == 403:
            reason_code = "API_DISABLED" if any(
                marker in detail for marker in ("not enabled", "api disabled", "has not been used")
            ) else "PERMISSION_DENIED"
            if reason_code == "API_DISABLED":
                quota_scope = "PROJECT"
            if any(marker in detail for marker in ("project disabled", "billing disabled", "consumer suspended", "service_disabled", "billing_disabled")):
                reason_code, quota_scope = "PROJECT_DISABLED", "PROJECT"
        invalid_key = status == 401 or (status in {400, 403} and any(
            marker in detail for marker in ("api key not valid", "api_key_invalid", "invalid api key", "api key expired")
        ))
        if invalid_key:
            failure_class, reason_code, quota_scope = FailureClass.AUTHENTICATION, "AUTH_FAILED", "CREDENTIAL"
        return NormalizedFailure(
            failure_class,
            status_code=status,
            retryable=(status == 429 and failure_class in {FailureClass.RATE_LIMIT, FailureClass.QUOTA_EXHAUSTED, FailureClass.SERVER})
            or (status is not None and 500 <= status <= 599),
            cooldown_seconds=retry_after or (300 if failure_class == FailureClass.MODEL_REMOVED else 0),
            quota_exhausted=quota_exhausted,
            auth_error=invalid_key,
            unsupported=status == 400 and any(
                marker in detail for marker in ("unsupported", "not supported", "does not support")
            ),
            reason_code=reason_code,
            quota_scope=quota_scope,
            safe_message={
                FailureClass.BAD_REQUEST: "Google rejected the request shape.",
                FailureClass.AUTHENTICATION: "The selected Google credential is invalid or unavailable.",
                FailureClass.PERMISSION: "The selected Google route is not permitted.",
                FailureClass.MODEL_REMOVED: "The selected Google model is unavailable.",
                FailureClass.RATE_LIMIT: "The selected Google route is temporarily rate limited.",
                FailureClass.QUOTA_EXHAUSTED: "The selected Google project/model quota is exhausted.",
                FailureClass.SERVER: "Google is temporarily unavailable.",
            }.get(failure_class, "Google could not complete the request."),
        )


@lru_cache(maxsize=1)
def provider_adapters() -> dict[tuple[str, str], AIProvider]:
    """Construct adapters without performing network or credential operations."""

    adapters: list[AIProvider] = [
        GoogleProvider(CredentialScope.USER_GOOGLE),
        GoogleProvider(CredentialScope.JUPITER_SHARED_GOOGLE),
        GroqProvider(),
        NvidiaProvider(),
        CloudflareProvider(),
        OpenCodeZenProvider(),
        OpenRouterProvider(),
        BytezProvider(),
    ]
    # Separate request-local adapters keep user secrets out of managed scopes
    # and preserve managed credential priority/fallback behavior.
    from services.optional_account_providers import MODEL_USER_SCOPES
    for adapter_type in (GroqProvider, NvidiaProvider, CloudflareProvider, OpenCodeZenProvider, OpenRouterProvider, BytezProvider):
        adapter = adapter_type()
        adapter.credential_scope = CredentialScope(MODEL_USER_SCOPES[adapter.name])
        adapters.append(adapter)
    return {(adapter.name, adapter.credential_scope.value): adapter for adapter in adapters}
