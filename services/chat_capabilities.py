"""Backend-authoritative, next-message chat capability selection and execution."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from difflib import get_close_matches
import hashlib
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx

from core.logger import Logger
from core.provider_contracts import (
    AIRequest,
    CredentialScope,
    PrivacyClass,
    ProviderModel,
    ReliabilityClass,
    TaskType,
)
from services.account_access import AccountAccessStore
from services.architecture_store import ArchitectureStore, ArchitectureStoreUnavailable
from services.integration_policy import GmailPolicy
from services.model_routing import FreeOnlyRouter
from services.credential_router import CredentialRouter
from config.credential_policy import managed_google_policy, managed_google_secret
from services.oauth_config import (
    OAuthConfigurationError,
    gmail_oauth_configured,
    gmail_oauth_credentials,
    gmail_oauth_is_testing,
)
from services.specialists import CloudflareImageSpecialist, SpecialistUnavailable


GMAIL = "gmail"
IMAGE_GENERATION = "image_generation"
IMAGE_EDITING = "image_editing"
WEATHER = "weather"
SUPPORTED_CAPABILITIES = frozenset({GMAIL, IMAGE_GENERATION, IMAGE_EDITING, WEATHER})

_IMAGE_REQUEST = re.compile(
    r"(?i)\b(generate|create|make|draw|design|illustrate|paint|render|produce)\b"
    r".{0,55}\b(image|picture|photo|poster|wallpaper|logo|illustration|artwork|graphic|banner|cover(?!\s+letter)|portrait)\b"
    # A bare draw/paint verb is a request only in request form ("draw a cat", "can you paint me ..."), never a
    # statement about the user ("I paint on weekends", "I like to draw" routed to image generation, 30-Sep).
    r"|(?:^|[.!?]\s*|\b(?:please|can\s+you|could\s+you|would\s+you|will\s+you|let'?s|i\s+want\s+you\s+to|"
    r"i'?d\s+like\s+you\s+to|jupiter,?)\s+)(draw|illustrate|paint)\b"
)
_IMAGE_DISCUSSION = re.compile(
    r"(?i)\b(how|why|what|explain|describe|compare|analy[sz]e|identify)\b.{0,60}"
    r"\b(image generation|images?|pictures?|photos?|art)\b"
)
_IMAGE_EDIT_REQUEST = re.compile(
    r"(?i)\b(edit|change|replace|remove|erase|improve|enhance|retouch|restore|recolor|"
    r"transform|restyle|upscale|sharpen|make\s+(?:this|the|my)\s+(?:image|photo|picture))\b"
    r"|\b(background|style|lighting|colors?|clarity)\b.{0,35}\b(change|remove|replace|improve|fix)\b"
)
# An edit verb alone is not an image request: "she may change phones later", "change the plan", "improve my resume"
# and "remove the old files" all reached the image editor (production memory turn, 30-Sep 17:32 IST). Image editing
# needs image context: an image word in the same sentence as the verb, an attached/chosen image, or a short edit
# command while the conversation already holds an image.
_IMAGE_NOUN = (
    r"(?:image|images|photo|photos|picture|pictures|pic|pics|selfie|selfies|screenshot|wallpaper|portrait|"
    r"headshot|avatar|thumbnail|background|foreground)"
)
_IMAGE_EDIT_VERB = (
    r"(?:edit|change|replace|remove|erase|improve|enhance|retouch|restore|recolou?r|colou?ri[sz]e|transform|"
    r"restyle|upscale|sharpen|crop|blur|unblur|brighten|darken|lighten|fix|adjust|swap|cut\s+out|clean\s+up)"
)
_IMAGE_EDIT_NEAR_NOUN = re.compile(
    rf"(?i)\b{_IMAGE_EDIT_VERB}\b[^.!?\n]{{0,45}}\b{_IMAGE_NOUN}\b"
    rf"|\b{_IMAGE_NOUN}\b[^.!?\n]{{0,35}}\b{_IMAGE_EDIT_VERB}\b"
    r"|\bmake\s+(?:this|the|my|that)\s+(?:image|photo|picture|pic|selfie)\b"
)
_IMAGE_EDIT_COMMAND = re.compile(
    rf"(?i)^\W*(?:(?:please|pls|jupiter,?|hey\s+jupiter,?)\s+)?(?:(?:can|could|would|will)\s+you\s+)?"
    rf"(?:{_IMAGE_EDIT_VERB}\b|make\s+(?:it|this|that)\b|turn\s+(?:it|this|that)\b|add\s+)"
)
_IMAGE_EDIT_SHORT_WORDS = 24
# A short follow-up command only counts as an image edit while an image is in play when it names something visual:
# "make it brighter" / "remove the background" edit the image; "make it shorter" / "fix the bug" do not.
_VISUAL_EDIT_WORD = re.compile(
    r"(?i)\b(?:crop|blur|unblur|brighten|darken|retouch|upscale|sharpen|recolou?r|colou?ri[sz]e|cut\s+out|"
    r"brighter|darker|lighter|black\s+and\s+white|b&w|gr[ae]yscale|sepia|vintage|retro|cartoon|anime|ghibli|pixar|"
    r"sketch|watercolou?r|oil\s+painting|realistic|photorealistic|cinematic|hd|4k|8k|sharper|clearer|blurr?y|"
    r"transparent|colou?rful|vibrant|warmer|cooler|sunset|night|snow(?:y)?|background|foreground|sky|hair|eyes|"
    r"face|smile|lighting|colou?rs?|saturation|contrast|exposure|shadows?|highlights|filter)\b"
)
# Text work about an image ("fix the grammar in this screenshot") is a chat/vision task, not an image edit.
_TEXT_TASK_OBJECT = re.compile(
    r"(?i)\b(?:grammar|spelling|typos?|code|bugs?|essay|answer|math|equation|homework|sentence|paragraph|resume|"
    r"cv|email|letter|story|poem|wording|translation)\b"
)
_CLEAR_TEXT_QUESTION = re.compile(
    r"(?i)^\s*(who|what|when|where|why|how|which|explain|tell me|calculate|solve|is|are|do|does|did)\b"
)
_GMAIL_EXPLICIT = re.compile(
    r"(?i)\b(gmail|inbox)\b|\b(email|e-mail|emial|mail)\b.{0,45}"
    r"\b(check|search|find|read|show|open|summari[sz]e|received?|got|latest|important|from|sent)\b"
    r"|\b(check|search|find|read|show|open|summari[sz]e)\b.{0,45}\b(email|e-mail|emial|mail)\b"
)
_GMAIL_NATURAL = re.compile(
    r"(?i)\b(did|has|have)\s+.{1,45}\s+(send|sent)\s+me\s+(anything|something|an?\s+(?:email|mail))\b"
    r"|\b(find|check|search)\b.{0,45}\b(otp|admission|order|college)\b.{0,25}\b(email|mail|inbox)?\b"
)
_GMAIL_PERSONAL_MAIL = re.compile(
    r"(?i)\b(did\s+i|have\s+i|has\s+my|did\s+my)\b.{0,55}"
    r"\b(receive|received|get|got|send|sent)\b.{0,35}\b(email|e-mail|mail|anything|something)\b"
)
_GMAIL_SELECTED_COMPATIBLE = re.compile(
    r"(?i)\b(today|latest|recent|important|otp|admission|college|order|from|received|inbox)\b"
    r"|\bwhat\s+did\s+.{1,35}\s+say\b"
)
_EMAIL_DISCUSSION = re.compile(
    r"(?i)^\s*(how|why|what|explain|tell me(?:\s+about)?|compare)\b.{0,70}\b(email|gmail|mail)\b"
    r"|^\s*(write|compose)\s+(an?\s+)?email\b(?!.*\b(gmail|draft|send|my account)\b)"
)
_WEATHER_REQUEST = re.compile(r"(?i)\b(weather|forecast|temperature|rain|umbrella|wind|humidity)\b")
_LATEST_EMAIL_REQUEST = re.compile(
    r"(?ix)\b(?:"
    r"(?:latest|newest|most\s+recent|last)\s+(?:inbox\s+)?"
    r"(?:gmail(?:\s+(?:message|msg|email))?|"
    r"(?:email|e-mail|emial|mail|message|msg)(?:\s+(?:(?:in|on|from)\s+)?gmail)?)"
    r"(?:\s+(?:that\s+)?i\s+(?:received|got))?"
    r"|(?:email|e-mail|emial|mail|message)\s+(?:did|have)\s+i\s+(?:receive|get)\s+(?:most\s+recently|last)"
    r"|(?:email|e-mail|emial|mail|message)\s+i\s+(?:received|got)\s+(?:most\s+recently|last)"
    r"|who\s+(?:emailed|e-mailed|mailed)\s+me\s+(?:last|most\s+recently)"
    r")\b(?!\s+(?:settings|versions?|updates?|features?|app)\b)"
)


def is_latest_email_request(text: str) -> bool:
    """Recognize latest-inbox semantics before generic Gmail search parsing."""

    message = " ".join(str(text or "").split())
    # Instructions about using Gmail are not consent to inspect an inbox.
    if re.match(r"(?i)^how\s+(?:to|do\s+i|can\s+i|does|do\s+you)\b", message):
        return False
    return bool(_LATEST_EMAIL_REQUEST.search(message))


_LATEST_EMAIL_OUTPUT_REQUEST = re.compile(
    r"(?ix)^(?:and\s+)?(?:please\s+)?(?:"
    r"show|include|display|give|tell|read|open|summari[sz]e|list|provide"
    r")\b"
)


def latest_email_filter_fragment(text: str) -> str:
    """Return the requested latest-email match target, excluding commentary/output fields.

    This is intentionally generic: it uses the portion after the latest-email
    intent, sentence boundaries, and ordinary relationship words rather than
    knowing any particular sender, organisation, school, or topic.
    """

    normalized = " ".join(str(text or "").split())
    match = _LATEST_EMAIL_REQUEST.search(normalized)
    if not match:
        return ""
    tail = normalized[match.end():].strip(" \t:-,;")
    if not tail:
        return ""
    tail = re.split(r"[?!]", tail, maxsplit=1)[0].strip(" \t:-,;.")
    tail = re.split(r"(?i)\b(?:because|since|as\s+that)\b", tail, maxsplit=1)[0].strip(" \t:-,;.")
    if (
        not tail
        or _LATEST_EMAIL_OUTPUT_REQUEST.match(tail)
        or re.match(r"(?i)^(?:that\s+)?i\s+(?:received|got)\b", tail)
    ):
        return ""
    tail = re.split(
        r"(?i)\s*(?:,|;)?\s+and\s+(?:show|include|display|give|tell|read|open|summari[sz]e|list|provide)\b",
        tail,
        maxsplit=1,
    )[0].strip(" \t:-,;.")
    tail = re.sub(
        r"(?i)^(?:(?:from|by|about|mentioning|matching|containing|regarding|for)"
        r"|(?:with\s+)?subject(?:\s+(?:is|contains|about))?)\s+",
        "",
        tail,
        count=1,
    ).strip(" \t:-,;.")
    return tail[:180]


@dataclass(frozen=True)
class CapabilityDecision:
    requested: tuple[str, ...]
    detected: tuple[str, ...]
    executed: tuple[str, ...]
    ignored_requested: tuple[str, ...]

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "requested": list(self.requested),
            "detected": list(self.detected),
            "executed": list(self.executed),
            "ignored_requested": list(self.ignored_requested),
        }


def normalize_selected_capabilities(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    aliases = {
        "gmail": GMAIL,
        "email": GMAIL,
        "image": IMAGE_GENERATION,
        "create_image": IMAGE_GENERATION,
        "image_generation": IMAGE_GENERATION,
        "image_editing": IMAGE_EDITING,
        "weather": WEATHER,
    }
    normalized = []
    for raw in values or ():
        capability = aliases.get(str(raw or "").strip().casefold())
        if capability in SUPPORTED_CAPABILITIES and capability not in normalized:
            normalized.append(capability)
    return tuple(normalized)


def is_image_edit_request(message: str, *, image_context: bool = False, recent_image: bool = False) -> bool:
    """True only for a genuine image-edit request (see _IMAGE_EDIT_NEAR_NOUN)."""
    if not message or _IMAGE_DISCUSSION.search(message) or _TEXT_TASK_OBJECT.search(message):
        return False
    if _IMAGE_EDIT_NEAR_NOUN.search(message):
        return True
    short_command = len(message.split()) <= _IMAGE_EDIT_SHORT_WORDS and bool(_IMAGE_EDIT_COMMAND.search(message))
    if image_context and (short_command or (
        len(message.split()) <= _IMAGE_EDIT_SHORT_WORDS and _IMAGE_EDIT_REQUEST.search(message)
    )):
        return True
    return bool(recent_image and short_command and _VISUAL_EDIT_WORD.search(message))


def needs_recent_image_context(text: str) -> bool:
    """Whether routing this message depends on an image already in the conversation (a cheap pre-check)."""
    message = " ".join(str(text or "").split())
    return bool(message) and len(message.split()) <= _IMAGE_EDIT_SHORT_WORDS and bool(
        _IMAGE_EDIT_COMMAND.search(message) and _VISUAL_EDIT_WORD.search(message)
        and not _TEXT_TASK_OBJECT.search(message) and not _IMAGE_EDIT_NEAR_NOUN.search(message)
    )


# Marker 223 (real 28 Oct: "my owner acc maintainer@example.invalid" -> "Gmail isn't connected"): the "gmail" inside an
# address is not a request to use Gmail. Addresses are blanked before the Gmail patterns run.
_EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def without_email_addresses(text: str) -> str:
    return " ".join(_EMAIL_ADDRESS.sub(" ", str(text or "")).split())


def detect_capability_intent(
    text: str,
    selected: list[str] | tuple[str, ...] | None = None,
    *,
    image_context: bool = False,
    recent_image: bool = False,
) -> CapabilityDecision:
    """image_context: an image is attached/chosen for this turn; recent_image: the conversation already holds one."""
    message = " ".join(str(text or "").split())
    requested = normalize_selected_capabilities(selected)
    detected: list[str] = []

    mail_text = without_email_addresses(message)
    latest_email = is_latest_email_request(mail_text)
    gmail_natural = bool(
        message
        and (
            latest_email
            or (
                not _EMAIL_DISCUSSION.search(mail_text)
                and (
                    _GMAIL_EXPLICIT.search(mail_text)
                    or _GMAIL_NATURAL.search(mail_text)
                    or _GMAIL_PERSONAL_MAIL.search(mail_text)
                    or GMAIL in requested
                )
            )
        )
    )
    if gmail_natural:
        detected.append(GMAIL)

    if message and (_WEATHER_REQUEST.search(message) or WEATHER in requested):
        detected.append(WEATHER)

    image_edit_natural = is_image_edit_request(
        message,
        image_context=image_context or IMAGE_EDITING in requested,
        recent_image=recent_image,
    )
    if image_edit_natural:
        detected.append(IMAGE_EDITING)

    image_natural = bool(
        message and _IMAGE_REQUEST.search(message) and not _IMAGE_DISCUSSION.search(message)
    )
    image_selected_prompt = bool(
        IMAGE_GENERATION in requested
        and message
        and not image_natural
        and not _CLEAR_TEXT_QUESTION.search(message)
        and not _IMAGE_DISCUSSION.search(message)
    )
    if (image_natural or image_selected_prompt) and not image_edit_natural:
        detected.append(IMAGE_GENERATION)

    executed = tuple(capability for capability in (GMAIL, WEATHER, IMAGE_EDITING, IMAGE_GENERATION) if capability in detected)
    ignored = tuple(capability for capability in requested if capability not in executed)
    return CapabilityDecision(requested, tuple(detected), executed, ignored)


class GmailNotConnected(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        state: str = "AUTHORIZATION_REQUIRED",
        category: str | None = None,
    ):
        super().__init__(message)
        self.state = state
        self.category = category or (
            "USER_NOT_CONNECTED" if state == "AUTHORIZATION_REQUIRED" else "ACCESS_REVOKED"
        )


class GmailOAuthNotConfigured(RuntimeError):
    pass


class GmailOAuthCallbackError(RuntimeError):
    def __init__(self, message: str, *, state: str):
        super().__init__(message)
        self.state = state


class GmailProviderFailure(RuntimeError):
    """A normalized, render-safe Gmail failure with no provider response body."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "UNKNOWN_ERROR",
        status_code: int | None = None,
        reason_code: str | None = None,
        retry_after: str | None = None,
        correlation_id: str | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.reason_code = reason_code
        self.retry_after = retry_after
        self.correlation_id = correlation_id or uuid4().hex[:12]


_oauth_states: dict[str, dict[str, Any]] = {}
_oauth_state_lock = Lock()


_gmail_refresh_locks: dict[str, threading.Lock] = {}
_gmail_refresh_locks_guard = threading.Lock()


def _gmail_refresh_lock(email: str) -> threading.Lock:
    """One token refresh at a time per user: concurrent requests reuse the fresh token instead of racing."""
    key = str(email or "").strip().casefold()
    with _gmail_refresh_locks_guard:
        return _gmail_refresh_locks.setdefault(key, threading.Lock())


class GmailService:
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    REVOKE_URL = "https://oauth2.googleapis.com/revoke"
    API_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
    API_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

    def __init__(self, store: AccountAccessStore):
        self.store = store

    @staticmethod
    def _google_reason(response: httpx.Response) -> str:
        """Extract only Google's machine-readable reason, never its response message."""

        try:
            payload = response.json()
        except Exception:
            return ""
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return ""
        # Prefer the provider's specific machine reason over the generic
        # top-level status (commonly PERMISSION_DENIED). Otherwise a disabled
        # Gmail API is indistinguishable from a missing OAuth scope.
        candidates: list[Any] = list(
            item.get("reason")
            for item in error.get("errors") or []
            if isinstance(item, dict)
        )
        for detail in error.get("details") or []:
            if not isinstance(detail, dict):
                continue
            metadata = detail.get("metadata")
            candidates.extend((
                detail.get("reason"),
                metadata.get("reason") if isinstance(metadata, dict) else None,
            ))
        candidates.append(error.get("status"))
        for candidate in candidates:
            raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(candidate or ""))
            normalized = re.sub(r"[^A-Z0-9_]+", "_", raw.upper()).strip("_")
            if normalized:
                return normalized[:80]
        return ""

    @staticmethod
    def _oauth_error_code(response: httpx.Response) -> str:
        """Return only an OAuth machine code, never Google's descriptive text."""

        try:
            payload = response.json()
        except Exception:
            return ""
        raw_error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(raw_error, dict):
            raw_error = raw_error.get("status") or raw_error.get("code")
        normalized = re.sub(r"[^A-Z0-9_]+", "_", str(raw_error or "").upper()).strip("_")
        return normalized[:80]

    @classmethod
    def _response_failure(cls, response: httpx.Response, *, stage: str) -> GmailProviderFailure:
        status = int(response.status_code)
        reason = cls._google_reason(response)
        retry_after = str(response.headers.get("Retry-After") or "").strip()[:40] or None
        if status == 401:
            category = "ACCESS_REVOKED"
        elif status == 429 or reason in {
            "RATE_LIMIT_EXCEEDED", "USER_RATE_LIMIT_EXCEEDED", "DAILY_LIMIT_EXCEEDED",
            "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED",
        }:
            category = "QUOTA_EXHAUSTED"
        elif status == 403 and reason in {
            "ACCESS_NOT_CONFIGURED", "API_NOT_ENABLED", "SERVICE_DISABLED",
            "SERVICE_NOT_ENABLED", "PERMISSION_DENIED_SERVICE_DISABLED",
        }:
            category = "API_NOT_ENABLED"
        elif status == 403:
            category = "PERMISSION_DENIED"
        elif status >= 500:
            category = "PROVIDER_OUTAGE"
        else:
            category = "UNKNOWN_ERROR"
        return GmailProviderFailure(
            "Gmail request failed.",
            category=category,
            status_code=status,
            reason_code=reason or f"HTTP_{status}",
            retry_after=retry_after,
        )

    @staticmethod
    def _network_failure(error: Exception) -> GmailProviderFailure:
        category = "TIMEOUT" if isinstance(error, httpx.TimeoutException) else "NETWORK_ERROR"
        return GmailProviderFailure(
            "Gmail could not be reached.",
            category=category,
            reason_code=type(error).__name__.upper()[:80],
        )

    @classmethod
    def _get(cls, url: str, **kwargs) -> httpx.Response:
        try:
            return httpx.get(url, **kwargs)
        except httpx.RequestError as error:
            raise cls._network_failure(error) from None

    def _mark_reauthorization_required(self, email: str, token: dict, category: str) -> None:
        marker = {
            key: token.get(key)
            for key in (
                "oauth_client_type", "authorized_at", "testing_authorization_expires_at"
            )
            if token.get(key) is not None
        }
        marker["reauthorization_reason"] = category
        scopes = self.store.gmail_connection_metadata(email).get("scopes", [])
        self.store.store_gmail_token(email, marker, scopes)

    def _raise_response_failure(self, email: str, response: httpx.Response, *, stage: str) -> None:
        failure = self._response_failure(response, stage=stage)
        if failure.category == "ACCESS_REVOKED":
            # _authorized_get already forced a refresh that Google ACCEPTED (an invalid grant would have raised
            # there), so this 401 does not prove revocation. Keep the stored grant; report a retryable failure.
            raise GmailProviderFailure(
                "Gmail rejected the request right after a successful authorization refresh.",
                category="PROVIDER_OUTAGE",
                status_code=failure.status_code,
                reason_code=failure.reason_code,
                retry_after=failure.retry_after,
            )
        raise failure

    @staticmethod
    def _record_health(
        email: str,
        *,
        started: float,
        success: bool,
        failure: GmailNotConnected | GmailProviderFailure | None = None,
    ) -> None:
        try:
            from services.api_health_center import api_health_center

            category = getattr(failure, "category", None)
            api_health_center().record_attempt(
                user_id=email,
                provider="google_gmail",
                requested_model="gmail-v1",
                actual_model="gmail-v1" if success else None,
                task_type="gmail_read",
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                success=success,
                status_code=getattr(failure, "status_code", None),
                error_class=category,
                fallback_number=0,
                reason_code=getattr(failure, "reason_code", None),
                safe_message=(
                    "Gmail read succeeded."
                    if success
                    else f"Gmail read failed: {category or 'UNKNOWN_ERROR'}."
                ),
                details={"retry_after": getattr(failure, "retry_after", None)},
            )
        except Exception:
            # Health telemetry must never block or alter a Gmail response.
            return

    @staticmethod
    def _client_credentials(client_type: str = "web") -> tuple[str, str]:
        try:
            credentials = gmail_oauth_credentials(client_type)
        except OAuthConfigurationError as error:
            raise GmailOAuthNotConfigured(str(error)) from None
        return credentials.client_id, credentials.client_secret

    @staticmethod
    def _testing_authorization_expired(token: dict | None) -> bool:
        """Whether Google's Testing-mode 7-day refresh-token lifetime has PROBABLY passed. Used only to explain an
        invalid_grant; never to disconnect on Jupiter's own clock (GMAIL_OAUTH_PUBLISHING_STATUS may not match the
        real consent-screen status, and a published app's grant does not expire after 7 days)."""
        if not token or not gmail_oauth_is_testing():
            return False
        expires_at = float(token.get("testing_authorization_expires_at") or 0)
        return bool(expires_at and expires_at <= time.time())

    def status(self, email: str, client_type: str = "web") -> dict:
        metadata = self.store.gmail_connection_metadata(email)
        token = self.store.gmail_token(email) if metadata.get("connected") else None
        # A saved grant belongs to its issuing client, not the current UI platform.
        grant_client_type = str((token or {}).get("oauth_client_type") or client_type)
        configured = gmail_oauth_configured(grant_client_type)
        retryable = False
        if not configured:
            state = "CONFIGURATION_REQUIRED"
            connection_state = "NOT_CONNECTED"
        elif not metadata.get("connected"):
            state = "AUTHORIZATION_REQUIRED"
            connection_state = "NOT_CONNECTED"
        elif not token:
            state = "REAUTHORIZATION_REQUIRED"
            connection_state = "REFRESH_FAILED"
        elif token.get("reauthorization_reason"):
            reason = str(token.get("reauthorization_reason") or "")
            state = "REAUTHORIZATION_REQUIRED"
            connection_state = {
                "SCOPE_MISSING": "SCOPE_MISSING",
                "REFRESH_FAILED": "REFRESH_FAILED",
                "ACCESS_REVOKED": "AUTH_REVOKED",
                "TESTING_EXPIRED": "TESTING_EXPIRED",
            }.get(reason, "AUTH_REVOKED")
        elif GmailPolicy.required_scopes(read=True, draft=False, send=False)[0] not in set(metadata.get("scopes", [])):
            state = "REAUTHORIZATION_REQUIRED"
            connection_state = "SCOPE_MISSING"
        elif token.get("access_token") and float(token.get("expires_at") or 0) > time.time():
            state = "AUTHORIZED"
            connection_state = "CONNECTED"
        elif token.get("refresh_token"):
            # Status is also the login/startup reconciliation point. Refresh an
            # expired access token here instead of making a normal expiry look
            # like a disconnected Gmail account.
            try:
                self._access_token(email, force_refresh=True)
                state = "AUTHORIZED"
                connection_state = "CONNECTED"
            except GmailNotConnected as error:
                state = "REAUTHORIZATION_REQUIRED"
                connection_state = {
                    "SCOPE_MISSING": "SCOPE_MISSING",
                    "PERMISSION_DENIED": "SCOPE_MISSING",
                    "ACCESS_REVOKED": "AUTH_REVOKED",
                    "TESTING_EXPIRED": "TESTING_EXPIRED",
                }.get(error.category, "REFRESH_FAILED")
            except GmailProviderFailure as error:
                # Only GmailNotConnected proves that consent/grant is invalid.
                # A failed provider response (including missing access_token)
                # must not make a still-stored refresh grant look revoked.
                retryable = True
                state = "TOKEN_REFRESH_REQUIRED"
                connection_state = "REFRESHING"
            except GmailOAuthNotConfigured:
                state = "CONFIGURATION_REQUIRED"
                connection_state = "NOT_CONNECTED"
        else:
            state = "REAUTHORIZATION_REQUIRED"
            connection_state = "REFRESH_FAILED"
        return {
            **metadata,
            # A database row can intentionally contain only a reauthorization
            # marker. Do not report that marker as a usable Gmail connection.
            "connected": connection_state in {"CONNECTED", "REFRESHING"},
            "configured": configured,
            "state": state,
            "connection_state": connection_state,
            "retryable": retryable,
            "oauth_client_type": "desktop" if grant_client_type == "desktop" else "web",
            "testing_authorization": gmail_oauth_is_testing(),
            "testing_authorization_expires_at": (token or {}).get("testing_authorization_expires_at"),
        }

    def authorization_url(
        self,
        email: str,
        redirect_uri: str,
        return_to: str,
        client_type: str = "web",
    ) -> tuple[str, str]:
        normalized_client_type = "desktop" if client_type == "desktop" else "web"
        client_id, _ = self._client_credentials(normalized_client_type)
        safe_return = return_to if return_to in {"/windows/", "/mobile/"} else "/windows/"
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        with _oauth_state_lock:
            now = time.time()
            for key, value in list(_oauth_states.items()):
                if now - float(value.get("created", 0)) > 900:
                    _oauth_states.pop(key, None)
            _oauth_states[state] = {
                "email": email.strip().lower(),
                "redirect_uri": redirect_uri,
                "return_to": safe_return,
                "client_type": normalized_client_type,
                "code_verifier": code_verifier,
                "created": now,
            }
        scopes = GmailPolicy.required_scopes(read=True, draft=False, send=False)
        query = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })
        return f"{self.AUTH_URL}?{query}", state

    def complete_authorization(self, state: str, code: str) -> dict:
        with _oauth_state_lock:
            pending = _oauth_states.pop(state, None)
        if not pending or time.time() - float(pending.get("created", 0)) > 900:
            raise GmailOAuthCallbackError(
                "The Gmail connection request expired. Please try again.",
                state="OAUTH_STATE_EXPIRED",
            )
        client_type = str(pending.get("client_type") or "web")
        client_id, client_secret = self._client_credentials(client_type)
        try:
            response = httpx.post(
                self.TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": pending["redirect_uri"],
                    "code_verifier": pending["code_verifier"],
                },
                timeout=30.0,
            )
        except httpx.TimeoutException:
            raise GmailOAuthCallbackError(
                "Google's OAuth token exchange timed out.", state="OAUTH_TIMEOUT"
            ) from None
        except httpx.RequestError:
            raise GmailOAuthCallbackError(
                "Google's OAuth token exchange could not be reached.", state="OAUTH_NETWORK_ERROR"
            ) from None
        if response.status_code != 200:
            oauth_error = self._oauth_error_code(response)
            state = "REDIRECT_URI_MISMATCH" if oauth_error == "REDIRECT_URI_MISMATCH" else "OAUTH_PROVIDER_FAILURE"
            raise GmailOAuthCallbackError(
                "Google rejected the Gmail authorization callback.", state=state
            )
        token = response.json()
        if not token.get("access_token"):
            raise GmailOAuthCallbackError(
                "Google did not return a Gmail access token.", state="MISSING_ACCESS_TOKEN"
            )
        if not token.get("refresh_token"):
            previous = self.store.gmail_token(pending["email"]) or {}
            # A refresh token is bound to the OAuth client that issued it (desktop vs web): reusing one across
            # clients makes every later refresh fail and looks like a disconnection.
            if previous.get("refresh_token") and str(previous.get("oauth_client_type") or "web") == client_type:
                token["refresh_token"] = previous["refresh_token"]
        self._store_authorized_token(pending["email"], token, client_type)
        return pending

    def complete_android_authorization(self, email: str, code: str) -> dict:
        client_id, client_secret = self._client_credentials("web")
        response = httpx.post(
            self.TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=30.0,
        )
        if response.status_code != 200:
            raise GmailOAuthCallbackError(
                "Google rejected the Android Gmail authorization code.",
                state="OAUTH_PROVIDER_FAILURE",
            )
        token = response.json()
        if not token.get("access_token") or not token.get("refresh_token"):
            raise GmailOAuthCallbackError(
                "Google did not return offline Gmail authorization.",
                state="REAUTHORIZATION_REQUIRED",
            )
        self._store_authorized_token(email, token, "web")
        return self.status(email, "web")

    def _store_authorized_token(self, email: str, token: dict, client_type: str) -> None:
        normalized_email = str(email or "").strip().lower()
        required_scope = GmailPolicy.required_scopes(read=True, draft=False, send=False)[0]
        scopes = str(token.get("scope") or "").split() or [required_scope]
        if required_scope not in scopes:
            raise GmailOAuthCallbackError(
                "Google did not grant Gmail read-only access.",
                state="REAUTHORIZATION_REQUIRED",
            )
        now = time.time()
        token["expires_at"] = now + max(60, int(token.get("expires_in") or 3600) - 30)
        token["oauth_client_type"] = "desktop" if client_type == "desktop" else "web"
        token["authorized_at"] = now
        if gmail_oauth_is_testing():
            token["testing_authorization_expires_at"] = now + (7 * 24 * 60 * 60)
        else:
            token.pop("testing_authorization_expires_at", None)
        with _gmail_refresh_lock(normalized_email):
            self.store.store_gmail_token(normalized_email, token, [required_scope])

    def disconnect(self, email: str) -> dict:
        """Revoke only this user's Google grant, then remove only their local token."""

        # Serialize deletion with refresh/consent writes so an in-flight refresh
        # cannot restore the token after this explicit disconnect has finished.
        with _gmail_refresh_lock(email):
            return self._disconnect_locked(email)

    def _disconnect_locked(self, email: str) -> dict:
        token = self.store.gmail_token(email) or {}
        revocation_token = token.get("refresh_token") or token.get("access_token")
        revoked = False
        if revocation_token:
            try:
                response = httpx.post(
                    self.REVOKE_URL,
                    params={"token": str(revocation_token)},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=20.0,
                )
                revoked = response.status_code == 200
            except Exception:
                revoked = False
        self.store.remove_gmail_token(email)
        return {
            "connected": False,
            "state": "AUTHORIZATION_REQUIRED",
            "revocation_requested": bool(revocation_token),
            "revoked": revoked,
        }

    def _access_token(self, email: str, *, force_refresh: bool = False) -> str:
        token = self.store.gmail_token(email)
        if not token:
            metadata = self.store.gmail_connection_metadata(email)
            state = "REAUTHORIZATION_REQUIRED" if metadata.get("connected") else "AUTHORIZATION_REQUIRED"
            raise GmailNotConnected(
                "Your Gmail connection needs to be reconnected."
                if state == "REAUTHORIZATION_REQUIRED"
                else "Gmail isn't connected to this Jupiter account. Connect Gmail to continue.",
                state=state,
                category=("ACCESS_REVOKED" if state == "REAUTHORIZATION_REQUIRED" else "USER_NOT_CONNECTED"),
            )
        observed_access_token = token.get("access_token")
        with _gmail_refresh_lock(email):
            current = self.store.gmail_token(email)
            if not current:
                raise GmailNotConnected(
                    "Gmail isn't connected to this Jupiter account. Connect Gmail to continue.",
                    state="AUTHORIZATION_REQUIRED", category="USER_NOT_CONNECTED",
                )
            required_scope = GmailPolicy.required_scopes(read=True, draft=False, send=False)[0]
            granted_scopes = set(self.store.gmail_connection_metadata(email).get("scopes", []))
            if required_scope not in granted_scopes:
                self._mark_reauthorization_required(email, current, "SCOPE_MISSING")
                raise GmailNotConnected(
                    "Jupiter does not have Gmail read permission. Reconnect Gmail.",
                    state="REAUTHORIZATION_REQUIRED", category="SCOPE_MISSING",
                )
            if (current.get("access_token")
                    and (not force_refresh or current.get("access_token") != observed_access_token)
                    and float(current.get("expires_at") or 0) > time.time()):
                return str(current["access_token"])              # a concurrent request already refreshed it
            return self._refresh_access_token(email, current)

    def _refresh_access_token(self, email: str, token: dict) -> str:
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            self._mark_reauthorization_required(email, token, "REFRESH_FAILED")
            raise GmailNotConnected(
                "Reconnect Gmail to complete this request.",
                state="REAUTHORIZATION_REQUIRED",
                category="REFRESH_FAILED",
            )
        client_type = str(token.get("oauth_client_type") or "web")
        client_id, client_secret = self._client_credentials(client_type)
        try:
            response = httpx.post(
                self.TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30.0,
            )
        except httpx.RequestError as error:
            raise self._network_failure(error) from None
        if response.status_code in {400, 401}:
            reason = self._oauth_error_code(response) or self._google_reason(response)
            if reason in {"INVALID_GRANT", "UNAUTHORIZED"}:
                # Google itself rejected the grant: revoked, password-changed, or the Testing-mode 7-day lifetime
                # ended. Only now is reconnecting genuinely required.
                testing_expired = self._testing_authorization_expired(token)
                category = "TESTING_EXPIRED" if testing_expired else "ACCESS_REVOKED"
                self._mark_reauthorization_required(email, token, category)
                raise GmailNotConnected(
                    "Google ended Jupiter's Gmail test authorization (Testing-mode apps expire after 7 days). "
                    "Reconnect Gmail." if testing_expired else "Gmail authorization was revoked. Reconnect Gmail.",
                    state="REAUTHORIZATION_REQUIRED",
                    category=category,
                )
            raise GmailProviderFailure(
                "Gmail authorization could not be refreshed.",
                category="REFRESH_FAILED",
                status_code=response.status_code,
                reason_code=reason or f"HTTP_{response.status_code}",
            )
        if response.status_code != 200:
            failure = self._response_failure(response, stage="token_refresh")
            raise GmailProviderFailure(
                "Gmail authorization refresh is temporarily unavailable.",
                category=failure.category,
                status_code=failure.status_code,
                reason_code=failure.reason_code,
                retry_after=failure.retry_after,
            )
        try:
            refreshed = response.json()
        except ValueError:
            refreshed = None
        if not isinstance(refreshed, dict) or not refreshed.get("access_token"):
            raise GmailProviderFailure(
                "Gmail did not return a refreshed access token.",
                category="REFRESH_FAILED",
                status_code=200,
                reason_code="MISSING_ACCESS_TOKEN",
            )
        try:
            expires_in = int(refreshed.get("expires_in") or 3600)
        except (TypeError, ValueError, OverflowError):
            raise GmailProviderFailure(
                "Gmail did not return a valid token expiry.", category="REFRESH_FAILED",
                status_code=200, reason_code="INVALID_TOKEN_EXPIRY",
            ) from None
        authorized_at = token.get("authorized_at")
        testing_expires_at = token.get("testing_authorization_expires_at")
        token.update(refreshed)
        token["refresh_token"] = refresh_token
        token["oauth_client_type"] = client_type
        if authorized_at:
            token["authorized_at"] = authorized_at
        if testing_expires_at:
            token["testing_authorization_expires_at"] = testing_expires_at
        token["expires_at"] = time.time() + max(60, expires_in - 30)
        scopes = str(token.get("scope") or "").split() or self.store.gmail_connection_metadata(email).get("scopes", [])
        self.store.store_gmail_token(email, token, scopes)
        return str(token["access_token"])

    def _authorized_get(self, email: str, url: str, **kwargs) -> httpx.Response:
        """GET with one silent refresh/retry when Google rejects an access token."""

        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self._access_token(email)}"
        response = self._get(url, headers=headers, **kwargs)
        if response.status_code == 401:
            headers["Authorization"] = f"Bearer {self._access_token(email, force_refresh=True)}"
            response = self._get(url, headers=headers, **kwargs)
        return response

    @staticmethod
    def _search_query(request_text: str) -> str:
        latest_filter = GmailService._latest_filter_query(request_text)
        if is_latest_email_request(request_text):
            return latest_filter
        text = " ".join(request_text.split())
        parts = []
        if re.search(r"(?i)\b(today|this morning|this afternoon|this evening)\b", text):
            parts.append(f"after:{datetime.now().strftime('%Y/%m/%d')}")
        elif re.search(r"(?i)\b(this week|recent)\b", text):
            parts.append("newer_than:7d")
        if re.search(r"(?i)\bunread\b", text):
            parts.append("is:unread")
        if re.search(r"(?i)\bimportant\b", text):
            parts.append("is:important")
        sender_match = re.search(
            r"(?i)\bfrom\s+([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|[A-Z][A-Z0-9 .'-]{1,40}?)"
            r"(?=\s+(?:today|this week|recent|about|with|and|please)\b|[?!,.;]|$)",
            text,
        )
        if sender_match:
            sender = sender_match.group(1).strip().replace('"', "")
            if sender:
                parts.append(f'from:"{sender}"')
        subject_match = re.search(
            r"(?i)\b(?:with\s+)?subject(?:\s+(?:is|contains|about))?\s+[\"']?([^\"'?!,;]{2,60})",
            text,
        )
        if subject_match:
            subject = subject_match.group(1).strip().replace('"', "")
            if subject:
                parts.append(f'subject:"{subject}"')
        meaningful = re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]{2,}", text)
        stop = {
            "check", "search", "find", "gmail", "email", "emails", "mail", "inbox",
            "please", "latest", "important", "today", "recent", "summarize", "summary",
            "what", "when", "where", "whether", "from", "about", "anything", "something",
            "received", "receive", "sent", "send", "show", "read", "open", "that", "this",
            "have", "has", "did", "got", "mine", "your", "their", "they", "there", "were",
            "subject", "sender", "contains", "titled", "any", "new",
        }
        terms = [word for word in meaningful if word.casefold() not in stop][:5]
        parts.extend(terms)
        return " ".join(parts)

    @classmethod
    def query_needs_semantic_parser(cls, request_text: str) -> bool:
        """Use a private model only when deterministic Gmail parsing is ambiguous."""

        text = " ".join(str(request_text or "").split())
        if not text or is_latest_email_request(text):
            return False
        if re.fullmatch(
            r"(?i)(?:please\s+)?(?:(?:check|show|open|read)\s+(?:my\s+)?|any\s+new\s+)"
            r"(?:gmail|inbox|emails?|mail)[.!?]*", text,
        ):
            return False  # Plain mailbox reads need neither a model nor search keywords.
        deterministic = cls._search_query(text)
        relative_or_indirect = bool(re.search(
            r"(?i)\b(around|approximately|sometime|near|regarding|related to|"
            r"the (?:message|email) (?:i|we|he|she|they)|what did .+ send|"
            r"physical verification|earlier that day|later that day)\b",
            text,
        ))
        # Ordinary from:/subject/date/keyword requests stay on the deterministic
        # zero-model path. The model is a parser fallback, never the default.
        return relative_or_indirect or not deterministic.strip()

    @staticmethod
    def safe_structured_query(value: str) -> str:
        """Validate the model-produced read-only Gmail search expression."""

        query = " ".join(str(value or "").split()).strip()
        if not query or len(query) > 300 or any(ord(char) < 32 for char in query):
            return ""
        # Gmail search is read-only, but still reject URL/control-like output and
        # keep the parser result to the documented query grammar's plain tokens.
        if re.search(r"(?i)(?:https?://|javascript:|<|>|\{|\}|\[|\])", query):
            return ""
        if not re.fullmatch(r"[\w@.+\-:/\"'()\s]+", query, flags=re.UNICODE):
            return ""
        return query

    @staticmethod
    def _latest_filter_query(request_text: str) -> str:
        fragment = latest_email_filter_fragment(request_text)
        if not fragment:
            return ""
        normalized = " ".join(str(request_text or "").split())
        match = _LATEST_EMAIL_REQUEST.search(normalized)
        tail = normalized[match.end():] if match else ""
        safe_fragment = fragment.replace('"', "").strip()
        if not safe_fragment:
            return ""
        if re.match(r"(?i)^\s*(?:from|by)\b", tail):
            return f'from:"{safe_fragment}"'
        if re.match(r"(?i)^\s*(?:with\s+)?subject\b", tail):
            return f'subject:"{safe_fragment}"'
        return f'"{safe_fragment}"'

    @staticmethod
    def _decode_body_data(value: str) -> str:
        if not value:
            return ""
        try:
            padding = "=" * (-len(value) % 4)
            return base64.urlsafe_b64decode(value + padding).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""

    @classmethod
    def _message_body(cls, payload: dict) -> str:
        plain_parts: list[str] = []
        html_parts: list[str] = []

        def visit(part: dict):
            mime_type = str(part.get("mimeType") or "").casefold()
            data = str((part.get("body") or {}).get("data") or "")
            if data and mime_type in {"text/plain", "text/html"}:
                decoded = cls._decode_body_data(data)
                (plain_parts if mime_type == "text/plain" else html_parts).append(decoded)
            for child in part.get("parts") or []:
                if isinstance(child, dict):
                    visit(child)

        visit(payload if isinstance(payload, dict) else {})
        body = "\n".join(plain_parts).strip()
        if not body and html_parts:
            body = re.sub(r"<[^>]+>", " ", "\n".join(html_parts))
            body = re.sub(r"\s+", " ", body).strip()
        return body[:8000]

    @classmethod
    def _message_images(cls, payload: dict, message_id: str) -> list[dict]:
        images: list[dict] = []

        def visit(part: dict):
            mime_type = str(part.get("mimeType") or "").split(";", 1)[0].casefold()
            body = part.get("body") or {}
            part_id = str(part.get("partId") or "")
            if mime_type.startswith("image/") and part_id and (body.get("attachmentId") or body.get("data")):
                filename = str(part.get("filename") or "").strip()
                if not filename:
                    filename = f"email-image-{len(images) + 1}{mimetypes.guess_extension(mime_type) or ''}"
                images.append({
                    "message_id": message_id,
                    "part_id": part_id,
                    "filename": filename[:180],
                    "mime_type": mime_type,
                    "size": int(body.get("size") or 0),
                })
            for child in part.get("parts") or []:
                if isinstance(child, dict):
                    visit(child)

        visit(payload if isinstance(payload, dict) else {})
        return images

    @staticmethod
    def _find_part(payload: dict, part_id: str) -> dict | None:
        if str(payload.get("partId") or "") == part_id:
            return payload
        for child in payload.get("parts") or []:
            if isinstance(child, dict):
                found = GmailService._find_part(child, part_id)
                if found is not None:
                    return found
        return None

    def image_part(self, email: str, message_id: str, part_id: str) -> tuple[bytes, str, str]:
        """Return one authenticated Gmail image part without exposing OAuth material."""
        response = self._authorized_get(
            email,
            f"{self.API_URL}/messages/{message_id}",
            params={"format": "full"},
            timeout=30.0,
        )
        if response.status_code != 200:
            self._raise_response_failure(email, response, stage="image_metadata")
        part = self._find_part(response.json().get("payload") or {}, part_id)
        mime_type = str((part or {}).get("mimeType") or "").split(";", 1)[0].casefold()
        if part is None or not mime_type.startswith("image/"):
            raise LookupError("Gmail image part is unavailable.")

        body = part.get("body") or {}
        encoded = str(body.get("data") or "")
        attachment_id = str(body.get("attachmentId") or "")
        if not encoded and attachment_id:
            attachment_response = self._authorized_get(
                email,
                f"{self.API_URL}/messages/{message_id}/attachments/{attachment_id}",
                timeout=30.0,
            )
            if attachment_response.status_code != 200:
                self._raise_response_failure(email, attachment_response, stage="image_attachment")
            encoded = str(attachment_response.json().get("data") or "")
        if not encoded:
            raise LookupError("Gmail image data is unavailable.")
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded + padding)
        if not raw:
            raise LookupError("Gmail image data is empty.")

        filename = os.path.basename(str(part.get("filename") or "").strip())
        filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", filename)
        extension = mimetypes.guess_extension(mime_type) or ""
        if not filename:
            filename = f"email-image{extension}"
        elif extension and not os.path.splitext(filename)[1]:
            filename += extension
        return raw, mime_type, filename[:180]

    def search(
        self,
        email: str,
        request_text: str,
        max_results: int = 10,
        *,
        structured_query: str | None = None,
    ) -> dict:
        started = time.monotonic()
        try:
            latest_requested = is_latest_email_request(request_text)
            latest_filter = self._latest_filter_query(request_text) if latest_requested else ""
            latest_only = latest_requested and not latest_filter
            latest_matching = latest_requested and bool(latest_filter)
            parsed_query = self.safe_structured_query(structured_query or "")
            query = latest_filter if latest_matching else (
                "" if latest_only else (parsed_query or self._search_query(request_text))
            )
            request_limit = 1 if latest_requested else max(1, min(max_results, 10))
            list_params: dict[str, Any] = {"maxResults": request_limit}
            if latest_only:
                # Dedicated newest-inbox route: do not turn the user's output
                # formatting words into Gmail search terms.
                list_params["labelIds"] = "INBOX"
            elif query:
                list_params["q"] = query
            response = self._authorized_get(
                email,
                f"{self.API_URL}/messages",
                params=list_params,
                timeout=self.API_TIMEOUT,
            )
            if response.status_code != 200:
                self._raise_response_failure(email, response, stage="message_list")
            message_refs = (response.json().get("messages") or [])[:request_limit]
            messages = []
            include_body = bool(re.search(
                r"(?i)\b(read|open|full|body|content|entire|whole|summari[sz]e)\b",
                request_text,
            ))

            def load_message(reference: dict) -> tuple[str, dict] | None:
                message_id = str(reference.get("id") or "")
                if not message_id:
                    return None
                detail = self._authorized_get(
                    email,
                    f"{self.API_URL}/messages/{message_id}",
                    params={
                        # Full payload is needed to discover image/inline attachment
                        # parts. Body text is still included in model context only
                        # when the user's request explicitly asks to read it.
                        "format": "full",
                        "metadataHeaders": ["From", "Subject", "Date"],
                    },
                    timeout=self.API_TIMEOUT,
                )
                if detail.status_code != 200:
                    self._raise_response_failure(email, detail, stage="message_detail")
                return message_id, detail.json()

            if len(message_refs) > 1:
                with ThreadPoolExecutor(max_workers=min(5, len(message_refs))) as executor:
                    loaded_messages = list(executor.map(load_message, message_refs))
            else:
                loaded_messages = [load_message(reference) for reference in message_refs]

            for loaded in loaded_messages:
                if loaded is None:
                    continue
                message_id, payload = loaded
                header_rows = (payload.get("payload") or {}).get("headers") or []
                message_headers = {
                    str(row.get("name") or "").casefold(): str(row.get("value") or "")
                    for row in header_rows
                }
                message = {
                    "id": message_id,
                    "thread_id": payload.get("threadId"),
                    "from": message_headers.get("from", ""),
                    "subject": message_headers.get("subject", "(no subject)"),
                    "date": message_headers.get("date", ""),
                    "internal_date": int(payload.get("internalDate") or 0),
                    "labels": [str(label) for label in payload.get("labelIds") or []],
                    "snippet": str(payload.get("snippet") or "")[:500],
                    "gmail_url": (
                        f"https://mail.google.com/mail/u/?authuser={quote(email, safe='')}"
                        f"#all/{payload.get('threadId') or message_id}"
                    ),
                    "images": self._message_images(payload.get("payload") or {}, message_id),
                }
                if include_body:
                    message["body"] = self._message_body(payload.get("payload") or {})
                messages.append(message)
            messages.sort(key=lambda item: item.get("internal_date", 0), reverse=True)
            messages = messages[:1] if latest_requested else messages[:max(1, min(max_results, 10))]
            result = {
                "authorization_state": "AUTHORIZED",
                "connection_state": "CONNECTED" if messages else "CONNECTED_ZERO_RESULTS",
                "request_mode": (
                    "latest_inbox" if latest_only else
                    "latest_matching" if latest_matching else
                    "search"
                ),
                "query": query,
                "count": len(messages),
                "messages": messages,
            }
            self._record_health(email, started=started, success=True)
            return result
        except (GmailNotConnected, GmailProviderFailure) as failure:
            self._record_health(email, started=started, success=False, failure=failure)
            raise
        except GmailOAuthNotConfigured:
            failure = GmailProviderFailure(
                "Gmail OAuth is not configured.",
                category="CONFIGURATION_REQUIRED",
                reason_code="OAUTH_CLIENT_MISSING",
            )
            self._record_health(email, started=started, success=False, failure=failure)
            raise
        except Exception as error:
            failure = GmailProviderFailure(
                "Gmail request failed safely.",
                category="UNKNOWN_ERROR",
                reason_code=type(error).__name__.upper()[:80],
            )
            self._record_health(email, started=started, success=False, failure=failure)
            raise failure from None


_bootstrap_image_cooldowns: dict[tuple[str, str], float] = {}
_bootstrap_image_lock = Lock()


@dataclass(frozen=True)
class CompiledImagePrompt:
    text: str
    refinement_model: str = "deterministic"
    constraints: dict[str, Any] = field(default_factory=dict)
    quality_mode: str = "basic"


class ImagePromptCompiler:
    """One bounded semantic refinement before the existing image specialist."""

    _LEADING_ACTION = re.compile(
        r"(?ix)^\s*(?:please\s+)?(?:create|generate|make|draw|design|illustrate|paint|render|produce)\s+"
        r"(?:me\s+)?"
    )
    _CONCEPT_GUIDES = {
        "lehenga": {
            "required": [
                "traditional Indian lehenga choli",
                "fitted choli/blouse fully visible",
                "skirt waistband visibly separate from the blouse",
                "separate wide flared lehenga skirt",
                "coordinated dupatta loose on the arms, behind the shoulders, or at the sides",
                "unobstructed blouse-to-skirt separation and lehenga silhouette",
            ],
            "negative": [
                "sari", "sari pallu", "diagonal fabric across the torso",
                "sari pleats", "fabric wrapped or tucked around the waist like a sari",
            ],
        },
        "sari": {
            "required": [
                "one continuous sari drape", "recognizable sari pleats", "visible pallu",
            ],
            "negative": ["separate lehenga skirt ensemble"],
        },
        "kimono": {
            "required": ["T-shaped wrapped robe", "straight sleeves", "obi belt"],
            "negative": ["generic wrap dress"],
        },
        "hanbok": {
            "required": ["jeogori jacket", "full chima skirt or appropriate baji trousers"],
            "negative": ["generic gown"],
        },
    }
    _EXACT_COUNTS = {
        "one": 1, "single": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }
    _HUMAN_SINGULAR = re.compile(r"(?i)\b(woman|man|person|girl|boy|child)\b")
    _HUMAN_PLURAL = re.compile(r"(?i)\b(women|men|people|girls|boys|children)\b")

    @classmethod
    def _requested_concepts(cls, prompt: str) -> list[str]:
        source = str(prompt or "").casefold()
        words = set(re.findall(r"[a-z]+", source))
        concepts: list[str] = []
        for concept in cls._CONCEPT_GUIDES:
            if re.search(
                rf"\b(?:not|no|without|exclude|excluding|avoid)\s+(?:an?\s+)?{re.escape(concept)}\b",
                source,
            ) or re.search(
                rf"\b(?:looks?|looked|appears?|appeared|mistaken)\s+(?:too\s+)?(?:like|as)\s+(?:an?\s+)?{re.escape(concept)}\b",
                source,
            ):
                continue
            if concept in words:
                concepts.append(concept)
                continue
            # Generic typo recovery for the small visual-concept vocabulary;
            # semantic refinement remains authoritative for ordinary wording.
            if get_close_matches(concept, words, n=1, cutoff=0.78):
                concepts.append(concept)
        return concepts

    @classmethod
    def _count_constraint(cls, prompt: str) -> str:
        source = str(prompt or "")
        exact = re.search(
            r"(?i)\b(single|one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})\s+"
            r"(?:adult\s+)?(?:[a-z]+\s+){0,2}(woman|women|man|men|person|people|girl|girls|boy|boys|child|children)\b",
            source,
        )
        if exact:
            token = exact.group(1).casefold()
            count = cls._EXACT_COUNTS.get(token, int(token) if token.isdigit() else 1)
            noun = exact.group(2)
            return f"Show exactly {count} primary {noun}; do not add extra people."
        if cls._HUMAN_SINGULAR.search(source) and not cls._HUMAN_PLURAL.search(source):
            return "Show exactly one primary person; do not add extra people."
        return ""

    @classmethod
    def _structured_constraints(cls, original: str, correction: str = "") -> dict[str, Any]:
        intent = cls._LEADING_ACTION.sub("", original).strip(" \t:;,.\n") or original
        concepts = cls._requested_concepts(intent)
        count = cls._count_constraint(intent)
        subject_match = re.search(
            r"(?i)\b(?:(?:single|one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})\s+)?"
            r"(?:adult\s+)?(?:[a-z]+\s+){0,2}(?:woman|women|man|men|person|people|girl|girls|boy|boys|child|children)\b",
            intent,
        )
        required: list[str] = []
        negative: list[str] = []
        for name in concepts:
            guide = cls._CONCEPT_GUIDES[name]
            required.extend(guide["required"])
            negative.extend(guide["negative"])
        observed_wrong = re.findall(
            r"(?i)\b(?:looks?|looked|appears?|appeared|mistaken)\s+(?:too\s+)?(?:like|as)\s+(?:an?\s+)?([a-z][a-z-]*)",
            correction,
        )
        negative.extend(observed_wrong)
        composition: list[str] = []
        if re.search(r"(?i)\b(full[- ]?(?:body|length|outfit)|head[- ]to[- ]toe|entire outfit)\b", intent):
            composition.append("full-length head-to-toe framing; do not crop the requested outfit")
        style_match = re.search(
            r"(?i)\b(realistic(?: photograph| photo)?|photorealistic|cinematic|watercolou?r|oil painting|anime|cartoon|3d render)\b",
            intent,
        )
        precision = bool(
            concepts or count or composition or correction
            or re.search(r"(?i)\b(exactly|specific|must|not|without|separate|distinct|only)\b", intent)
        )
        return {
            "subject": subject_match.group(0) if subject_match else intent,
            "count": count or "preserve the exact singular/plural count from the request",
            "primary_concept": ", ".join(concepts) or "preserve the exact named primary object, garment, profession, or architecture",
            "required_features": list(dict.fromkeys(required)) or [
                "preserve every explicitly requested defining feature"
            ],
            "composition": composition or ["preserve the requested composition and framing"],
            "style": style_match.group(0) if style_match else "preserve the requested style without inventing one",
            "negative_constraints": list(dict.fromkeys(negative)) or [
                "do not substitute a nearby but different concept"
            ],
            "original_intent": intent,
            "correction": str(correction or "").strip(),
            "quality_mode": "precision" if precision else "basic",
        }

    @classmethod
    def _merge_constraints(
        cls, current: dict[str, Any], inherited: dict[str, Any] | None
    ) -> dict[str, Any]:
        if not inherited:
            return current
        merged = dict(current)
        for key in ("required_features", "composition", "negative_constraints"):
            merged[key] = list(dict.fromkeys([
                *(inherited.get(key) or []), *(current.get(key) or [])
            ]))
        for key in ("subject", "count", "primary_concept", "style", "original_intent"):
            if inherited.get(key):
                merged[key] = inherited[key]
        merged["quality_mode"] = "precision"
        return merged

    @classmethod
    def _finalize(
        cls,
        refined: str,
        original: str,
        *,
        correction: str = "",
        inherited_constraints: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any], str]:
        provider_prompt = str(refined or cls._fallback(original)).strip().rstrip(".")
        constraints = cls._merge_constraints(
            cls._structured_constraints(original, correction), inherited_constraints
        )
        lines = [
            f"SUBJECT: {constraints['subject']}",
            f"COUNT: {constraints['count']}",
            f"PRIMARY OBJECT/GARMENT: {constraints['primary_concept']}",
            f"REQUIRED FEATURES: {'; '.join(constraints['required_features'])}",
            f"COMPOSITION: {'; '.join(constraints['composition'])}",
            f"STYLE: {constraints['style']}",
            f"MUST NOT: {'; '.join(constraints['negative_constraints'])}",
            f"AUTHORITATIVE USER INTENT: {constraints['original_intent']}",
        ]
        if constraints.get("correction"):
            lines.append(f"REVISION CORRECTION: {constraints['correction']}")
        lines.append(f"REFINED DESCRIPTION: {provider_prompt}")
        return "\n".join(lines), constraints, constraints["quality_mode"]

    @classmethod
    def _fallback(cls, prompt: str) -> str:
        intent = cls._LEADING_ACTION.sub("", prompt).strip(" \t:;,.\n") or prompt
        return (
            f"Create an image whose primary subject and exact intent are: {intent}. "
            "Preserve every explicit subject count, age, identity or cultural context, clothing or object type, "
            "setting, pose, style, color, composition, framing, mood, and exclusion from the request. "
            "Make the requested defining features clearly recognizable and visually central. "
            "Do not substitute a nearby but different concept, change singular to plural or plural to singular, "
            "or add unrelated demographic or appearance assumptions."
        )

    @classmethod
    def compile(
        cls,
        prompt: str,
        *,
        correction: str = "",
        inherited_constraints: dict[str, Any] | None = None,
    ) -> CompiledImagePrompt:
        original = " ".join(str(prompt or "").split())[:1800]
        correction = " ".join(str(correction or "").split())[:900]
        fallback = cls._fallback(original)
        if not original:
            text, constraints, quality_mode = cls._finalize(
                fallback,
                original,
                correction=correction,
                inherited_constraints=inherited_constraints,
            )
            return CompiledImagePrompt(text, constraints=constraints, quality_mode=quality_mode)
        refinement_request = original
        if correction:
            refinement_request = (
                f"Original image intent: {original}\n"
                f"Requested revision: {correction}"
            )
        try:
            from brain.llm import ask_llm
            from core.model_manager import Task

            response = ask_llm(
                Task.IMAGE_PROMPT,
                [
                    {
                        "role": "system",
                        "content": (
                            "Compile the user's image request into one concise provider-ready generation prompt. "
                            "Preserve the requested subject and singular/plural count, age when stated, clothing/object type, "
                            "setting, pose, style, colors, composition, camera/framing, mood, cultural context, negative constraints, "
                            "and explicit exclusions. Correct a small spelling or wording mistake only when the intended common concept "
                            "is semantically clear. Make defining features visually explicit. Add a narrowly relevant exclusion only when "
                            "it prevents a commonly confused adjacent concept. Do not invent a setting, color, pose, camera, mood, style, "
                            "or appearance attribute that the user did not request; use only minimal neutral wording needed for visibility. "
                            "Never add demographic assumptions, change intent, or answer "
                            "the user. Return only the compiled prompt, with no label, Markdown, JSON, or commentary. Maximum 900 characters."
                        ),
                    },
                    {"role": "user", "content": refinement_request},
                ],
                temperature=0.2,
            )
            refined = str(response or "").strip()
            refined = re.sub(r"^```(?:text)?\s*|\s*```$", "", refined, flags=re.IGNORECASE).strip()
            refined = re.sub(r"(?i)^compiled\s+(?:image\s+)?prompt\s*:\s*", "", refined).strip()
            if 24 <= len(refined) <= 1200:
                text, constraints, quality_mode = cls._finalize(
                    refined,
                    original,
                    correction=correction,
                    inherited_constraints=inherited_constraints,
                )
                return CompiledImagePrompt(
                    text,
                    str(getattr(response, "model", "") or "routed-lightweight-model"),
                    constraints,
                    quality_mode,
                )
        except Exception as error:
            Logger.warning(
                f"Image prompt refinement category={type(error).__name__} fallback=deterministic"
            )
        text, constraints, quality_mode = cls._finalize(
            fallback,
            original,
            correction=correction,
            inherited_constraints=inherited_constraints,
        )
        return CompiledImagePrompt(text, constraints=constraints, quality_mode=quality_mode)

    @classmethod
    def compile_revision(
        cls,
        original_prompt: str,
        correction: str,
        inherited_constraints: dict[str, Any] | None = None,
    ) -> CompiledImagePrompt:
        """Compile a correction without discarding the source image's intent."""

        return cls.compile(
            original_prompt,
            correction=correction,
            inherited_constraints=inherited_constraints,
        )


class ImageGenerationService:
    _PRECISION_MODEL_ORDER = (
        # The current bounded adherence comparison selected SDXL Base: it was
        # the highest-quality route that preserved one subject, separate
        # garment geometry, full framing, and avoided a sari-like torso wrap.
        "@cf/stabilityai/stable-diffusion-xl-base-1.0",
        "@cf/lykon/dreamshaper-8-lcm",
        "@cf/leonardo/lucid-origin",
        "@cf/leonardo/phoenix-1.0",
        "@cf/bytedance/stable-diffusion-xl-lightning",
        "@cf/black-forest-labs/flux-1-schnell",
    )
    _BASIC_MODEL_ORDER = (
        "@cf/black-forest-labs/flux-1-schnell",
        "@cf/bytedance/stable-diffusion-xl-lightning",
    )

    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()
        self.specialist = CloudflareImageSpecialist()

    @staticmethod
    def _bootstrap_candidates(quality_mode: str = "basic") -> list[ProviderModel]:
        # Historical bootstrap evidence is not enough to prove today's free
        # quota. Only the current atomic registry can activate a specialist.
        from services.local_model_registry import LocalModelRegistry
        from config.model_audit import evidence_for

        ranked = []
        now = time.time()
        from services.api_health_center import api_health_center

        for row in LocalModelRegistry().load().get("models", []):
            if row.get("provider") != "cloudflare":
                continue
            model_id = str(row.get("model_id") or "")
            confidence = str(row.get("free_status_confidence") or "").casefold()
            discovery = row.get("discovery") or {}
            if (row.get("status") not in {"active", "backup"}
                    or row.get("output_valid") is not True
                    or not any(marker in confidence for marker in ("official-free", "supplied-free", "positive-free"))
                    or any(marker in confidence for marker in ("unknown", "unconfirmed", "paid", "zero"))
                    or discovery.get("available") is False or discovery.get("catalog_free_status") == "paid"):
                continue
            capability_exists = str(row.get("capability") or "").casefold() in {
                "text-to-image", "text_to_image", "image_generation"
            } or "image_generation" in set(row.get("roles") or [])
            current_verified = (
                row.get("status") == "active"
                and "image_generation" in set(row.get("roles") or [])
                and row.get("output_valid") is True
            )
            previous_verified_health = api_health_center().has_recent_success(
                "cloudflare", model_id, TaskType.IMAGE_GENERATION.value
            )
            audit = evidence_for("cloudflare", model_id) or {}
            audited_verified = bool(
                audit.get("result") == "PASS"
                and audit.get("free_verified") is True
                and str(audit.get("capability") or "").casefold() == "text-to-image"
            )
            if not capability_exists or not (
                current_verified or previous_verified_health or audited_verified
            ):
                continue
            with _bootstrap_image_lock:
                if _bootstrap_image_cooldowns.get(("cloudflare", model_id), 0) > now:
                    continue
            ranked.append(ProviderModel(
                provider="cloudflare",
                model_id=model_id,
                display_name=str(row.get("requested_model_id") or model_id),
                credential_scope=CredentialScope.JUPITER_SHARED_CLOUDFLARE,
                available=True,
                free_eligible=True,
                free_verified=True,
                free_plan_available=True,
                reliability_class=ReliabilityClass.VERIFIED_HEALTHY,
                routing_allowed=True,
                input_modalities=frozenset({"text"}),
                output_modalities=frozenset({"image"}),
                capabilities=frozenset({"image_generation"}),
                status="ACTIVE",
                metadata={
                    "source": (
                        "current_verified_local_registry" if current_verified else
                        "recent_live_health_evidence" if previous_verified_health else
                        "verified_bootstrap_audit"
                    ),
                    "model_exists": True,
                    "capability_exists": True,
                    "free_allocation_eligible": True,
                    "free_allocation_evidence": (
                        "current_registry" if current_verified else
                        "runtime_health" if previous_verified_health else
                        "audited_http_200"
                    ),
                    "live_or_previous_health_verified": bool(
                        current_verified or previous_verified_health or audited_verified
                    ),
                    "route_currently_healthy": True,
                },
            ))
        preferred = (
            ImageGenerationService._PRECISION_MODEL_ORDER
            if quality_mode == "precision"
            else ImageGenerationService._BASIC_MODEL_ORDER
        )
        rank = {model_id: index for index, model_id in enumerate(preferred)}
        ranked.sort(key=lambda model: (rank.get(model.model_id, len(rank)), model.model_id))
        return ranked[:6]

    @classmethod
    def eligible_source_models(cls, quality_mode: str = "basic") -> tuple[ProviderModel, ...]:
        """Return source/health-eligible image routes without making inference calls."""

        return tuple(cls._bootstrap_candidates(quality_mode))

    @classmethod
    def _rank_for_quality(
        cls, candidates: list[ProviderModel], quality_mode: str
    ) -> list[ProviderModel]:
        preferred = (
            cls._PRECISION_MODEL_ORDER
            if quality_mode == "precision"
            else cls._BASIC_MODEL_ORDER
        )
        rank = {model_id: index for index, model_id in enumerate(preferred)}
        return sorted(
            candidates,
            key=lambda model: (rank.get(model.model_id, len(rank)), model.model_id),
        )

    def _candidates(
        self, email: str, prompt: str, job_id: str, quality_mode: str = "basic"
    ) -> tuple[list[ProviderModel], bool]:
        request = AIRequest(
            request_id=uuid4().hex,
            user_id=email,
            conversation_id=None,
            job_id=job_id,
            task_type=TaskType.IMAGE_GENERATION,
            messages=[{"role": "user", "content": prompt}],
            required_modalities=frozenset({"text"}),
            required_capabilities=frozenset({"image_generation"}),
            privacy_class=PrivacyClass.NORMAL_USER_DATA,
        )
        if self.store.configured:
            try:
                decision = FreeOnlyRouter(self.store).route(request)
                candidates = [
                    candidate.model
                    for candidate in CredentialRouter().order_candidates(request, decision.candidates)
                ]
                if candidates:
                    return self._rank_for_quality(candidates, quality_mode)[:6], True
            except ArchitectureStoreUnavailable:
                pass
        return self._bootstrap_candidates(quality_mode), False

    def generate(
        self,
        email: str,
        prompt: str,
        job_id: str,
        *,
        quality_mode: str = "basic",
    ) -> tuple[bytes, str, ProviderModel]:
        candidates, _ = self._candidates(email, prompt, job_id, quality_mode)
        if not candidates:
            raise SpecialistUnavailable(
                "No verified free image route is currently available.",
                reason_code="NO_COMPATIBLE_ROUTE",
            )
        attempts: list[dict[str, Any]] = []
        previous_model: ProviderModel | None = None
        previous_reason = ""
        from services.quota_governor import quota_governor
        governor = quota_governor()
        for fallback_number, model in enumerate(candidates, start=1):
            if (
                previous_model is not None
                and previous_model.provider == model.provider
                and previous_model.model_id == model.model_id
                and previous_model.credential_scope != model.credential_scope
                and previous_reason not in {"AUTH_FAILED", "FREE_QUOTA_EXHAUSTED", "RATE_LIMIT"}
            ):
                continue
            if not (
                model.free_verified
                and model.free_eligible
                and model.free_plan_available
                and model.routing_allowed
                and "image_generation" in model.capabilities
            ):
                continue
            admission = governor.reserve(model, max(1, len(prompt) // 4))
            if not admission.allowed:
                attempts.append({
                    "provider": model.provider,
                    "model": model.model_id,
                    "stage": "quota_governor",
                    "category": "FREE_QUOTA_EXHAUSTED",
                    "status": 429,
                    "fallback": fallback_number,
                })
                continue
            try:
                usage = {}
                if model.provider == "cloudflare":
                    raw, mime_type = self.specialist.generate(prompt=prompt, model=model, usage=usage)
                elif model.provider == "google":
                    raw, mime_type = GoogleImageGenerationSpecialist().generate(
                        prompt=prompt,
                        model=model,
                        job_id=job_id,
                    )
                else:
                    continue
                Logger.info(
                    f"Image route job={job_id} provider={model.provider} model={model.model_id} "
                    f"stage=provider_call category=SUCCESS status=200 fallback={fallback_number}"
                )
                try:
                    from services.api_health_center import api_health_center
                    api_health_center().record_attempt(
                        user_id=email, provider=model.provider,
                        requested_model=model.model_id, actual_model=model.model_id,
                        task_type=TaskType.IMAGE_GENERATION.value, latency_ms=None,
                        success=True, status_code=200, error_class=None,
                        fallback_number=fallback_number - 1,
                    )
                except Exception:
                    pass
                governor.success(model, input_tokens=max(1, len(prompt) // 4), admission=admission,
                                 actual_shared_units=usage.get("neurons"))
                return raw, mime_type, model
            except Exception as error:
                reason_code = str(getattr(error, "reason_code", None) or "INTERNAL")
                previous_model = model
                previous_reason = reason_code
                governor.failure(model, reason_code)
                status_code = getattr(error, "status_code", None)
                attempts.append({
                    "provider": model.provider,
                    "model": model.model_id,
                    "stage": "provider_call",
                    "category": reason_code,
                    "status": status_code,
                    "fallback": fallback_number,
                })
                Logger.warning(
                    f"Image route job={job_id} provider={model.provider} model={model.model_id} "
                    f"stage=provider_call category={reason_code} status={status_code or 'none'} "
                    f"fallback={fallback_number} action=continue"
                )
                with _bootstrap_image_lock:
                    _bootstrap_image_cooldowns[(model.provider, model.model_id)] = time.time() + 30
        all_quota = bool(attempts) and all(
            attempt["category"] in {"FREE_QUOTA_EXHAUSTED", "RATE_LIMIT"}
            for attempt in attempts
        )
        raise SpecialistUnavailable(
            "All verified-free image capacity is temporarily exhausted."
            if all_quota
            else "All currently verified free image routes failed for this image job.",
            status_code=429 if all_quota else None,
            reason_code="FREE_QUOTA_EXHAUSTED" if all_quota else "IMAGE_ROUTES_FAILED",
            attempt_diagnostics=attempts,
        )


class GoogleImageGenerationSpecialist:
    """Generate images only for a Google model already verified free by the registry."""

    @staticmethod
    def generate(*, prompt: str, model: ProviderModel, job_id: str) -> tuple[bytes, str]:
        if model.provider != "google" or "image_generation" not in model.capabilities:
            raise ValueError("A Google image-generation model is required.")
        if not (
            model.free_eligible and model.free_verified
            and model.free_plan_available and model.routing_allowed
        ):
            raise PermissionError("Google image route is not currently verified free.")
        from core.request_context import resolve_request_credential
        from google import genai

        if model.credential_scope == CredentialScope.USER_GOOGLE:
            api_key = resolve_request_credential(CredentialScope.USER_GOOGLE.value)
        else:
            policy = managed_google_policy(model.credential_owner_id)
            if policy is None or not policy.allows_model(model.model_id):
                raise SpecialistUnavailable(
                    "This managed Google credential is not eligible for the selected image model.",
                    reason_code="CREDENTIAL_MODEL_DENIED",
                )
            api_key = managed_google_secret(policy.credential_id)
        if not api_key:
            raise SpecialistUnavailable(
                "Google image credentials are unavailable.", reason_code="AUTH_FAILED"
            )
        try:
            response = genai.Client(api_key=api_key).interactions.create(
                model=model.model_id,
                input=[{"type": "text", "text": prompt}],
                response_modalities=["IMAGE"],
                store=False,
                labels={"jupiter_job": hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]},
            )
            raw, mime_type, _ = GeminiImageEditingService._output_bytes(response)
            return raw, mime_type
        except SpecialistUnavailable:
            raise
        except Exception as error:
            message = str(error).casefold()
            reason = (
                "FREE_QUOTA_EXHAUSTED" if "429" in message or "quota" in message
                else "AUTH_FAILED" if "401" in message or "unauthorized" in message or "api key not valid" in message
                else "MODEL_NOT_FOUND" if "404" in message or "not found" in message
                else "UNSUPPORTED" if "unsupported" in message
                else "IMAGE_SAFETY_BLOCKED" if "safety" in message or "blocked" in message
                else "IMAGE_ROUTES_FAILED"
            )
            raise SpecialistUnavailable(
                "The verified-free Google image route could not complete the request.",
                reason_code=reason,
            ) from None


class CloudflareSDXLImageEditingService:
    """True img2img/mask primary; semantic regeneration remains the fallback."""

    MODEL_ID = "@cf/stabilityai/stable-diffusion-xl-base-1.0"

    @classmethod
    def _eligible_model(cls) -> ProviderModel | None:
        return next(
            (model for model in ImageGenerationService._bootstrap_candidates("precision") if model.model_id == cls.MODEL_ID),
            None,
        )

    def edit(self, *, prompt: str, image_bytes: bytes, mask_bytes: bytes | None = None) -> tuple[bytes, str, str]:
        model = self._eligible_model()
        if model is None:
            raise SpecialistUnavailable("Cloudflare SDXL image editing is not eligible.", reason_code="IMAGE_MODEL_UNAVAILABLE")
        from services.quota_governor import quota_governor
        governor = quota_governor()
        admission = governor.reserve(model, max(1, len(prompt) // 4))
        if not admission.allowed:
            raise SpecialistUnavailable("Cloudflare shared image budget is unavailable.", reason_code="FREE_QUOTA_EXHAUSTED")
        usage = {}
        try:
            raw, mime_type = CloudflareImageSpecialist().edit(
                prompt=prompt, image=image_bytes, mask=mask_bytes, model=model, usage=usage,
            )
            governor.success(model, admission=admission, actual_shared_units=usage.get("neurons"))
        except Exception as error:
            governor.failure(model, str(getattr(error, "reason_code", "IMAGE_ROUTES_FAILED")))
            raise
        return raw, mime_type, model.model_id


class GeminiImageEditingService:
    """Free-only Gemini image-to-image route, activated solely by verified registry evidence."""

    @staticmethod
    def _eligible_models() -> list[str]:
        from services.local_model_registry import LocalModelRegistry

        eligible: list[str] = []
        for row in LocalModelRegistry().load().get("models", []):
            roles = set(row.get("roles") or [])
            confidence = str(row.get("free_status_confidence") or "").casefold()
            if not (
                row.get("provider") == "google"
                and row.get("status") == "active"
                and ("image_editing" in roles or row.get("capability") == "image_editing")
                and "positive" in confidence
                and row.get("output_valid") is True
            ):
                continue
            model_id = str(row.get("model_id") or "").strip()
            if model_id and model_id not in eligible:
                eligible.append(model_id)
        return eligible

    @staticmethod
    def _output_bytes(response: Any) -> tuple[bytes, str, str]:
        output = getattr(response, "output_image", None)
        data = getattr(output, "data", None) if output is not None else None
        mime_type = str(getattr(output, "mime_type", None) or "image/png")
        if isinstance(data, bytes):
            raw = data
        elif data:
            try:
                raw = base64.b64decode(str(data), validate=True)
            except (ValueError, TypeError):
                raw = b""
        else:
            raw = b""
        if not raw:
            raise SpecialistUnavailable(
                "The image model returned no image artifact.", reason_code="IMAGE_OUTPUT_MISSING"
            )
        return raw, mime_type, str(getattr(response, "id", None) or "")

    def edit(
        self,
        *,
        api_key: str,
        prompt: str,
        image_bytes: bytes,
        image_mime_type: str,
        job_id: str,
        previous_interaction_id: str | None = None,
        previous_model: str | None = None,
    ) -> tuple[bytes, str, str, str]:
        models = self._eligible_models()
        if not models:
            raise SpecialistUnavailable(
                "No verified free image-editing route is currently available.",
                reason_code="IMAGE_MODEL_UNAVAILABLE",
            )
        if not api_key:
            raise SpecialistUnavailable(
                "A Google API key is required for image editing.", reason_code="AUTH"
            )
        from google import genai

        last_error: Exception | None = None
        for model_id in models:
            continuation_attempts = [True, False] if previous_interaction_id and previous_model == model_id else [False]
            for use_continuation in continuation_attempts:
                try:
                    body: dict[str, Any] = {
                        "model": model_id,
                        "input": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                                "mime_type": image_mime_type,
                            },
                        ],
                        "response_modalities": ["IMAGE"],
                        "store": True,
                        "labels": {"jupiter_job": hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]},
                    }
                    if use_continuation:
                        body["previous_interaction_id"] = previous_interaction_id
                        body["input"] = [{"type": "text", "text": prompt}]
                    response = genai.Client(api_key=api_key).interactions.create(**body)
                    raw, mime_type, interaction_id = self._output_bytes(response)
                    Logger.info(
                        f"Image edit route job={job_id} provider=google model={model_id} category=SUCCESS"
                    )
                    return raw, mime_type, model_id, interaction_id
                except SpecialistUnavailable:
                    if use_continuation:
                        continue
                    raise
                except Exception as error:
                    last_error = error
                    if use_continuation:
                        continue
                    message = str(error).casefold()
                    reason = "FREE_QUOTA_EXHAUSTED" if "429" in message or "quota" in message else (
                        "IMAGE_SAFETY_BLOCKED" if "safety" in message or "blocked" in message else "IMAGE_ROUTES_FAILED"
                    )
                    Logger.warning(
                        f"Image edit route job={job_id} provider=google model={model_id} category={reason}"
                    )
        raise SpecialistUnavailable(
            "The verified free image-editing route could not complete this request.",
            reason_code=(
                "FREE_QUOTA_EXHAUSTED"
                if last_error and ("429" in str(last_error) or "quota" in str(last_error).casefold())
                else "IMAGE_ROUTES_FAILED"
            ),
        )


def gmail_tool_context(result: dict) -> str:
    """Bounded JSON for the model; contains no OAuth material."""

    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))[:12000]
