"""Central, provider-verified Jupiter voice choices.

The names below are Gemini prebuilt voice identifiers already verified by the
one-time comparison-sample workflow.  This module performs configuration only;
importing it never contacts a provider.
"""

from __future__ import annotations

import re


# Keep Gemini Live's established native voice separate from normal/custom TTS.
# Iapetus is a technical synthesis fallback, NOT an implicit user preference.
# Live Conversation continues to use its existing Leda transport voice.
DEFAULT_JUPITER_VOICE = "Leda"
DEFAULT_JUPITER_TTS_VOICE = "Iapetus"

JUPITER_VOICES = (
    {"id": "Iapetus", "label": "Iapetus", "gender": "male", "description": "Clear"},
    {"id": "Algieba", "label": "Algieba", "gender": "male", "description": "Smooth"},
    {"id": "Achird", "label": "Achird", "gender": "male", "description": "Friendly"},
    {"id": "Schedar", "label": "Schedar", "gender": "male", "description": "Even"},
    {"id": "Umbriel", "label": "Umbriel", "gender": "male", "description": "Easy-going"},
    {"id": "Zubenelgenubi", "label": "Zubenelgenubi", "gender": "male", "description": "Casual"},
    {"id": "Charon", "label": "Charon", "gender": "male", "description": "Informative"},
    {"id": "Rasalgethi", "label": "Rasalgethi", "gender": "male", "description": "Informative"},
    {"id": "Sadaltager", "label": "Sadaltager", "gender": "male", "description": "Knowledgeable"},
    {"id": "Orus", "label": "Orus", "gender": "male", "description": "Firm"},
    {"id": "Alnilam", "label": "Alnilam", "gender": "male", "description": "Firm"},
    {"id": "Puck", "label": "Puck", "gender": "male", "description": "Upbeat"},
    {"id": "Sadachbia", "label": "Sadachbia", "gender": "male", "description": "Lively"},
    {"id": "Fenrir", "label": "Fenrir", "gender": "male", "description": "Excitable"},
    {"id": "Enceladus", "label": "Enceladus", "gender": "male", "description": "Breathy"},
    {"id": "Algenib", "label": "Algenib", "gender": "male", "description": "Gravelly"},
    {"id": "Aoede", "label": "Aoede", "gender": "female", "description": "Breezy"},
    {"id": "Leda", "label": "Leda", "gender": "female", "description": "Youthful"},
    {"id": "Kore", "label": "Kore", "gender": "female", "description": "Firm"},
    {"id": "Callirrhoe", "label": "Callirrhoe", "gender": "female", "description": "Easy-going"},
    {"id": "Despina", "label": "Despina", "gender": "female", "description": "Smooth"},
    {"id": "Achernar", "label": "Achernar", "gender": "female", "description": "Soft"},
    {"id": "Vindemiatrix", "label": "Vindemiatrix", "gender": "female", "description": "Gentle"},
    {"id": "Sulafat", "label": "Sulafat", "gender": "female", "description": "Warm"},
)


def _validate_voice_catalog(catalog: tuple[dict, ...]) -> None:
    allowed_genders = {"male", "female"}
    seen_ids: set[str] = set()
    for index, item in enumerate(catalog):
        voice_id = str(item.get("id") or "").strip()
        label = str(item.get("label") or "").strip()
        gender = str(item.get("gender") or "").strip().casefold()
        description = str(item.get("description") or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,39}", voice_id):
            raise ValueError(f"Invalid canonical Jupiter voice ID at index {index}.")
        if voice_id.casefold() in seen_ids:
            raise ValueError(f"Duplicate canonical Jupiter voice ID: {voice_id}")
        if not label:
            raise ValueError(f"Missing display label for Jupiter voice: {voice_id}")
        if gender not in allowed_genders:
            raise ValueError(f"Invalid gender metadata for Jupiter voice: {voice_id}")
        if not description:
            raise ValueError(f"Missing description for Jupiter voice: {voice_id}")
        seen_ids.add(voice_id.casefold())


_validate_voice_catalog(JUPITER_VOICES)
JUPITER_VOICE_IDS = frozenset(item["id"] for item in JUPITER_VOICES)


def normalize_jupiter_voice(value: object) -> str:
    """Return a supported canonical TTS voice, defaulting safely to Iapetus."""

    return canonical_jupiter_voice(value) or DEFAULT_JUPITER_TTS_VOICE


def canonical_jupiter_voice(value: object) -> str:
    """Strict identity lookup; missing/unknown is not a user preference."""
    candidate = str(value or "").strip().casefold()
    return next((voice for voice in JUPITER_VOICE_IDS if voice.casefold() == candidate), "")


# Live Conversation (and calls) speak only in a female voice: Jupiter is female. The user may pick any female voice
# from the catalog; anything else (a male voice, an unknown or missing value) falls back to Leda.
LIVE_VOICE_IDS = tuple(item["id"] for item in JUPITER_VOICES if item["gender"] == "female")


def normalize_live_voice(value: object) -> str:
    candidate = str(value or "").strip().casefold()
    return next((voice for voice in LIVE_VOICE_IDS if voice.casefold() == candidate), DEFAULT_JUPITER_VOICE)
