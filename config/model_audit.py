"""Read-only bootstrap evidence from the completed exhaustive model audit.

The snapshot supplies initial health evidence only.  Live provider discovery,
free-status changes, and later runtime health observations can supersede it.
"""

from __future__ import annotations

import base64
import gzip
import json
from functools import lru_cache
from pathlib import Path
from typing import Any


VERIFIED_HEALTHY = "VERIFIED_HEALTHY"
TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
PROBATIONARY_LAST_RESORT = "PROBATIONARY_LAST_RESORT"
HARD_BLOCKED = "HARD_BLOCKED"
REMOVED_OR_INCOMPATIBLE = "REMOVED_OR_INCOMPATIBLE"
CANDIDATE = "CANDIDATE"

_AUDIT_PATH = Path(__file__).with_name("model_audit_bootstrap.json")
_TEMPORARY_RESULTS = {"RATE_LIMITED", "FAILED_TEMPORARY_UPSTREAM", "TIMEOUT"}


def canonical_model_id(provider: str, model_id: str) -> str:
    normalized = model_id.strip()
    if provider == "google":
        normalized = normalized.removeprefix("models/")
    return normalized


@lru_cache(maxsize=1)
def audit_index() -> dict[tuple[str, str], dict[str, Any]]:
    if not _AUDIT_PATH.is_file():
        return {}
    try:
        payload = json.loads(_AUDIT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if payload.get("schema_version") != 2:
        return {}
    if payload.get("encoding") != "gzip+base64+short-objects-v1":
        return {}
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in fields.items()
    ):
        return {}
    try:
        compressed = base64.b64decode(str(payload.get("data") or ""), validate=True)
        packed_rows = json.loads(gzip.decompress(compressed).decode("utf-8"))
        rows = [{fields[key]: value for key, value in values.items()} for values in packed_rows]
    except (AttributeError, OSError, KeyError, TypeError, ValueError):
        return {}
    if not isinstance(rows, list) or payload.get("model_count") != len(rows):
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "").strip()
        model_id = canonical_model_id(provider, str(row.get("model_id") or ""))
        if provider and model_id:
            result[(provider, model_id)] = row
    return result


def evidence_for(provider: str, model_id: str) -> dict[str, Any] | None:
    return audit_index().get((provider, canonical_model_id(provider, model_id)))


def capability_evidence(
    capability: str,
    failure_reason: str = "",
) -> dict[str, frozenset[str]]:
    """Translate the audit's explicit capability label, never a model name."""

    normalized = capability.strip().casefold()
    if failure_reason == "TTS_PAYLOAD_REQUIRED":
        return {
            "input_modalities": frozenset({"text"}),
            "output_modalities": frozenset({"audio"}),
            "capabilities": frozenset({"tts"}),
        }
    if failure_reason == "NON_TEXT_SPECIALIST_PAYLOAD_REQUIRED":
        # The audit disproved normal text input but did not prove a callable
        # specialist protocol, so retain the model without foreground routing.
        return {
            "input_modalities": frozenset(),
            "output_modalities": frozenset(),
            "capabilities": frozenset(),
        }
    if normalized in {"embedding", "text embeddings"}:
        return {
            "input_modalities": frozenset({"text"}),
            "output_modalities": frozenset({"embedding"}),
            "capabilities": frozenset({"embeddings"}),
        }
    if normalized in {"audio/stt", "automatic speech recognition"}:
        return {
            "input_modalities": frozenset({"audio"}),
            "output_modalities": frozenset({"text"}),
            "capabilities": frozenset({"audio_transcription"}),
        }
    if normalized == "text-to-image":
        return {
            "input_modalities": frozenset({"text"}),
            "output_modalities": frozenset({"image"}),
            "capabilities": frozenset({"image_generation"}),
        }
    if normalized == "text-to-speech":
        return {
            "input_modalities": frozenset({"text"}),
            "output_modalities": frozenset({"audio"}),
            "capabilities": frozenset({"tts"}),
        }
    if normalized in {"image-to-text", "image classification"}:
        return {
            "input_modalities": frozenset({"image"}),
            "output_modalities": frozenset({"text"}),
            "capabilities": frozenset({"vision"}),
        }
    if normalized == "text classification":
        return {
            "input_modalities": frozenset({"text"}),
            "output_modalities": frozenset({"text"}),
            "capabilities": frozenset({"classification"}),
        }
    if normalized == "translation":
        return {
            "input_modalities": frozenset({"text"}),
            "output_modalities": frozenset({"text"}),
            "capabilities": frozenset({"translation"}),
        }
    if normalized in {"text/chat", "text generation"}:
        return {
            "input_modalities": frozenset({"text"}),
            "output_modalities": frozenset({"text"}),
            "capabilities": frozenset(),
        }
    return {
        "input_modalities": frozenset(),
        "output_modalities": frozenset(),
        "capabilities": frozenset(),
    }


def classify_evidence(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {
            "reliability_class": CANDIDATE,
            "status": "CANDIDATE",
            "routing_allowed": False,
            "initial_health": "DEGRADED",
            "cooldown_seconds": None,
            "block_reason": "AWAITING_HEALTH_EVIDENCE",
        }
    result = str(row.get("result") or "")
    reason = str(row.get("failure_reason") or "")
    if result == "PASS":
        return {
            "reliability_class": VERIFIED_HEALTHY,
            "status": "ACTIVE",
            "routing_allowed": True,
            "initial_health": "HEALTHY",
            "cooldown_seconds": None,
            "block_reason": None,
        }
    if result in _TEMPORARY_RESULTS:
        cooldown = 300 if result == "RATE_LIMITED" else 180 if result == "TIMEOUT" else 60
        return {
            "reliability_class": TEMPORARY_FAILURE,
            "status": "COOLDOWN",
            "routing_allowed": True,
            "initial_health": "RATE_LIMITED" if result == "RATE_LIMITED" else "DEGRADED",
            "cooldown_seconds": cooldown,
            "block_reason": reason,
        }
    if result == "FAILED_VALID_REQUEST" and reason in {
        "SPECIALIZED_PAYLOAD_REQUIRED",
        "IMAGE_PAYLOAD_REQUIRED",
        "TTS_PAYLOAD_REQUIRED",
        "NON_TEXT_SPECIALIST_PAYLOAD_REQUIRED",
        "TEST_PAYLOAD_TOO_SHORT",
    }:
        return {
            "reliability_class": PROBATIONARY_LAST_RESORT,
            "status": "CANDIDATE",
            # Eligible only through the matching specialist execution path.
            "routing_allowed": True,
            "initial_health": "DEGRADED",
            "cooldown_seconds": None,
            "block_reason": "SPECIALIZED_ADAPTER_REQUIRED",
        }
    if result == "UNSUPPORTED_CAPABILITY":
        return {
            "reliability_class": PROBATIONARY_LAST_RESORT,
            "status": "CANDIDATE",
            "routing_allowed": True,
            "initial_health": "DEGRADED",
            "cooldown_seconds": None,
            "block_reason": "CAPABILITY_OR_ADAPTER_CONFIRMATION_REQUIRED",
        }
    if result in {"PAID_PLAN_ONLY", "TERMS_REQUIRED", "AUTH_FAILURE"}:
        return {
            "reliability_class": HARD_BLOCKED,
            "status": "INCOMPATIBLE",
            "routing_allowed": False,
            "initial_health": "UNAVAILABLE",
            "cooldown_seconds": None,
            "block_reason": reason,
        }
    return {
        "reliability_class": REMOVED_OR_INCOMPATIBLE,
        "status": "INCOMPATIBLE",
        "routing_allowed": False,
        "initial_health": "UNAVAILABLE",
        "cooldown_seconds": None,
        "block_reason": reason or result or "INCOMPATIBLE",
    }
