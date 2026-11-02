"""Provider-neutral requests, results, failures, and model capabilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Iterator


class CredentialScope(StrEnum):
    USER_GOOGLE = "USER_GOOGLE"
    USER_GROQ = "USER_GROQ"
    USER_NVIDIA = "USER_NVIDIA"
    USER_CLOUDFLARE = "USER_CLOUDFLARE"
    USER_OPENCODE = "USER_OPENCODE"
    USER_OPENROUTER = "USER_OPENROUTER"
    USER_BYTEZ = "USER_BYTEZ"
    USER_OPENWEATHER = "USER_OPENWEATHER"
    JUPITER_SHARED_GOOGLE = "JUPITER_SHARED_GOOGLE"
    JUPITER_SHARED_GROQ = "JUPITER_SHARED_GROQ"
    JUPITER_SHARED_NVIDIA = "JUPITER_SHARED_NVIDIA"
    JUPITER_SHARED_CLOUDFLARE = "JUPITER_SHARED_CLOUDFLARE"
    JUPITER_SHARED_OPENCODE = "JUPITER_SHARED_OPENCODE"
    JUPITER_SHARED_OPENROUTER = "JUPITER_SHARED_OPENROUTER"
    JUPITER_SHARED_BYTEZ = "JUPITER_SHARED_BYTEZ"
    JUPITER_FACEPLUSPLUS = "JUPITER_FACEPLUSPLUS"
    JUPITER_OPENWEATHER = "JUPITER_OPENWEATHER"
    JUPITER_GOOGLE_CLOUD_VISION = "JUPITER_GOOGLE_CLOUD_VISION"


class TaskType(StrEnum):
    INTENT_CLASSIFICATION = "INTENT_CLASSIFICATION"
    CAPABILITY_CLASSIFICATION = "CAPABILITY_CLASSIFICATION"
    CASUAL_CHAT = "CASUAL_CHAT"
    COMPLEX_CHAT = "COMPLEX_CHAT"
    HARD_REASONING = "HARD_REASONING"
    MATH = "MATH"
    CODING = "CODING"
    FAST_CODING = "FAST_CODING"
    HUGE_CONTEXT = "HUGE_CONTEXT"
    FINAL_SYNTHESIS = "FINAL_SYNTHESIS"
    MEMORY_CANDIDATE_DETECTION = "MEMORY_CANDIDATE_DETECTION"
    MEMORY_TIER_CLASSIFICATION = "MEMORY_TIER_CLASSIFICATION"
    MEMORY_FACT_EXTRACTION = "MEMORY_FACT_EXTRACTION"
    MEMORY_RELATIONSHIP_EXTRACTION = "MEMORY_RELATIONSHIP_EXTRACTION"
    MEMORY_DUPLICATE_CONSOLIDATION = "MEMORY_DUPLICATE_CONSOLIDATION"
    MEMORY_CONTRADICTION = "MEMORY_CONTRADICTION"
    MEMORY_RERANKING = "MEMORY_RERANKING"
    MEMORY_P0_JUDGE = "MEMORY_P0_JUDGE"
    MEMORY_EXTRACTION = "MEMORY_EXTRACTION"
    MEMORY_CURATOR = "MEMORY_CURATOR"
    PERSONALITY_EXTRACTION = "PERSONALITY_EXTRACTION"
    TOPIC_DETECTION = "TOPIC_DETECTION"
    SUMMARIZATION = "SUMMARIZATION"
    STRUCTURED_EXTRACTION = "STRUCTURED_EXTRACTION"
    VISION_GENERAL = "VISION_GENERAL"
    VISION_UI = "VISION_UI"
    OCR = "OCR"
    PDF_SIMPLE = "PDF_SIMPLE"
    PDF_COMPLEX = "PDF_COMPLEX"
    PDF_EXTRACTION = "PDF_EXTRACTION"
    VIDEO_UNDERSTANDING = "VIDEO_UNDERSTANDING"
    AUDIO_TRANSCRIPTION = "AUDIO_TRANSCRIPTION"
    AUDIO_DIFFICULT = "AUDIO_DIFFICULT"
    LIVE_CONVERSATION = "LIVE_CONVERSATION"
    TTS = "TTS"
    IMAGE_GENERATION = "IMAGE_GENERATION"
    EMBEDDING = "EMBEDDING"
    WEB_RESEARCH = "WEB_RESEARCH"
    QUICK_CURRENT_INFO = "QUICK_CURRENT_INFO"
    SHOPPING_RESEARCH = "SHOPPING_RESEARCH"
    TRANSLATION = "TRANSLATION"
    EMAIL_READING = "EMAIL_READING"
    EMAIL_WRITING = "EMAIL_WRITING"
    WEATHER = "WEATHER"
    PLANNING = "PLANNING"
    REVIEWING = "REVIEWING"


class PrivacyClass(StrEnum):
    PUBLIC = "PUBLIC"
    NORMAL_USER_DATA = "NORMAL_USER_DATA"
    PRIVATE = "PRIVATE"
    SENSITIVE = "SENSITIVE"
    BIOMETRIC = "BIOMETRIC"
    SECRET = "SECRET"


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH_FAILED = "AUTH_FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class ReliabilityClass(StrEnum):
    VERIFIED_HEALTHY = "VERIFIED_HEALTHY"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    PROBATIONARY_LAST_RESORT = "PROBATIONARY_LAST_RESORT"
    HARD_BLOCKED = "HARD_BLOCKED"
    REMOVED_OR_INCOMPATIBLE = "REMOVED_OR_INCOMPATIBLE"
    CANDIDATE = "CANDIDATE"


class FailureClass(StrEnum):
    BAD_REQUEST = "BAD_REQUEST"
    AUTHENTICATION = "AUTHENTICATION"
    PERMISSION = "PERMISSION"
    MODEL_REMOVED = "MODEL_REMOVED"
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    SERVER = "SERVER"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Attachment:
    file_id: str
    mime_type: str
    content: bytes | None = None
    text: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class AIRequest:
    request_id: str
    user_id: str
    conversation_id: str | None
    job_id: str | None
    task_type: TaskType
    messages: list[dict[str, Any]]
    system_context: str = ""
    personality_brief: str = ""
    memory_capsule: str = ""
    attachments: tuple[Attachment, ...] = ()
    required_modalities: frozenset[str] = frozenset({"text"})
    required_capabilities: frozenset[str] = frozenset()
    max_output: int | None = None
    structured_output_schema: dict[str, Any] | None = None
    tools: tuple[dict[str, Any], ...] = ()
    stream: bool = False
    privacy_class: PrivacyClass = PrivacyClass.NORMAL_USER_DATA
    no_persist: bool = False
    estimated_input_tokens: int | None = None
    idempotency_key: str | None = None
    # Optional work is explicitly marked by its caller.  Keeping interactive
    # as the default preserves the existing foreground routing behaviour.
    execution_priority: str = "interactive"


@dataclass(frozen=True)
class AIResponse:
    content: str
    provider: str
    requested_model: str
    actual_model: str
    finish_reason: str | None = None
    input_usage: int | None = None
    output_usage: int | None = None
    audio_usage: int | None = None
    latency_ms: int | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    structured_output: Any = None
    provider_request_id: str | None = None
    fallback_number: int = 0
    shared_quota_units: float | None = None


@dataclass(frozen=True)
class AIStreamEvent:
    text: str = ""
    event_type: str = "delta"
    provider: str = ""
    requested_model: str = ""
    actual_model: str = ""
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True)
class ProviderModel:
    provider: str
    model_id: str
    display_name: str
    credential_scope: CredentialScope
    family: str | None = None
    credential_owner_id: str = "*"
    available: bool = False
    free_eligible: bool = False
    free_verified: bool = False
    free_plan_available: bool = False
    reliability_class: ReliabilityClass = ReliabilityClass.CANDIDATE
    routing_allowed: bool = False
    input_modalities: frozenset[str] = frozenset({"text"})
    output_modalities: frozenset[str] = frozenset({"text"})
    capabilities: frozenset[str] = frozenset()
    context_window: int | None = None
    max_output: int | None = None
    status: str = "CANDIDATE"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedFailure:
    failure_class: FailureClass
    status_code: int | None = None
    retryable: bool = False
    cooldown_seconds: int | None = None
    quota_exhausted: bool = False
    auth_error: bool = False
    unsupported: bool = False
    reason_code: str | None = None
    quota_scope: str | None = None
    safe_message: str = "Provider request failed."


class ProviderFailure(RuntimeError):
    def __init__(self, failure: NormalizedFailure):
        super().__init__(failure.safe_message)
        self.failure = failure


class AIProvider(ABC):
    name: str
    credential_scope: CredentialScope

    @abstractmethod
    def list_models(self) -> list[ProviderModel]:
        """Fetch current catalog metadata when an explicit discovery job runs."""

    @abstractmethod
    def generate(self, request: AIRequest, model: ProviderModel) -> AIResponse:
        pass

    @abstractmethod
    def stream(self, request: AIRequest, model: ProviderModel) -> Iterator[AIStreamEvent]:
        pass

    def supports(self, model: ProviderModel, request: AIRequest) -> bool:
        return (
            request.required_modalities.issubset(model.input_modalities)
            and request.required_capabilities.issubset(model.capabilities)
        )

    @abstractmethod
    def normalize_error(self, error: Exception) -> NormalizedFailure:
        pass

    def extract_usage(self, payload: Any) -> dict[str, int] | None:
        return None

    def extract_actual_model(self, payload: Any, requested_model: str) -> str:
        return requested_model
