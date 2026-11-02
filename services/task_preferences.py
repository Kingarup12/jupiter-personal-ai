"""Non-authoritative workload preferences for already eligible models.

This file never creates model inventory and never proves availability, free
allocation, capability, or health.  Routers apply it only after those gates.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.provider_contracts import TaskType


@dataclass(frozen=True)
class ModelPreference:
    provider: str | None
    terms: tuple[str, ...]
    label: str
    excluded_terms: tuple[str, ...] = ()

    def matches(self, provider: str, model_id: str, display_name: str = "") -> bool:
        if self.provider and provider.casefold() != self.provider:
            return False
        searchable = f"{model_id} {display_name}".casefold()
        return (
            all(term.casefold() in searchable for term in self.terms)
            and not any(term.casefold() in searchable for term in self.excluded_terms)
        )


def _p(
    provider: str | None,
    label: str,
    *terms: str,
    exclude: tuple[str, ...] = (),
) -> ModelPreference:
    return ModelPreference(provider, tuple(terms), label, tuple(exclude))


FAST_LIGHTWEIGHT = (
    _p("groq", "Groq GPT-OSS 20B", "gpt-oss", "20b", exclude=("120b",)),
    _p("nvidia", "NVIDIA Nemotron 3.5 Lightning", "nemotron", "3.5", "lightning"),
    _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
    _p("opencode", "OpenCode Nemotron Lightning Free", "nemotron-3.5-lightning-free"),
    _p("opencode", "MiMo V2.5 Free", "mimo", "v2.5"),
)


TASK_MODEL_PREFERENCES: dict[TaskType, tuple[ModelPreference, ...]] = {
    TaskType.INTENT_CLASSIFICATION: FAST_LIGHTWEIGHT,
    TaskType.CAPABILITY_CLASSIFICATION: FAST_LIGHTWEIGHT,
    TaskType.MEMORY_CANDIDATE_DETECTION: FAST_LIGHTWEIGHT,
    TaskType.CASUAL_CHAT: (
        _p("groq", "Groq GPT-OSS 20B", "gpt-oss", "20b", exclude=("120b",)),
        _p("nvidia", "NVIDIA Nemotron 3.5 Lightning", "nemotron", "3.5", "lightning"),
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.1 Flash-Lite", "3.1", "flash", "lite"),
        _p("opencode", "OpenCode Nemotron Lightning Free", "nemotron-3.5-lightning-free"),
        _p("opencode", "MiMo V2.5 Free", "mimo", "v2.5"),
        _p("openrouter", "Specific OpenRouter free model", ":free"),
    ),
    TaskType.COMPLEX_CHAT: (
        _p("google", "Gemini 3.6 Flash", "3.6", "flash"),
        _p("google", "Verified Gemini 3 Flash", "gemini-3-flash"),
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("groq", "Groq GPT-OSS 120B", "gpt-oss", "120b"),
        _p("nvidia", "NVIDIA Nemotron 3 Ultra", "nemotron", "3", "ultra"),
        _p("nvidia", "NVIDIA Nemotron 3 Super", "nemotron", "3", "super"),
        _p("cloudflare", "Cloudflare GLM Flash", "glm-4.7-flash"),
        _p("opencode", "OpenCode Nemotron Ultra", "nemotron-3-ultra-free"),
        _p("nvidia", "NVIDIA Nemotron 3.5 Lightning", "nemotron", "3.5", "lightning"),
        _p("groq", "Groq GPT-OSS 20B", "gpt-oss", "20b", exclude=("120b",)),
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("openrouter", "OpenRouter free router last resort", "openrouter/free"),
        _p("openrouter", "Specific OpenRouter free model", ":free"),
    ),
    TaskType.HARD_REASONING: (
        _p("groq", "Groq GPT-OSS 120B", "gpt-oss", "120b"),
        _p("nvidia", "NVIDIA Nemotron 3 Ultra", "nemotron", "3", "ultra"),
        _p("nvidia", "NVIDIA Nemotron 3 Super", "nemotron", "3", "super"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
        _p("google", "Verified Gemini 3 Flash", "gemini-3-flash"),
        _p("cloudflare", "Cloudflare GLM Flash", "glm-4.7-flash"),
        _p("opencode", "OpenCode Nemotron Ultra", "nemotron-3-ultra-free"),
        _p("google", "Verified-free Gemini Pro", "pro"),
        _p("openrouter", "OpenRouter free router last resort", "openrouter/free"),
    ),
    TaskType.MATH: (
        _p(None, "GPT-OSS 120B", "gpt-oss", "120b"),
        _p("nvidia", "NVIDIA Nemotron Super", "nemotron", "super"),
        _p("google", "Verified Gemini 3 Flash", "gemini-3-flash"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
        _p(None, "Nemotron 3 Ultra Free", "nemotron", "3", "ultra"),
    ),
    TaskType.CODING: (
        _p("groq", "Groq GPT-OSS 120B", "gpt-oss", "120b"),
        _p("nvidia", "NVIDIA Qwen 3 Coder", "qwen", "coder"),
        _p("nvidia", "NVIDIA Nemotron Super", "nemotron", "super"),
        _p("cloudflare", "Cloudflare Qwen Coder", "qwen", "coder"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
        _p("google", "Verified Gemini 3 Flash", "gemini-3-flash"),
        _p(None, "North Mini Code Free", "north", "mini", "code"),
        _p("openrouter", "OpenRouter free router last resort", "openrouter/free"),
    ),
    TaskType.FAST_CODING: (
        _p("groq", "Groq GPT-OSS 20B", "gpt-oss", "20b", exclude=("120b",)),
        _p("nvidia", "NVIDIA GPT-OSS 20B", "gpt-oss", "20b", exclude=("120b",)),
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p(None, "North Mini Code Free", "north", "mini", "code"),
        _p(None, "MiMo V2.5 Free", "mimo", "v2.5"),
    ),
    TaskType.HUGE_CONTEXT: (
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
        _p("google", "Gemini 3.6 Flash", "3.6", "flash"),
        _p("google", "Gemini 3.5 Flash", "3.5", "flash", exclude=("lite",)),
        _p(None, "GPT-OSS 120B", "gpt-oss", "120b"),
    ),
    TaskType.FINAL_SYNTHESIS: (
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
        _p(None, "GPT-OSS 120B", "gpt-oss", "120b"),
        # 17 Oct 2026 routing review: active Google fallbacks so synthesis / reviewing never depend on one model
        _p("google", "Verified Gemini 3 Flash", "gemini-3-flash"),
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
    ),
    TaskType.MEMORY_TIER_CLASSIFICATION: (
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
    ),
    TaskType.MEMORY_FACT_EXTRACTION: (
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
    ),
    TaskType.MEMORY_RELATIONSHIP_EXTRACTION: (
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
    ),
    TaskType.MEMORY_DUPLICATE_CONSOLIDATION: (
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
    ),
    # Memory judgement tasks run as PRIVATE requests, which only Google credentials may serve. They therefore
    # always need registry-served Google TEXT models (never TTS/image/live/embedding). Llama 3.3 70B was retired
    # by Groq on 2026-08-16 and is no longer preferred.
    TaskType.MEMORY_CONTRADICTION: (
        _p("google", "Gemini 3.7 Flash", "3.7", "flash", exclude=("image", "tts", "live")),
        _p("google", "Verified Gemini 3 Flash", "gemini-3-flash", exclude=("image", "tts", "live")),
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite", exclude=("image", "tts", "live")),
        _p(None, "GPT-OSS 120B", "gpt-oss", "120b"),
    ),
    TaskType.MEMORY_RERANKING: (
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
    ),
    TaskType.MEMORY_P0_JUDGE: (
        _p("google", "Gemini 3.7 Flash", "3.7", "flash", exclude=("image", "tts", "live")),
        _p("google", "Verified Gemini 3 Flash", "gemini-3-flash", exclude=("image", "tts", "live")),
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite", exclude=("image", "tts", "live")),
        _p(None, "GPT-OSS 120B", "gpt-oss", "120b"),
    ),
    TaskType.EMAIL_READING: (
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.1 Flash-Lite", "3.1", "flash", "lite"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
    ),
    TaskType.SUMMARIZATION: (
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.1 Flash-Lite", "3.1", "flash", "lite"),
    ),
    TaskType.PDF_EXTRACTION: (
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.1 Flash-Lite", "3.1", "flash", "lite"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
    ),
    TaskType.PDF_SIMPLE: (
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.1 Flash-Lite", "3.1", "flash", "lite"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
    ),
    TaskType.PDF_COMPLEX: (
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
        _p("google", "Verified Gemini 3 Flash extracted-document reasoning", "gemini-3-flash"),
        _p(None, "GPT-OSS 120B", "gpt-oss", "120b"),
    ),
    TaskType.VISION_GENERAL: (
        _p("google", "Verified Gemini 3 Flash vision", "gemini-3-flash"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
        _p("google", "Gemini 3.6 Flash", "3.6", "flash"),
        _p("google", "Gemini 3.5 Flash", "3.5", "flash", exclude=("lite",)),
        _p("cloudflare", "Verified Cloudflare vision", "vision"),
        _p("nvidia", "Verified NVIDIA vision", "vision"),
    ),
    TaskType.VIDEO_UNDERSTANDING: (
        _p("google", "Verified Gemini 3 Flash video", "gemini-3-flash"),
        _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
        _p("google", "Gemini 3.6 Flash", "3.6", "flash"),
        _p("google", "Gemini 3.5 Flash", "3.5", "flash", exclude=("lite",)),
    ),
    TaskType.WEB_RESEARCH: (
        _p("google", "Gemini 3.7 Flash + search", "3.7", "flash"),
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.1 Flash-Lite", "3.1", "flash", "lite"),
        _p("google", "Verified Gemini 3 Flash", "gemini-3-flash"),
    ),
    TaskType.QUICK_CURRENT_INFO: (
        _p("google", "Gemini Flash-Lite + search", "flash", "lite"),
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.1 Flash-Lite", "3.1", "flash", "lite"),
    ),
    TaskType.SHOPPING_RESEARCH: (
        _p("google", "Gemini 3.7 Flash + search", "3.7", "flash"),
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.1 Flash-Lite", "3.1", "flash", "lite"),
    ),
    TaskType.AUDIO_TRANSCRIPTION: (
        _p("groq", "Whisper Large V3 Turbo", "whisper", "large", "v3", "turbo"),
        _p("groq", "Whisper Large V3", "whisper", "large", "v3", exclude=("turbo",)),
        _p("cloudflare", "Verified Cloudflare Whisper", "whisper"),
    ),
    TaskType.TTS: (
        _p("google", "Gemini 3.1 Flash TTS", "3.1", "flash", "tts"),
        _p("google", "Verified-free Gemini Flash TTS", "flash", "tts"),
    ),
    TaskType.LIVE_CONVERSATION: (
        _p("google", "Verified Gemini Flash Live", "flash", "live"),
        _p("google", "Verified Gemini Native Audio", "native", "audio"),
    ),
    TaskType.TRANSLATION: (
        _p("google", "Gemini 3.5 Flash-Lite", "3.5", "flash", "lite"),
        _p("google", "Gemini 3.1 Flash-Lite", "3.1", "flash", "lite"),
    ),
    TaskType.IMAGE_GENERATION: (
        _p("cloudflare", "Cloudflare FLUX.1 Schnell", "flux-1-schnell"),
        _p("cloudflare", "Other verified-free Cloudflare text-to-image"),
        _p("google", "Eligible Google image generation"),
    ),
    TaskType.EMBEDDING: (
        _p("google", "Gemini Embedding 2", "embedding", "2"),
        _p("google", "Gemini Embedding 001", "embedding", "001"),
    ),
}

# Quality preferences never certify a model or credential. All registry/free,
# modality, privacy and scoped-health checks still run before this ranking.
FLAGSHIP = _p("google", "Gemini 3.6 Flash", "3.6", "flash", exclude=("image", "tts", "live"))
WEB_TASKS = frozenset({TaskType.WEB_RESEARCH, TaskType.QUICK_CURRENT_INFO, TaskType.SHOPPING_RESEARCH})
for _task in (
    TaskType.COMPLEX_CHAT, TaskType.HARD_REASONING, TaskType.MATH, TaskType.CODING,
    TaskType.HUGE_CONTEXT, TaskType.FINAL_SYNTHESIS, TaskType.PDF_COMPLEX,
    TaskType.VISION_GENERAL, TaskType.VIDEO_UNDERSTANDING, TaskType.MEMORY_P0_JUDGE,
    TaskType.MEMORY_CONTRADICTION,
):
    TASK_MODEL_PREFERENCES[_task] = (FLAGSHIP, *TASK_MODEL_PREFERENCES[_task])

# Only the Google adapter currently implements and verifies actual retrieval.
# A text model with a 'web' label is not a grounded fallback.
for _task in WEB_TASKS:
    TASK_MODEL_PREFERENCES[_task] = (
        _p("google", "Gemini 3.5 Flash-Lite + Search", "3.5", "flash", "lite"),
        FLAGSHIP,
        _p("google", "Gemini 3.7 Flash + Search", "3.7", "flash"),
    ) if _task == TaskType.QUICK_CURRENT_INFO else (
        FLAGSHIP,
        _p("google", "Gemini 3.7 Flash + Search", "3.7", "flash"),
        _p("google", "Gemini 3.5 Flash-Lite + Search", "3.5", "flash", "lite"),
    )

# Existing Jupiter task names keep using the same workload-specific policies;
# the aliases avoid duplicating model truth across old and new call sites.
TASK_MODEL_PREFERENCES.update({
    TaskType.MEMORY_EXTRACTION: TASK_MODEL_PREFERENCES[TaskType.MEMORY_FACT_EXTRACTION],
    TaskType.MEMORY_CURATOR: TASK_MODEL_PREFERENCES[TaskType.MEMORY_CONTRADICTION],
    # Deterministic parsing is primary. If context is genuinely required,
    # private-route enforcement narrows these non-authoritative preferences to
    # the smallest eligible Google extraction models in the live registry.
    TaskType.PERSONALITY_EXTRACTION: (
        # Text-capable structured extraction only: never TTS / Live / image / embedding / audio models.
        _p("google", "Gemini Flash-Lite personality extraction", "flash", "lite",
           exclude=("tts", "live", "image", "embedding", "audio")),
        _p("google", "Gemini Flash personality extraction", "flash",
           exclude=("pro", "tts", "live", "image", "embedding", "audio")),
    ),
    TaskType.TOPIC_DETECTION: FAST_LIGHTWEIGHT,
    TaskType.STRUCTURED_EXTRACTION: FAST_LIGHTWEIGHT,
    TaskType.PLANNING: TASK_MODEL_PREFERENCES[TaskType.HARD_REASONING],
    TaskType.REVIEWING: TASK_MODEL_PREFERENCES[TaskType.FINAL_SYNTHESIS],
    TaskType.VISION_UI: TASK_MODEL_PREFERENCES[TaskType.VISION_GENERAL],
    TaskType.EMAIL_WRITING: TASK_MODEL_PREFERENCES[TaskType.EMAIL_READING],
    TaskType.AUDIO_DIFFICULT: (
        _p("groq", "Whisper Large V3", "whisper", "large", "v3", exclude=("turbo",)),
        *TASK_MODEL_PREFERENCES[TaskType.AUDIO_TRANSCRIPTION],
    ),
    # Explicit specialist contracts: no generic chat fallback.
    TaskType.OCR: (_p("google_cloud_vision", "Cloud Vision OCR specialist"),),
    TaskType.WEATHER: (_p("openweather", "OpenWeather deterministic specialist"),),
    TaskType.LIVE_CONVERSATION: (
        _p("google", "Gemini 3.8 Live audited native transport", "gemini-3.8-live", exclude=("extended",)),
        *TASK_MODEL_PREFERENCES[TaskType.LIVE_CONVERSATION],
    ),
})


# ── Free-provider chains (user-reviewed research, official sources checked 2026-10-07) ─────────────────────────────
# Candidate ORDER only, placed ahead of the older preferences above, which remain as later fallbacks. A chain never
# makes a model eligible: the registry's verified-free/health/modality gates and config/provider_catalog.json's
# free-only policy run first, so an unaudited model (e.g. gemini-3.8-flash while still a catalog candidate) is simply
# skipped. Chains are hypotheses for evaluation, not measured rankings.
_CHAIN_MODELS = {
    "LIGHT": (_p("google", "Gemini 3.5 Flash-Lite", "gemini-3.5-flash-lite"),
              _p("groq", "Groq GPT-OSS 20B", "openai/gpt-oss-20b"),
              _p("cloudflare", "Cloudflare GLM 4.7 Flash", "@cf/zai-org/glm-4.7-flash")),
    "STRONG": (_p("google", "Gemini 3.8 Flash", "gemini-3.8-flash", exclude=("tts", "live", "image")),
               _p("groq", "Groq GPT-OSS 120B", "openai/gpt-oss-120b"),
               _p("cloudflare", "Cloudflare GLM 4.7 Flash", "@cf/zai-org/glm-4.7-flash")),
    "MULTIMODAL": (_p("google", "Gemini 3.8 Flash", "gemini-3.8-flash", exclude=("tts", "live", "image")),
                   _p("cloudflare", "Cloudflare Gemma 4 26B vision", "@cf/google/gemma-4-26b-a4b-it")),
    "LONG": (_p("google", "Gemini 3.8 Flash", "gemini-3.8-flash", exclude=("tts", "live", "image")),
             _p("google", "Gemini 3.5 Flash-Lite", "gemini-3.5-flash-lite")),
    "PRIVATE_GOOGLE": (_p("google", "Gemini 3.5 Flash-Lite", "gemini-3.5-flash-lite"),
                       _p("google", "Gemini 3.8 Flash", "gemini-3.8-flash", exclude=("tts", "live", "image"))),
    "PDF_LIGHT": (_p("google", "Gemini 3.5 Flash-Lite", "gemini-3.5-flash-lite"),
                  _p("google", "Gemini 3.8 Flash", "gemini-3.8-flash", exclude=("tts", "live", "image"))),
    "VIDEO": (_p("google", "Gemini 3.8 Flash", "gemini-3.8-flash", exclude=("tts", "live", "image")),
              _p("google", "Gemini 3.5 Flash-Lite", "gemini-3.5-flash-lite")),
    # Free Google Search grounding exists only on 2.5 Flash / Flash-Lite (500 RPD shared) on the free API.
    "WEB": (_p("google", "Gemini 2.5 Flash + free Search grounding", "gemini-2.5-flash", exclude=("lite", "image", "tts", "audio")),
            _p("google", "Gemini 2.5 Flash-Lite + free Search grounding", "gemini-2.5-flash-lite")),
}
# 17 Oct 2026 routing review: per-message classification runs on Groq / NVIDIA first, keeping Google's free quota for
# PRIVATE memory and email (which only Google may serve). All three are active, verified-free models.
_CHAIN_MODELS["CLASSIFY"] = (_p("groq", "Groq GPT-OSS 20B", "openai/gpt-oss-20b"),
                             _p("nvidia", "NVIDIA Nemotron 3.5 Lightning", "nvidia/nemotron-3.5-lightning-30b-a3b"),
                             _p("google", "Gemini 3.5 Flash-Lite", "gemini-3.5-flash-lite"),
                             _p("cloudflare", "Cloudflare GLM 4.7 Flash", "@cf/zai-org/glm-4.7-flash"))
# Every TaskType appears exactly once (tests enforce it). SPECIALIST tasks run their own verified execution paths.
TASK_CHAINS: dict[TaskType, str] = {
    **{task: "CLASSIFY" for task in (TaskType.INTENT_CLASSIFICATION, TaskType.CAPABILITY_CLASSIFICATION,
                                     TaskType.TOPIC_DETECTION)},
    **{task: "LIGHT" for task in (TaskType.CASUAL_CHAT, TaskType.FAST_CODING, TaskType.SUMMARIZATION,
                                  TaskType.STRUCTURED_EXTRACTION, TaskType.TRANSLATION)},
    **{task: "STRONG" for task in (TaskType.COMPLEX_CHAT, TaskType.HARD_REASONING, TaskType.MATH, TaskType.CODING,
                                   TaskType.FINAL_SYNTHESIS, TaskType.PLANNING, TaskType.REVIEWING)},
    **{task: "PRIVATE_GOOGLE" for task in (TaskType.MEMORY_CANDIDATE_DETECTION, TaskType.MEMORY_TIER_CLASSIFICATION,
                                           TaskType.MEMORY_FACT_EXTRACTION, TaskType.MEMORY_RELATIONSHIP_EXTRACTION,
                                           TaskType.MEMORY_DUPLICATE_CONSOLIDATION, TaskType.MEMORY_CONTRADICTION,
                                           TaskType.MEMORY_RERANKING, TaskType.MEMORY_P0_JUDGE, TaskType.MEMORY_EXTRACTION,
                                           TaskType.MEMORY_CURATOR, TaskType.PERSONALITY_EXTRACTION,
                                           TaskType.EMAIL_READING, TaskType.EMAIL_WRITING)},
    TaskType.HUGE_CONTEXT: "LONG", TaskType.PDF_COMPLEX: "LONG",
    TaskType.VISION_GENERAL: "MULTIMODAL", TaskType.VISION_UI: "MULTIMODAL",
    TaskType.PDF_SIMPLE: "PDF_LIGHT", TaskType.PDF_EXTRACTION: "PDF_LIGHT",
    TaskType.VIDEO_UNDERSTANDING: "VIDEO",
    TaskType.WEB_RESEARCH: "WEB", TaskType.QUICK_CURRENT_INFO: "WEB", TaskType.SHOPPING_RESEARCH: "WEB",
    # Specialist execution paths (never generic chat fallbacks): existing preferences above stay authoritative.
    TaskType.AUDIO_TRANSCRIPTION: "SPECIALIST", TaskType.AUDIO_DIFFICULT: "SPECIALIST", TaskType.TTS: "SPECIALIST",
    TaskType.LIVE_CONVERSATION: "SPECIALIST", TaskType.IMAGE_GENERATION: "SPECIALIST", TaskType.EMBEDDING: "SPECIALIST",
    TaskType.OCR: "SPECIALIST", TaskType.WEATHER: "SPECIALIST",
}
for _task, _chain in TASK_CHAINS.items():
    if _chain == "SPECIALIST":
        continue
    _existing = TASK_MODEL_PREFERENCES.get(_task, ())
    _labels = {pref.label for pref in _CHAIN_MODELS[_chain]}
    # Web tasks: only free-grounding chain entries (the older 3.x "+ search" labels cannot ground on the free API and
    # are rejected by the registry gate anyway; dropping them keeps the preference truthful).
    _tail = () if _chain == "WEB" else tuple(pref for pref in _existing if pref.label not in _labels)
    TASK_MODEL_PREFERENCES[_task] = (*_CHAIN_MODELS[_chain], *_tail)


def preferences_for(task_type: TaskType, estimated_input_tokens: int | None = None) -> tuple[ModelPreference, ...]:
    if task_type in {TaskType.EMAIL_READING, TaskType.EMAIL_WRITING} and (estimated_input_tokens or 0) >= 6000:
        return (
            FLAGSHIP,
            _p("google", "Gemini 3.7 Flash", "3.7", "flash"),
            _p("google", "Verified Gemini 3 Flash", "gemini-3-flash"),
            _p("google", "Gemini 3.5 Flash", "3.5", "flash", exclude=("lite",)),
        )
    return TASK_MODEL_PREFERENCES.get(task_type, ())


def workload_allowed(task_type: TaskType, provider: str, model_id: str) -> bool:
    """Workload isolation, never evidence of model availability or free access."""
    name = model_id.casefold()
    if task_type in WEB_TASKS and provider != "google":
        return False
    if "compound" in name:
        return False                    # groq/compound + compound-mini retired by Groq on 2026-09-21
    if "allam" in name:
        return False                    # not a Groq production model (docs 2026-09-26); 7B, 0/4 recent successes
    # OpenCode Zen trial models: NVIDIA's trial terms say "do not submit personal or confidential data" and the
    # MiMo / Muse Spark / Ling trials may use prompts for training (opencode.ai/docs/zen, 2026-09-26). They are
    # never given user conversation or memory content; coding tasks may still use them as late fallbacks.
    if provider == "opencode" and any(term in name for term in ("nemotron", "mimo", "muse", "ling")) and task_type not in {
            TaskType.CODING, TaskType.FAST_CODING}:
        return False
    # Guard models are not general classifiers, even if an old audit assigned
    # an intent role. Native/speech/embedding models never enter text routing.
    if any(term in name for term in ("prompt-guard", "llama-guard", "safeguard")):
        return False
    if task_type in {TaskType.EMAIL_READING, TaskType.EMAIL_WRITING} and provider != "google":
        return False
    light = {TaskType.CASUAL_CHAT, TaskType.INTENT_CLASSIFICATION, TaskType.CAPABILITY_CLASSIFICATION,
             TaskType.MEMORY_CANDIDATE_DETECTION, TaskType.TOPIC_DETECTION, TaskType.FAST_CODING}
    if task_type in light and any(term in name for term in ("120b", "ultra", "gemini-3.8-flash")):
        return False
    if name == "gemini-3.8-flash" and task_type in {
        TaskType.LIVE_CONVERSATION, TaskType.AUDIO_TRANSCRIPTION, TaskType.AUDIO_DIFFICULT,
        TaskType.TTS, TaskType.IMAGE_GENERATION, TaskType.EMBEDDING,
    }:
        return False
    return True


def preference_rank(
    task_type: TaskType,
    provider: str,
    model_id: str,
    display_name: str = "",
    estimated_input_tokens: int | None = None,
) -> tuple[int | None, str | None]:
    for index, preference in enumerate(preferences_for(task_type, estimated_input_tokens)):
        if preference.matches(provider, model_id, display_name):
            return index, preference.label
    return None, None
