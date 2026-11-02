"""Registry-gated Google Live route selection and session policy metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from datetime import datetime, timezone

from services.local_model_registry import LocalModelRegistry
from config.voice_settings import DEFAULT_JUPITER_TTS_VOICE, DEFAULT_JUPITER_VOICE
from config.credential_policy import managed_google_policies
from core.provider_contracts import CredentialScope
from core.request_context import current_request_context, resolve_request_credential


@dataclass(frozen=True)
class LiveRoute:
    model_id: str
    role: str
    active: bool
    registry_status: str
    reason: str
    discovery_access: dict = field(default_factory=dict, repr=False)


class LiveVoiceRouter:
    """Select only audited Live models; never infer eligibility from a name."""

    # Verified 2026-09-26 against ai.google.dev: gemini-3.8-live is the STABLE Live model (audio+video+text in,
    # audio out, input/output transcription, manual activity, <=1 FPS frames, 128k context). The 3.1 Flash Live
    # preview is legacy ("recommend updating to Gemini 3.8 Live") and stays as the first native fallback.
    # gemini-3.8-live-extended-thinking needs the unimplemented interaction_status protocol and adds background
    # reasoning latency, so it is never selected for realtime conversation.
    PRIMARY = "gemini-3.8-live"
    LEGACY_PRIMARY = "gemini-3.1-flash-live-preview"
    CONVERSATION_FALLBACK = "gemini-2.5-flash-native-audio-preview-12-2025"
    TRANSLATION = "gemini-3.5-live-translate-preview"
    NATIVE_PROTOCOLS = frozenset({"gemini-bidi-audio-v1"})

    ROLE_MODELS = {
        "conversation": (PRIMARY, LEGACY_PRIMARY, CONVERSATION_FALLBACK),
        "translation": (TRANSLATION,),
        "language_learning": (PRIMARY, LEGACY_PRIMARY, CONVERSATION_FALLBACK),
    }

    @classmethod
    def is_native_live_row(cls, row: dict[str, Any]) -> bool:
        """A native Live route needs an audited bidi audio contract. Text/TTS models (generateContent only,
        e.g. gemini-3.5-flash-lite) can never qualify, whatever their name or registry priority."""
        contract = row.get("live_transport") or {}
        methods = (row.get("discovery") or {}).get("supported_generation_methods") or []
        return bool(
            row.get("provider") == "google"
            and contract.get("verified") is True
            and contract.get("protocol") in cls.NATIVE_PROTOCOLS
            and not contract.get("blocked_reason")
            and contract.get("audio_input") is True and contract.get("audio_output") is True
            and (not methods or "bidiGenerateContent" in methods)
        )

    def __init__(self, registry: LocalModelRegistry | None = None):
        self.registry = registry or LocalModelRegistry()

    def _row(self, model_id: str) -> dict[str, Any]:
        return next((
            row for row in self.registry.load().get("models", [])
            if row.get("provider") == "google" and row.get("model_id") == model_id
        ), {})

    @staticmethod
    def _credential_available(model_id: str) -> bool:
        context = current_request_context()
        return bool(
            resolve_request_credential(CredentialScope.USER_GOOGLE.value)
            or managed_google_policies(
                model_id,
                "live",
                include_jupiter_user_google=bool(context.managed_google_credentials),
            )
        )

    @classmethod
    def _eligible(cls, row: dict[str, Any], model_id: str) -> bool:
        contract = row.get("live_transport") or {}
        discovery = row.get("discovery") or {}
        if (row.get("disabled_by_policy") or row.get("paid_only")
                or discovery.get("available") is False
                or discovery.get("catalog_free_status") == "paid"
                or str(row.get("status") or "").casefold() in {"deprecated", "disabled", "paid-only", "invalid-model", "removed", "hard-blocked"}):
            return False
        try:
            cooldown = datetime.fromisoformat(str(row.get("circuit_open_until") or "").replace("Z", "+00:00"))
            if cooldown.tzinfo and cooldown > datetime.now(timezone.utc):
                return False
        except ValueError:
            pass
        # An audited protocol contract supplements catalogs that omit audio
        # modalities. Names (including "live") are never capability evidence.
        methods = discovery.get("supported_generation_methods") or []
        if methods and "bidiGenerateContent" not in methods:
            return False
        if discovery.get("modalities_explicit") and (
                "audio" not in discovery.get("input_modalities", [])
                or "audio" not in discovery.get("output_modalities", [])):
            return False
        return bool(
            cls.is_native_live_row(row)
            and contract.get("free_tier") is True
            and cls._credential_available(model_id)
        )

    def routes(self, purpose: str = "conversation") -> tuple[LiveRoute, ...]:
        role = purpose if purpose in self.ROLE_MODELS else "conversation"
        routes = []
        # Refreshable registry is the candidate list, not ROLE_MODELS (legacy
        # identifiers retained for callers). Protocol audit is required per row.
        payload = self.registry.load()
        rows = [row for row in payload.get("models", [])
                if row.get("provider") == "google"
                and role in (row.get("live_transport") or {}).get("purposes", [])]
        canonical_order = {mid: idx for idx, mid in enumerate(self.ROLE_MODELS.get(role, ()))}
        rows.sort(key=lambda row: (
            canonical_order.get(str(row.get("model_id")), 100),
            float((row.get("live_transport") or {}).get("priority", 100)),
            str(row.get("model_id")),
        ))
        for row in rows:
            model_id = str(row.get("model_id") or "")
            eligible = self._eligible(row, model_id)
            registry_verified = bool(row and row.get("status") in {"active", "backup"} and row.get("output_valid") is True)
            reason = (
                "verified-free-live-route" if eligible and registry_verified
                else "audited-native-protocol-candidate" if eligible
                else str(row.get("normalized_error") or "no-eligible-credential-or-live-route")
            )

            routes.append(LiveRoute(
                model_id=model_id,
                role=role,
                active=eligible,
                registry_status=str(row.get("status") or "missing"),
                reason=reason,
                discovery_access=payload.get("discovery_access") or {},
            ))
        return tuple(routes)

    def select(self, purpose: str = "conversation") -> LiveRoute | None:
        return next((route for route in self.routes(purpose) if route.active), None)

    def status(self) -> dict[str, Any]:
        conversation = self.routes("conversation")
        translation = self.routes("translation")
        language_learning = self.routes("language_learning")
        
        selected_conv = next((route for route in conversation if route.active), None)
        selected_trans = next((route for route in translation if route.active), None)
        selected_learn = next((route for route in language_learning if route.active), None)
        
        native_transport_implemented = True
        return {
            "provider_session_available": bool(selected_conv),
            "native_transport_implemented": native_transport_implemented,
            "native_route_eligible": bool(selected_conv),
            "mode": "gemini_live_backend_session" if selected_conv else "processed_stt_routed_chat_natural_tts",
            "generation_mode": "live_native" if selected_conv else "live_degraded",
            "fallback_reason": None if selected_conv else "no_eligible_native_live_route",
            "read_tts_voice": DEFAULT_JUPITER_TTS_VOICE,
            "live_native_voice": DEFAULT_JUPITER_VOICE if selected_conv else None,
            "session_policy": {
                "one_session_per_live_entry": True,         # Persistent Live Session Design
                "official_resumption_required": True,
                "specialist_tool_delegation": True,         # Specialist Delegation logic
                "shared_memory_manager": True,
                "language_memory_capsule": True,            # P0 + Language Capsule only for learning
                "endpoint_policy": "adaptive_foreground_transcript",
                "endpoint_seconds": {"stable_recovery": 6},
                "post_response_ready_seconds": 10,
                "preserve_text_on_tts_failure": True,       # Never rollback assistant text on audio failure
                "barge_in": True,
                "input_audio": "processed_get_user_media_pcm16_16khz",
                "browser_speech_recognition": "emergency_only",
            },
            "modes": {
                "LiveTranslationMode": bool(selected_trans),
                "LanguageLearningMode": bool(selected_learn),
            }
        }
