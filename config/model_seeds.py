"""Routing preferences applied only to models returned by provider catalogs.

These rules are deliberately not a model inventory.  They never create a
callable model ID and never prove availability, capability, or free access.
"""

from __future__ import annotations

from typing import Any


MODEL_FAMILY_PREFERENCES: tuple[dict[str, Any], ...] = (
    {
        "provider": "google",
        "all_terms": ("3.5", "flash", "lite"),
        "family": "google-3.5-flash-lite",
        "roles": ("lightweight", "chat", "summarization", "memory"),
        "weight": 30,
    },
    {
        "provider": "google",
        "all_terms": ("3.6", "flash"),
        "family": "google-3.6-flash",
        "roles": ("chat", "reasoning", "coding", "vision", "pdf"),
        "weight": 35,
    },
    {
        "provider": "google",
        "all_terms": ("3.1", "live"),
        "family": "google-3.1-live",
        "roles": ("live",),
        "weight": 35,
    },
    {
        "provider": "google",
        "all_terms": ("3.1", "tts"),
        "family": "google-3.1-tts",
        "roles": ("tts",),
        "weight": 35,
    },
    {
        "provider": "google",
        "all_terms": ("embedding", "2"),
        "family": "google-embedding-2",
        "roles": ("embeddings",),
        "weight": 35,
    },
    {
        "provider": "groq",
        "exact_ids": ("openai/gpt-oss-20b",),
        "family": "groq-lightweight",
        "roles": ("lightweight", "chat", "structured"),
        "weight": 25,
    },
    {
        "provider": "groq",
        "exact_ids": ("openai/gpt-oss-120b", "qwen/qwen3.6-27b"),
        "family": "groq-complex",
        "roles": ("reasoning", "reviewing"),
        "weight": 25,
    },
    {
        "provider": "groq",
        "exact_ids": ("groq/compound-mini",),
        "family": "groq-compound-mini",
        "roles": ("lightweight", "research"),
        "weight": 20,
    },
    {
        "provider": "groq",
        "exact_ids": ("groq/compound",),
        "family": "groq-compound",
        "roles": ("research", "reasoning"),
        "weight": 20,
    },
    {
        "provider": "groq",
        "exact_ids": ("whisper-large-v3-turbo",),
        "family": "groq-whisper-turbo",
        "roles": ("audio_transcription",),
        "weight": 30,
    },
    {
        "provider": "groq",
        "exact_ids": ("whisper-large-v3",),
        "family": "groq-whisper-v3",
        "roles": ("audio_difficult", "audio_transcription"),
        "weight": 25,
    },
    {
        "provider": "nvidia",
        "exact_ids": ("z-ai/glm-5.2",),
        "family": "nvidia-glm",
        "roles": ("reasoning", "coding", "reviewing"),
        "weight": 30,
    },
    {
        "provider": "nvidia",
        "exact_ids": ("nvidia/nemotron-3-ultra-550b-a55b",),
        "family": "nvidia-nemotron-ultra",
        "roles": ("reasoning", "reviewing"),
        "weight": 28,
    },
    {
        "provider": "nvidia",
        "exact_ids": ("nvidia/nemotron-3.5-lightning-30b-a3b",),
        "family": "nvidia-nemotron-lightning",
        "roles": ("fast_coding", "coding", "lightweight"),
        "weight": 25,
    },
    {
        "provider": "nvidia",
        "exact_ids": ("deepseek-ai/deepseek-v4-flash-0731",),
        "family": "nvidia-deepseek-flash",
        "roles": ("coding",),
        "weight": 23,
    },
    {
        "provider": "opencode",
        "exact_ids": (
            "big-pickle",
            "deepseek-v4-flash-free",
            "hy3-free",
            "mimo-v2.5-free",
            "laguna-s-2.1-free",
            "ling-3.0-tiny-free",
            "nemotron-3-ultra-free",
            "nemotron-3.5-lightning-free",
        ),
        "family": "opencode-preferred-free",
        "roles": ("chat",),
        "weight": 20,
    },
    {
        "provider": "cloudflare",
        "exact_ids": ("@cf/openai/gpt-oss-20b", "@cf/zai-org/glm-4.7-flash"),
        "family": "cloudflare-fast-text",
        "roles": ("lightweight", "chat", "fast_coding"),
        "weight": 18,
    },
    {
        "provider": "cloudflare",
        "exact_ids": (
            "@cf/openai/gpt-oss-120b",
            "@cf/nvidia/nemotron-3-120b-a12b",
            "@cf/qwen/qwen3-30b-a3b-fp8",
        ),
        "family": "cloudflare-strong-text",
        "roles": ("reasoning", "reviewing", "coding"),
        "weight": 18,
    },
    {
        "provider": "cloudflare",
        "exact_ids": ("@cf/qwen/qwen2.5-coder-32b-instruct",),
        "family": "cloudflare-coding",
        "roles": ("coding", "fast_coding"),
        "weight": 20,
    },
    {
        "provider": "cloudflare",
        "exact_ids": (
            "@cf/black-forest-labs/flux-1-schnell",
            "@cf/bytedance/stable-diffusion-xl-lightning",
            "@cf/lykon/dreamshaper-8-lcm",
        ),
        "family": "cloudflare-image-generation",
        "roles": ("image_generation",),
        "weight": 22,
    },
    {
        "provider": "openrouter",
        "exact_ids": (
            "poolside/laguna-s-2.1:free",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "nvidia/nemotron-nano-12b-v2-vl:free",
            "nvidia/nemotron-3.5-lightning:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "nvidia/nemotron-3-nano-30b-a3b:free",
        ),
        "family": "openrouter-specific-free",
        "roles": ("chat", "coding", "lightweight", "reasoning"),
        "weight": 15,
    },
    {
        "provider": "openrouter",
        "exact_ids": ("openrouter/free",),
        "family": "openrouter-free-router",
        "roles": ("fallback",),
        "weight": 10,
    },
)


def preference_metadata(
    provider: str,
    model_id: str,
    display_name: str = "",
) -> dict[str, Any]:
    """Return non-authoritative preferences for an already discovered model."""

    searchable = f"{model_id} {display_name}".casefold()
    roles: set[str] = set()
    weight = 0
    family: str | None = None
    for rule in MODEL_FAMILY_PREFERENCES:
        if rule["provider"] != provider:
            continue
        exact_ids = {str(value).casefold() for value in rule.get("exact_ids", ())}
        terms = tuple(str(value).casefold() for value in rule.get("all_terms", ()))
        matches = model_id.casefold() in exact_ids if exact_ids else bool(terms) and all(
            term in searchable for term in terms
        )
        if not matches:
            continue
        roles.update(str(role) for role in rule.get("roles", ()))
        rule_weight = int(rule.get("weight", 0))
        if rule_weight >= weight:
            weight = rule_weight
            family = str(rule.get("family") or "") or None
    return {
        "preference_roles": sorted(roles),
        "preference_weight": weight,
        "family": family,
    }
