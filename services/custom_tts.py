"""Persistent, user-scoped custom TTS artifacts and Jupiter voice catalog.

This module is deliberately provider-agnostic at the storage boundary. It uses
Jupiter's existing verified-free TTS service and never reads credentials itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from config.settings import DATA_DIR, USERS_DIR
from config.voice_settings import (
    DEFAULT_JUPITER_TTS_VOICE,
    JUPITER_VOICES,
    JUPITER_VOICE_IDS,
    normalize_jupiter_voice,
    canonical_jupiter_voice,
)
from services.voice_pipeline import VerifiedFreeTTSService


CUSTOM_TTS_VERSION = "custom-tts-v1"
TTS_CAPABILITY = "tts"
TTS_MAX_TEXT_CHARS = 12000
TTS_PROVIDER_CHUNK_CHARS = 6000
VOICE_SAMPLE_TEXT = "Hello! I'm Jupiter. It's really nice to meet you. How can I help you today?"
_AUDIO_LOCK = Lock()


def _compact(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _safe_user_key(user_id: str) -> str:
    return hashlib.sha256(str(user_id or "anonymous").casefold().encode("utf-8")).hexdigest()[:32]


def voice_catalog(aliases: dict[str, str] | None = None) -> list[dict]:
    aliases = aliases or {}
    reverse_aliases: dict[str, list[str]] = {}
    for alias, voice in aliases.items():
        reverse_aliases.setdefault(voice, []).append(alias)
    return [
        {
            "id": item["id"],
            "name": item["label"],
            "gender": item["gender"],
            "description": item["description"],
            "aliases": sorted(reverse_aliases.get(item["id"], []), key=str.casefold),
        }
        for item in JUPITER_VOICES
    ]


class VoiceAliasStore:
    """Store friendly aliases separately for each authenticated Jupiter user."""

    def __init__(self, user_id: str):
        self.path = USERS_DIR / re.sub(r"[^A-Za-z0-9_-]", "_", user_id).strip("._") / "voice_aliases.json"

    def load(self) -> dict[str, str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        clean: dict[str, str] = {}
        for alias, voice in payload.items():
            normalized_alias = _compact(alias, 40)
            canonical = canonical_jupiter_voice(voice)
            if normalized_alias and canonical in JUPITER_VOICE_IDS:
                # Preserve legacy keys until resolution can detect a normalized
                # collision, rather than silently taking the last entry.
                clean[alias] = canonical
        return clean

    def resolve(self, value: object) -> str:
        return resolve_voice_identity(value, self)

    def set(self, alias: str, voice: str) -> dict[str, str]:
        alias = _compact(alias, 40)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{1,39}", alias):
            raise ValueError("Voice aliases must be 2-40 ordinary letters, numbers, spaces, '_' or '-'.")
        if alias.casefold() in {item.casefold() for item in JUPITER_VOICE_IDS}:
            raise ValueError("A voice alias cannot replace a canonical Jupiter voice name.")
        canonical = canonical_jupiter_voice(voice)
        if not canonical:
            raise ValueError("Choose a supported Jupiter voice for this nickname.")
        aliases = self.load()
        normalized_alias = " ".join(alias.casefold().split())
        collisions = {value for key, value in aliases.items() if " ".join(key.casefold().split()) == normalized_alias}
        if collisions and collisions != {canonical}:
            raise ValueError("That voice alias is already assigned to another Jupiter voice.")
        aliases = {key: value for key, value in aliases.items() if " ".join(key.casefold().split()) != normalized_alias}
        aliases[alias] = canonical
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(aliases, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)
        return aliases

    def delete(self, alias: str) -> dict[str, str]:
        aliases = self.load()
        filtered = {key: value for key, value in aliases.items() if key.casefold() != str(alias).casefold()}
        if filtered == aliases:
            return aliases
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(filtered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)
        return filtered


@dataclass(frozen=True)
class TTSIntent:
    kind: str
    text: str = ""
    voice: str = DEFAULT_JUPITER_TTS_VOICE
    segments: tuple[dict, ...] = ()
    voice_issue: str = ""
    speed: float = 1.0
    custom_instructions: str = ""


def resolve_voice_identity(value: object, aliases: VoiceAliasStore) -> str:
    """Canonical name first, then one unique owned alias; never synthesize a preference."""
    canonical = canonical_jupiter_voice(value)
    if canonical:
        return canonical
    candidate = " ".join(str(value or "").casefold().split())
    matches = {voice for alias, voice in aliases.load().items()
               if " ".join(alias.casefold().split()) == candidate and canonical_jupiter_voice(voice)}
    return next(iter(matches)) if candidate and len(matches) == 1 else ""


_EXPLICIT_CATALOG_PATTERNS = (
    r"\bwhat (?:jupiter )?voices are (?:available|supported)\b",
    r"\bshow (?:me )?(?:all |the )?(?:jupiter )?voices\b",
    r"\bshow (?:me )?(?:the )?(?:available|supported) (?:jupiter )?voices\b",
    r"\b(?:list|browse|compare) (?:all |the )?(?:jupiter )?voices\b",
)
_DOWNLOAD_LAST_PATTERN = re.compile(
    r"(?i)\b(?:(?:give|show|surface|provide)\s+(?:me\s+)?(?:the\s+)?download"
    r"(?:\s+(?:option|button|link))?|download\s+(?:it|that|the\s+(?:audio|mp3))"
    r"|(?:give|provide|send)\s+(?:me\s+)?(?:that|the|this)\s+(?:audio|mp3))\b"
)
# These are semantic action families, not one-reproduction phrase checks.  The
# operation may be a short suffix after a long script, so matching is not
# weighted by the dominant topic/language of the supplied content.
_TTS_ACTION = re.compile(
    r"(?is)(?:"
    # English action + output combinations.
    r"\b(?:convert|turn)\s+(?:(?:this|that|it|the\s+following|this\s+text|the\s+text)\s+)?"
    r"(?:in|into|to)\s+(?:an?\s+)?(?:audio\s+file|audio|speech|mp3|voice[- ]?over)\b|"
    r"\b(?:make|create|generate|produce)\s+(?:this|that|it|the\s+following|this\s+text|the\s+text)\s+"
    r"(?:in|into|as)\s+(?:an?\s+)?(?:audio\s+file|audio|speech|mp3|voice[- ]?over)\b|"
    r"\b(?:make|create|generate|produce)\s+(?:an?\s+)?(?:audio\s+file|audio|speech|mp3|voice[- ]?over|voice)"
    r"(?:\s+(?:from|for|of)\s+(?:this|that|it|the\s+following|this\s+text|the\s+text))?\b|"
    r"\bgenerate\s+(?:text[- ]to[- ]speech|tts)(?:\s+for\s+(?:this|that|it|the\s+following))?\b|"
    r"\b(?:read|speak)\s+(?:this|that|it|these\s+lines|the\s+following)\b"
    r".{0,55}\b(?:audio|mp3|speech|voice)\b|"
    r"\b(?:read|speak)\s+(?:this|that|it|these\s+lines|the\s+following)\b|"
    r"\bnarrate\s+(?:this|that|it|the\s+following)\b|"
    r"\b(?:make|create)\s+(?:a\s+)?voice[- ]?over\s+(?:of|from|for)\s+(?:this|that|it|the\s+following)\b|"
    r"\bgive\s+(?:(?:this|that|it|the\s+following)\s+as|me\s+(?:an?\s+)?)\s*mp3\b|"
    r"\b(?:female|human[- ]sounding|most\s+human)\s+voice\s+mp3\b|"
    # Roman-script Hinglish action forms.
    r"\b(?:iska|isko|issko|ye|yah|iss)\b.{0,55}\b(?:audio|mp3|voice(?:[- ]?over)?)\b"
    r".{0,35}\b(?:bana\s*(?:do|dena)?|banao|banado|convert\s*karo|de\s*do|dedo)\b|"
    r"\b(?:audio|mp3|voice[- ]?over)\s+(?:bana\s*do|banao|banado|de\s*do|dedo)\b|"
    # Devanagari Hindi action forms.
    r"(?:इसका|इसे|इसको|यह|ये|इस\s+पाठ|इस\s+टेक्स्ट).{0,60}"
    r"(?:ऑडियो|आडियो|आवाज़|आवाज़|वॉइस\s*ओवर|वॉइसओवर|वॉयसओवर|एमपी\s*3|एमपी३)"
    r".{0,40}(?:बना\s*दो|बनाओ|बदल\s*दो|बदलो|दे\s*दो|करो)|"
    r"(?:ऑडियो|आडियो|वॉइस\s*ओवर|वॉइसओवर|वॉयसओवर|एमपी\s*3|एमपी३)\s*"
    r"(?:बना\s*दो|बनाओ|दे\s*दो)|"
    # Hindi commands often keep the output noun in English ("audio banao").
    r"(?:इसका|इसे|इसको|यह|ये).{0,80}\b(?:audio|mp3|voice[- ]?over)\b"
    r".{0,40}(?:बना\s*दो|बनाओ|दे\s*दो|करो)|"
    # Bengali action forms; mixed Bengali content + English/Hinglish commands
    # is already covered by the language-independent patterns above.
    r"(?:এটা|এটি|এই|একে|এর).{0,60}(?:অডিও|অডিওতে|ভয়েস|ভয়েস|এমপি৩|এমপি\s*3)"
    r".{0,40}(?:বানাও|বানিয়ে\s*দাও|বানিয়ে\s*দাও|রূপান্তর\s*করো|দাও)"
    r")"
)
_ENGLISH_UI_RESPONSE = re.compile(
    r"(?is)(?:^|[.!?\n])\s*(?:please\s+)?provide\s+(?:me\s+)?(?:the\s+)?response\s+in\s+english\s*[.!?]*\s*$"
)

_VOICE_DIRECTED_ACTION = re.compile(
    r"(?is)\b(?:convert|turn|make|create|generate|produce|read|speak|narrate)\b"
    r".{0,100}\b(?:use|using|with|in)\b"
)
_VOICE_ONLY_ACTION = re.compile(
    r"(?is)^\s*(?:please\s+)?(?:use|choose|select|pick|try)\s+.{1,70}?[\s.!?]*$"
)


def _normalized_voice_phrase(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _voice_lookup(aliases: VoiceAliasStore) -> dict[str, set[str]]:
    lookup: dict[str, set[str]] = {}
    for voice in JUPITER_VOICE_IDS:
        lookup.setdefault(_normalized_voice_phrase(voice), set()).add(voice)
    for alias, voice in aliases.load().items():
        if not canonical_jupiter_voice(alias) and canonical_jupiter_voice(voice):
            lookup.setdefault(_normalized_voice_phrase(alias), set()).add(voice)
    return lookup


def _voice_occurrence_pattern(candidate: str) -> str:
    words = [re.escape(word) for word in str(candidate or "").split() if word]
    return r"\s+".join(words)


def _extract_requested_voice(message: str, aliases: VoiceAliasStore) -> tuple[str, str]:
    """Return (status, canonical voice) for an explicitly requested voice.

    A name is considered explicit only beside a voice-selection cue. This keeps
    character names inside the narration from silently changing its voice.
    """

    source = str(message or "")
    lookup = _voice_lookup(aliases)
    matches: set[str] = set()
    ambiguous = False
    for candidate, voices in sorted(lookup.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = _voice_occurrence_pattern(candidate)
        explicit = bool(
            re.search(rf"(?i)\b(?:use|using|with|in|choose|select|pick|try)\s+(?:the\s+)?{pattern}(?:\s+voice)?\b", source)
            or re.search(rf"(?i)\b{pattern}\s+voice\b", source)
        )
        if not explicit:
            continue
        if len(voices) != 1:
            ambiguous = True
        matches.update(voices)
    if ambiguous or len(matches) > 1:
        return "ambiguous", ""
    if len(matches) == 1:
        return "explicit", next(iter(matches))
    if re.search(r"(?i)\b(?:male|female|another|different|deep)\s+voice\b", source):
        return "ambiguous", ""
    if re.search(
        r"(?i)\b(?:use|using|with|in|choose|select|pick|try)\s+(?:the\s+)?"
        r"[A-Za-z][A-Za-z0-9 _-]{1,39}\s+voice\b",
        source,
    ):
        return "unknown", ""
    if re.search(r"(?i)\b(?:use|using|choose|select|pick|try)\s+[A-Za-z][\w-]*(?:\s+for\s+(?:this|that|it))?[.!?]*$", source):
        return "unknown", ""
    return "missing", ""


def _strip_requested_voice_directive(message: str, aliases: VoiceAliasStore) -> str:
    source = str(message or "")
    for candidate in sorted(_voice_lookup(aliases), key=len, reverse=True):
        pattern = _voice_occurrence_pattern(candidate)
        source = re.sub(
            rf"(?i)\b(?:use|using|with|in|choose|select|pick|try)\s+(?:the\s+)?{pattern}(?:\s+voice)?\b",
            " ",
            source,
        )
        source = re.sub(rf"(?i)\b{pattern}\s+voice\b", " ", source)
    source = re.sub(
        r"(?i)\b(?:use|using|with|in|choose|select|pick|try)\s+(?:the\s+)?"
        r"[A-Za-z][A-Za-z0-9 _-]{1,39}\s+voice\b",
        " ",
        source,
    )
    source = re.sub(r"(?i)\b(?:use|using|choose|select|pick|try)\s+[A-Za-z][\w-]*(?:\s+for\s+(?:this|that|it))?[.!?]*$", " ", source)
    return re.sub(r"[ \t]{2,}", " ", source)


def _clean_tts_source(value: str) -> str:
    cleaned = _ENGLISH_UI_RESPONSE.sub("", str(value or "")).strip(" \t\r\n\"'")
    cleaned = re.sub(r"^[,:;\-]+\s*", "", cleaned)
    cleaned = re.sub(r"\s*[:,;\-]+$", "", cleaned)
    return cleaned.strip(" \t\r\n\"'")


def _strip_tts_instruction(message: str) -> str:
    source = str(message or "").strip()
    # Prefer the same canonical action span used by detection. This prevents a
    # shorter legacy prefix (for example ``read this``) from leaving the rest
    # of an operation such as ``and give me the audio`` in spoken content.
    match = _TTS_ACTION.search(source)
    if match:
        before = _clean_tts_source(source[:match.start()])
        after = _clean_tts_source(source[match.end():])
        if match.start() <= 40 and after:
            return after
        if match.end() >= len(source) - 40 and before:
            return before
        if before and after:
            return before if len(before) >= len(after) else after
        return before or after

    patterns = (
        r"(?is)^\s*(?:please\s+)?generate\s+(?:text[- ]to[- ]speech|tts)\s*(?:for|from)?\s*(?:this|the following)?\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?convert\s+(?:this|the following)?\s*text\s+to\s+speech\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?make\s+(?:audio|speech)\s+from\s+(?:this|the following)\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?turn\s+(?:this|the following)\s+into\s+speech\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?(?:read|speak)\s+(?:this|these lines|the following)?\s*(?:for\s+me)?\s*(?:in\s+[A-Za-z][A-Za-z0-9 _-]{1,39}(?:\s+voice)?)?\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?make\s+(?:this|the following)?\s*(?:an?\s+)?(?:(?:female|human[- ]sounding|most human)\s+)?(?:voice\s+)?mp3\s*(?:saying|of)?\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?give\s+(?:this|the following)\s+to\s+me\s+as\s+(?:an?\s+)?mp3\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?give\s+me\s+(?:an?\s+)?mp3\s*(?:saying|of)?\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?give\s+(?:me\s+)?(?:this|the following)?\s+as\s+(?:an?\s+)?mp3\s*(?:(?:female|human[- ]sounding|most human)\s+voice)?\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?give\s+(?:me\s+)?(?:this|the following)?\s*(?:as\s+)?(?:an?\s+)?(?:(?:female|human[- ]sounding|most human)\s+)?(?:voice\s+)?mp3\s*(?:saying|of)?\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?create\s+(?:an?\s+)?(?:(?:female|human[- ]sounding|most human)\s+)?voice[- ]over\s*(?:in\s+[A-Za-z][A-Za-z0-9 _-]{1,39}(?:\s+voice)?)?\s*(?:saying|of)?\s*[:\-]?\s*",
        r"(?is)^\s*(?:please\s+)?(?:female|human[- ]sounding|most human)\s+voice\s+mp3\s*(?:saying|of)?\s*[:\-]?\s*",
    )
    for pattern in patterns:
        match = re.match(pattern, source)
        if match:
            return source[match.end():].strip(" \t\r\n\"'")
    return ""


def _previous_user_script(conversation_messages: list | tuple) -> str:
    """Return prior supplied text without treating an old command as content."""

    for item in reversed(list(conversation_messages or [])[:-1]):
        role = str(getattr(item, "role", "") or (item.get("role") if isinstance(item, dict) else "")).casefold()
        content = str(getattr(item, "content", "") or (item.get("content") if isinstance(item, dict) else "")).strip()
        if role != "user" or not content:
            continue
        if _DOWNLOAD_LAST_PATTERN.search(content) or _TTS_ACTION.search(content):
            continue
        if _ENGLISH_UI_RESPONSE.fullmatch(content):
            continue
        return content
    return ""


def _speaker_segments(text: str, aliases: VoiceAliasStore, default_voice: str) -> tuple[dict, ...]:
    segments: list[dict] = []
    voices = [item["id"] for item in JUPITER_VOICES]
    for line in str(text or "").splitlines():
        match = re.match(r"^\s*([^:\n]{1,40})(?:\s*\(([^)]+)\))?\s*:\s*(.+)$", line)
        if not match:
            continue
        speaker = _compact(match.group(1), 40)
        spoken = _compact(match.group(3), 6000)
        if not spoken:
            continue
        requested = match.group(2) or speaker
        resolved = aliases.resolve(requested)
        if requested.casefold() not in {item.casefold() for item in (*JUPITER_VOICE_IDS, *aliases.load().keys())}:
            resolved = voices[len(segments) % len(voices)] if len(segments) else default_voice
        segments.append({"speaker": speaker, "voice": resolved, "text": spoken})
    return tuple(segments) if len(segments) >= 2 else ()


def detect_tts_intent(
    message: str,
    conversation_messages: list | tuple,
    aliases: VoiceAliasStore,
    preferred_voice: str | None = None,
) -> TTSIntent | None:
    """Detect explicit custom-speech actions before ordinary chat routing."""

    source = str(message or "").strip()
    lowered = " ".join(source.casefold().split())
    if re.fullmatch(r"(?:play|replay) (?:that|it|that audio)(?: again)?[.!?]*|read (?:aloud )?the previous (?:answer|response)[.!?]*", lowered):
        return TTSIntent(kind="download_last", voice=aliases.resolve(preferred_voice))
    if _DOWNLOAD_LAST_PATTERN.search(lowered):
        return TTSIntent(kind="download_last", voice=aliases.resolve(preferred_voice))
    if any(re.search(pattern, lowered) for pattern in _EXPLICIT_CATALOG_PATTERNS):
        return TTSIntent(kind="catalog", voice=aliases.resolve(preferred_voice))
    # Gmail reading requests belong to the Gmail capability, not speech output.
    if re.search(r"\b(?:email|gmail|inbox|message from)\b", lowered) and not re.search(r"\b(?:mp3|voice[- ]over)\b", lowered):
        return None
    voice_status, requested_voice = _extract_requested_voice(source, aliases)
    preferred_canonical_voice = resolve_voice_identity(preferred_voice, aliases)
    explicit_action = bool(
        _TTS_ACTION.search(source)
        or re.search(r"\b(?:give|make|create|generate|convert)\b.{0,80}\bmp3\b", lowered)
        or (voice_status == "explicit" and re.search(r"(?:audio|mp3|ऑडियो|वॉइस|অডিও)", source, re.IGNORECASE)
            and re.search(r"(?:बना|बनाओ|বান|দাও|make|create|convert|generate|read|speak)", source, re.IGNORECASE))
        or (voice_status in {"explicit", "ambiguous", "unknown"} and _VOICE_DIRECTED_ACTION.search(source))
    )
    if not explicit_action and voice_status != "missing" and _VOICE_ONLY_ACTION.search(source):
        if re.search(r"(?i)\bfor\s+(?:this|that|it)\b", source) or voice_status != "explicit":
            explicit_action = True
        else:
            return TTSIntent(kind="voice_selection", voice=requested_voice)
    if re.search(r"\b(?:female|most human|human[- ]sounding) voice(?:s)?\b", lowered) and not explicit_action:
        return TTSIntent(kind="catalog", voice=aliases.resolve(preferred_voice))
    if not explicit_action:
        return None

    without_voice_directive = _strip_requested_voice_directive(source, aliases)
    speed_match = re.search(r"(?i)\bat\s+(0\.[5-9]|1(?:\.[0-5])?)x\b", without_voice_directive)
    speed = float(speed_match.group(1)) if speed_match else 1.0
    if speed_match:
        without_voice_directive = without_voice_directive[:speed_match.start()] + without_voice_directive[speed_match.end():]
    text = _strip_tts_instruction(without_voice_directive)
    if not text or text.casefold() in {"this", "it", "these lines", "the following"}:
        text = _previous_user_script(conversation_messages)
    if not text:
        return TTSIntent(
            kind="needs_text",
            voice=requested_voice if voice_status == "explicit" else preferred_canonical_voice,
            voice_issue=voice_status,
        )
    if len(text) > TTS_MAX_TEXT_CHARS:
        return TTSIntent(
            kind="too_long",
            voice=requested_voice if voice_status == "explicit" else preferred_canonical_voice,
            voice_issue=voice_status,
        )
    if voice_status in {"ambiguous", "unknown"} or (voice_status == "missing" and not preferred_canonical_voice):
        return TTSIntent(kind="needs_voice", text=text, voice="", voice_issue=voice_status, speed=speed)
    selected_voice = requested_voice if voice_status == "explicit" else preferred_canonical_voice
    segments = _speaker_segments(text, aliases, selected_voice)
    return TTSIntent(kind="custom", text=text, voice=selected_voice, segments=segments, speed=speed)


def recover_pending_tts_intent(
    conversation_messages: list | tuple,
    aliases: VoiceAliasStore,
    preferred_voice: str | None = None,
) -> TTSIntent | None:
    """Recover the newest unresolved explicit TTS request in this chat."""

    messages = list(conversation_messages or [])
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        role = str(getattr(item, "role", "") or (item.get("role") if isinstance(item, dict) else "")).casefold()
        content = str(getattr(item, "content", "") or (item.get("content") if isinstance(item, dict) else "")).strip()
        if role != "user" or not content or _DOWNLOAD_LAST_PATTERN.search(content):
            continue
        intent = detect_tts_intent(content, messages[:index + 1], aliases, preferred_voice)
        if intent is not None and intent.kind in {"custom", "needs_text", "needs_voice"}:
            return intent
    return None


def _split_tts_text(text: str) -> tuple[str, ...]:
    """Bound provider chunks without truncating the accepted source text."""

    remaining = str(text or "").strip()
    chunks: list[str] = []
    while len(remaining) > TTS_PROVIDER_CHUNK_CHARS:
        window = remaining[:TTS_PROVIDER_CHUNK_CHARS + 1]
        split_at = max(
            window.rfind(separator)
            for separator in ("\n", "। ", ". ", "! ", "? ", "? ", "; ", ", ")
        )
        if split_at < TTS_PROVIDER_CHUNK_CHARS // 2:
            split_at = window.rfind(" ")
        if split_at < TTS_PROVIDER_CHUNK_CHARS // 2:
            split_at = TTS_PROVIDER_CHUNK_CHARS
        else:
            split_at += 1
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


class _CustomDeliveryTTS(VerifiedFreeTTSService):
    """Custom-artifact delivery only; Live/read transport remains unchanged."""
    def __init__(self, voice, instructions):
        super().__init__(voice)
        self.instructions = _compact(instructions, 240)

    def _speech_prompt(self, speakable, language):
        base = super()._speech_prompt(speakable, language)
        return ("Delivery style (never change the selected voice identity or spoken words): "
                + json.dumps(self.instructions) + "\n" + base)


class CustomTTSArtifactStore:
    def __init__(self, user_id: str):
        self.user_id = str(user_id)
        self.root = DATA_DIR / "speech_cache" / "custom" / _safe_user_key(user_id)

    @staticmethod
    def _artifact_id(segments: list[dict], options: dict | None = None) -> str:
        canonical = json.dumps(
            {"version": CUSTOM_TTS_VERSION, "segments": segments, **({"options": options} if options else {})},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _paths(self, artifact_id: str) -> tuple[Path, Path]:
        if not re.fullmatch(r"[a-f0-9]{64}", str(artifact_id or "")):
            raise FileNotFoundError("Invalid speech artifact")
        return self.root / f"{artifact_id}.mp3", self.root / f"{artifact_id}.json"

    def get(self, artifact_id: str) -> dict | None:
        audio_path, metadata_path = self._paths(artifact_id)
        if not audio_path.is_file() or not metadata_path.is_file():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if metadata.get("artifact_id") != artifact_id:
            return None
        return {**metadata, "path": audio_path}

    def generate(self, text: str, voice: str, segments: list[dict] | None = None, *, speed: float = 1.0, custom_instructions: str = "") -> dict:
        speed = float(speed)
        if not 0.5 <= speed <= 1.5:
            raise ValueError("Choose a speech speed from 0.5x to 1.5x.")
        custom_instructions = _compact(custom_instructions, 240)
        if len(str(text or "")) > TTS_MAX_TEXT_CHARS:
            raise ValueError(f"Custom speech is limited to {TTS_MAX_TEXT_CHARS} characters.")
        source_segments = segments or [
            {"speaker": "", "voice": voice, "text": chunk}
            for chunk in _split_tts_text(text)
        ]
        normalized_segments = []
        for item in source_segments:
            spoken = _compact(item.get("text"), TTS_PROVIDER_CHUNK_CHARS)
            if spoken:
                normalized_segments.append({
                    "speaker": _compact(item.get("speaker"), 40),
                    "voice": normalize_jupiter_voice(item.get("voice") or voice),
                    "text": spoken,
                })
        if not normalized_segments or len(normalized_segments) > 12:
            raise ValueError("Custom speech requires 1-12 non-empty speech segments.")
        options = {"speed": speed, "instructions": custom_instructions} if speed != 1 or custom_instructions else None
        artifact_id = self._artifact_id(normalized_segments, options)
        cached = self.get(artifact_id)
        if cached:
            return {**cached, "cache_status": "HIT"}

        with _AUDIO_LOCK:
            cached = self.get(artifact_id)
            if cached:
                return {**cached, "cache_status": "HIT"}
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("Local MP3 conversion is unavailable.")
            with tempfile.TemporaryDirectory(prefix="jupiter-custom-tts-") as temporary_directory:
                temporary_root = Path(temporary_directory)
                wav_paths = []
                providers = []
                models = []
                durations = []
                for index, segment in enumerate(normalized_segments):
                    service = (_CustomDeliveryTTS(segment["voice"], custom_instructions)
                               if custom_instructions else VerifiedFreeTTSService(segment["voice"]))
                    result = service.synthesize(segment["text"])
                    wav_path = temporary_root / f"segment-{index:02d}.wav"
                    wav_path.write_bytes(result.audio)
                    wav_paths.append(wav_path)
                    providers.append(result.provider)
                    models.append(result.model_id)
                    durations.append(max(0, int(result.duration_ms or 0)))
                output_path = temporary_root / "speech.mp3"
                command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
                for path in wav_paths:
                    command.extend(["-i", str(path)])
                if len(wav_paths) > 1:
                    streams = "".join(f"[{index}:a]" for index in range(len(wav_paths)))
                    command.extend(["-filter_complex", f"{streams}concat=n={len(wav_paths)}:v=0:a=1,atempo={speed}[out]", "-map", "[out]"])
                elif speed != 1:
                    command.extend(["-filter:a", f"atempo={speed}"])
                command.extend(["-b:a", "128k", str(output_path)])
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=180,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode != 0 or not output_path.is_file():
                    raise RuntimeError("Local MP3 conversion failed.")
                self.root.mkdir(parents=True, exist_ok=True)
                final_audio, final_metadata = self._paths(artifact_id)
                temporary_audio = final_audio.with_suffix(".mp3.tmp")
                temporary_audio.write_bytes(output_path.read_bytes())
                temporary_audio.replace(final_audio)
                metadata = {
                    "artifact_id": artifact_id,
                    "filename": f"Jupiter_Voice_{normalized_segments[0]['voice']}.mp3",
                    "mime_type": "audio/mpeg",
                    "voice": normalized_segments[0]["voice"],
                    "speakers": [item["speaker"] for item in normalized_segments if item["speaker"]],
                    "segment_count": len(normalized_segments),
                    "duration_ms": round(sum(durations) / speed) or None,
                    "provider": providers[0] if providers else "google",
                    "model": models[0] if models else "",
                    "version": CUSTOM_TTS_VERSION,
                }
                temporary_metadata = final_metadata.with_suffix(".json.tmp")
                temporary_metadata.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
                temporary_metadata.replace(final_metadata)
        return {**metadata, "path": final_audio, "cache_status": "MISS"}


class VoicePreviewStore:
    root = DATA_DIR / "speech_cache" / "voice_previews"

    @classmethod
    def get_or_generate(cls, voice: str) -> Path:
        canonical = normalize_jupiter_voice(voice)
        path = cls.root / f"{canonical}.mp3"
        if path.is_file():
            return path
        # Previews contain a fixed public sentence and are intentionally shared
        # globally; user speech and credentials are never written here.
        store = CustomTTSArtifactStore("__global_voice_previews__")
        generated = store.generate(VOICE_SAMPLE_TEXT, canonical)
        cls.root.mkdir(parents=True, exist_ok=True)
        with _AUDIO_LOCK:
            if not path.is_file():
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(Path(generated["path"]).read_bytes())
                temporary.replace(path)
        return path
