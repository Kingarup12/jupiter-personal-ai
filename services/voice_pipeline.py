"""Provider-neutral live-voice state, memory sidecar, and pronunciation rules."""

from __future__ import annotations

import base64
import httpx
import io
import math
import os
import re
import sys
import time
import wave
from array import array
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable

from core.logger import Logger
from core.provider_contracts import CredentialScope, ProviderModel
from core.request_context import current_request_context, resolve_request_credential, request_google_quota_group
from config.voice_settings import DEFAULT_JUPITER_TTS_VOICE, normalize_jupiter_voice

# Ceiling for the private session context in the native Live system instruction (persona, identity, personality,
# P0 memory, recent dialogue). Marker 179: the persona alone outgrew the old 16 000-character ceiling, so recent
# dialogue, the personality capsule and P0 memory were silently cut from every Live session and call.
LIVE_SESSION_CONTEXT_MAX_CHARS = 40000
LIVE_CONTINUITY_MAX_CHARS = 8000

LIVE_CURRENT_TURN_GUIDANCE = (
    "Current spoken input and earlier dialogue are separate. Earlier dialogue is reference only, "
    "never a substitute for the current utterance or material to prepend to it. Follow the newest "
    "valid user instruction over earlier user tasks, within the existing safety and permission rules. "
    "Interpret verbatim/repeat requests from the complete newest utterance, not a keyword. "
    "When explicitly asked to repeat current speech exactly, reproduce the requested current words "
    "without answering their content, paraphrasing, commentary, or quoting an older utterance. "
    "A question about the meaning of 'repeat after me' is not an echo instruction. "
    "Do not carry an echo mode into later independent turns without contextual evidence. "
    "Preserve the current speech's language/script; old dialogue language is not a forced locale."
)

# Live/voice-only conversation style (native Live system instruction + degraded Live chat turns). Normal text chat
# never receives it, so its written formatting rules (headings, bullets, emoji, fenced blocks) stay unchanged. Only
# rules the shared persona does not already state are listed here; persona, identity/BOSS addressing, language
# matching and stop/wait handling already live elsewhere and are not repeated.
LIVE_SPOKEN_CONVERSATION_GUIDANCE = (
    "Spoken conversation style (voice only):\n"
    "- Priorities, in order: understand who is speaking and what they mean; follow speaker turns and context; answer "
    "the actual point; be empathetic without automatically agreeing; adapt tone and language to the speaker; stay "
    "concise; clarify only when needed; never repeat yourself or ask what was already answered.\n"
    "- Talk like a warm friend in a real conversation, not a narrator or a textbook, with your energy matched to the "
    "person you are talking to. Never claim to be human.\n"
    "- In Hindi, address the person as “tum”, never “tu”.\n"
    "- Use a brief acknowledgement such as “haan”, “achha”, “hmm” or “got it” only "
    "occasionally and naturally, never on every sentence or every turn.\n"
    "- A simple question usually needs one to four spoken sentences. Go longer only when asked or genuinely "
    "necessary, and then in small chunks: the core idea, then useful details, then an example, with a short "
    "check-in only when it helps.\n"
    "- For a guided task give one manageable step at a time. When the user says “next”, continue from the "
    "previous step instead of restarting.\n"
    "- In casual chat do not turn things into unsolicited advice or a motivational speech. When someone is venting, "
    "listen first; offer advice only when asked, and ask whether they want it at most once. Adapt gently to apparent frustration, confusion or excitement, "
    "but never state an inferred emotion as a fact.\n"
    "- Do not ask the user to repeat something that was clearly captured. Ask one short clarification only when the "
    "audio or meaning is genuinely uncertain; never build a long answer on a guess.\n"
    "- When told to stop talking, be quiet or wait, stop at once: at most “Okay”. You cannot mute the microphone, "
    "pause or end Live, or close the app yourself; never claim you did, and name the button if they ask.\n"
    "- Silence is not a turn: never fill it with invented speech, a monologue or repeated acknowledgements, and "
    "never answer a partial thought while the user is still speaking.\n"
    "- Your words are spoken aloud: no emoji, emoji names, Markdown symbols, bullet characters, headings, tables, "
    "raw URLs or code blocks. Say a link's site name instead of the URL and describe code briefly rather than "
    "reading it. A creator's nickname with an emoji is spoken as the plain word.\n"
    "- Never imply phone calling, WhatsApp messaging or any other capability unless it is actually available in "
    "this session; say plainly when something failed or is unverified.\n"
    "- A short reaction right after YOUR answer (“very good”, “correct”, “wow”, “nice”) is feedback on what you said: "
    "thank them briefly. Never congratulate them as if they had answered.\n"
    "- Several people may share the microphone (a partner, a friend, a call). The audio is one mixed stream with no "
    "speaker labels, so follow the several-people rules: work out who is speaking from context, answer that person "
    "directly in your own voice, and never repeat their words as if you were them. Speech between them is not "
    "addressed to you; when words are unclear or clearly meant for someone else, do not invent what happened or what "
    "anyone felt. Wait, or ask one short question.\n"
    "- Describe only what is visible in the CURRENT camera or screen frame. Never present something you only heard "
    "about earlier as if you can see it, and say plainly when you cannot see clearly or the camera is off.\n"
    "- Reply in the language of the user's latest full sentence: Hindi → Hindi, Hinglish → Hinglish in Roman script, "
    "English → English. When they ask for a language, switch at once (never announce the switch in another "
    "language) and stay in it until they change it.\n"
    "- You are female. In Hindi and Hinglish always speak about yourself in feminine forms: “main karungi”, “karti "
    "hoon”, “kar sakti hoon”, “bol rahi hoon”, “samajh gayi” (करूँगी, करती हूँ, कर सकती हूँ, बोल रही हूँ, समझ गई). "
    "Never “karunga”, “sakta”, “raha”, “gaya” (करूँगा, सकता, रहा, गया) about yourself."
)


def live_verbatim_grounding(text: str) -> str:
    """Optional current-turn evidence, not a persistent echo mode or model call."""
    value = str(text or "").strip()
    if re.search(r"\b(?:what does|meaning of|translate|explain the phrase)\b", value, re.I):
        return ""
    if not re.search(
        r"\b(?:repeat (?:exactly|what I(?:'m| am| said)|this exactly)|"
        r"say exactly what I said|echo this|word (?:for|to) word)\b", value, re.I
    ):
        return ""
    # JSON delineates untrusted user wording; it is not promoted to system policy.
    import json
    return ("CURRENT USER UTTERANCE (this activity only; user-level data, not history):\n"
            + json.dumps(value, ensure_ascii=False)
            + "\nApply the current-turn rules to this utterance, not an older transcript.")


@dataclass
class LiveVoiceSession:
    user_id: str
    personality_brief: dict | str
    p0_capsule: str
    language_learning_capsule: str | None = None
    p4_turns: list[dict] = field(default_factory=list)
    degraded_mode: bool = False

    def append_turn(self, role: str, text: str) -> None:
        self.p4_turns.append({"role": role, "content": text})
        self.p4_turns = self.p4_turns[-20:]

    @classmethod
    def create(cls, user_id: str) -> 'LiveVoiceSession':
        from memory.service import MemoryService
        service = MemoryService(user_id=user_id, read_only=True)
        try:
            brief = service.profile_data() or {}
            p0 = service.get_p0_capsule() or ""
            lang_capsule = service.get_language_learning_capsule()
        finally:
            service.close()
            
        return cls(
            user_id=user_id,
            personality_brief=brief,
            p0_capsule=p0,
            language_learning_capsule=lang_capsule
        )


class LiveMemorySidecar:
    """Retrieve deeper tiers only when the transcript makes them relevant."""

    _P2 = re.compile(r"(?i)\b(before|previous|used to|past|last year|earlier)\b")
    _P3 = re.compile(r"(?i)\b(yesterday|last week|recently|when did|what did i)\b")
    _ENTITY = re.compile(r"(?i)\b(remember when|do you remember|my laptop|my phone|with [A-Z][a-z]+)\b")

    @classmethod
    def tiers_for_transcript(cls, transcript: str) -> tuple[str, ...]:
        tiers = []
        if cls._ENTITY.search(transcript):
            tiers.append("P1")
        if cls._P2.search(transcript):
            tiers.append("P2")
        if cls._P3.search(transcript):
            tiers.append("P3")
        return tuple(dict.fromkeys(tiers))

    def prefetch(self, partial_transcript: str, retrieve: Callable[[str, tuple[str, ...]], str]) -> str:
        tiers = self.tiers_for_transcript(partial_transcript)
        return retrieve(partial_transcript, tiers) if tiers else ""


class PronunciationNormalizer:
    """Deterministic aliases first; no LLM call for straightforward names."""

    def __init__(self, aliases: dict[str, str] | None = None):
        self.aliases = {key.lower(): value for key, value in (aliases or {}).items()}

    def for_tts(self, exact_response: str) -> str:
        words = re.split(r"(\W+)", exact_response)
        return "".join(self.aliases.get(word.lower(), word) for word in words)

    def correct_stt_name(self, candidate: str, known_names: list[str], threshold: float = 0.88) -> str | None:
        ranked = sorted(
            ((SequenceMatcher(None, candidate.lower(), name.lower()).ratio(), name)
             for name in known_names),
            reverse=True,
        )
        if not ranked or ranked[0][0] < threshold:
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.04:
            return None
        return ranked[0][1]


@dataclass(frozen=True)
class ReadAloudRequest:
    response_id: str
    exact_response: str
    normalized_pronunciation: str

    @classmethod
    def from_existing_response(
        cls, response_id: str, exact_response: str, normalizer: PronunciationNormalizer
    ) -> "ReadAloudRequest":
        return cls(
            response_id=response_id,
            exact_response=exact_response,
            normalized_pronunciation=normalizer.for_tts(exact_response),
        )


@dataclass(frozen=True)
class VoiceFallbackPlan:
    stt_scope: str = "JUPITER_SHARED_GROQ"
    stt_primary: str = "whisper-large-v3"
    stt_fallback: str = "whisper-large-v3-turbo"
    response_pipeline: str = "normal_text_free_router"
    tts_pipeline: str = "verified_free_tts_router"


class STTUnavailable(RuntimeError):
    def __init__(self, message: str, *, category: str = "STT_ROUTES_FAILED"):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class STTResult:
    text: str
    provider: str
    model_id: str
    language: str = ""
    confidence: float | None = None
    raw_text: str = ""


class ProcessedAudioSTTService:
    """Transcribe Jupiter's processed mic stream without fixing one language."""

    MODEL_PRIORITY = ("whisper-large-v3-turbo", "whisper-large-v3")
    CLOUDFLARE_MODEL_PRIORITY = (
        "@cf/openai/whisper-large-v3-turbo",
        "@cf/openai/whisper",
    )

    @classmethod
    def _eligible_model_ids(cls) -> tuple[str, ...]:
        """Honor current registry health while retaining a bootstrap path."""

        from services.local_model_registry import LocalModelRegistry

        rows = [
            row for row in LocalModelRegistry().load().get("models", [])
            if isinstance(row, dict)
            and row.get("provider") == "groq"
            and row.get("model_id") in cls.MODEL_PRIORITY
        ]
        active = {
            str(row.get("model_id")) for row in rows
            if row.get("status") in {"active", "backup"}
            and "audio_transcription" in set(row.get("roles") or [])
            and row.get("output_valid") is True
            and any(marker in str(row.get("free_status_confidence") or "").casefold() for marker in ("supplied-free", "official-free", "positive-free"))
            and not any(marker in str(row.get("free_status_confidence") or "").casefold() for marker in ("unknown", "unconfirmed", "paid", "zero"))
            and (row.get("discovery") or {}).get("available") is not False
        }
        return tuple(model_id for model_id in cls.MODEL_PRIORITY if model_id in active)

    @classmethod
    def _eligible_cloudflare_model_ids(cls) -> tuple[str, ...]:
        """Return only output-verified, free-evidence Workers AI ASR rows."""

        from services.local_model_registry import LocalModelRegistry

        rows = LocalModelRegistry().load().get("models", [])
        eligible: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("provider") != "cloudflare":
                continue
            model_id = str(row.get("model_id") or "")
            if model_id not in cls.CLOUDFLARE_MODEL_PRIORITY:
                continue
            confidence = str(row.get("free_status_confidence") or "").casefold()
            free_evidence = bool(
                any(marker in confidence for marker in ("official-free", "supplied-free", "positive-free"))
                and not any(marker in confidence for marker in ("unknown", "unconfirmed", "paid", "zero"))
            )
            roles = set(row.get("roles") or [])
            if (
                row.get("status") in {"active", "backup"}
                and row.get("output_valid") is True
                and free_evidence
                and (row.get("discovery") or {}).get("available") is not False
                and (row.get("discovery") or {}).get("catalog_free_status") != "paid"
                and ("audio_transcription" in roles or "audio_difficult" in roles)
            ):
                eligible.add(model_id)
        return tuple(model_id for model_id in cls.CLOUDFLARE_MODEL_PRIORITY if model_id in eligible)

    @staticmethod
    def _correct_assistant_name(text: str, confidence: float | None) -> str:
        """Correct a phonetic Jupiter candidate only in an assistant-address context."""

        if confidence is not None and confidence < 0.45:
            return text
        normalizer = PronunciationNormalizer()
        patterns = (
            re.compile(r"(?i)\b(hey|hi|hello|okay|ok|thanks|thank you|dear)(\s+)([a-z]{4,10})\b"),
            re.compile(r"(?i)\b([a-z]{4,10})(?=,?\s+(?:can|could|would|will|please|do|what|how|why|tell|help|are|is)\b)"),
        )

        def replace_vocative(match: re.Match) -> str:
            candidate = match.group(3)
            # "Peter" is a common partial-phonetic Whisper rendering of the
            # final syllables in "Jupiter". The lower threshold is confined
            # to a clear assistant-address/vocative grammar; it is never a
            # global Peter/sir replacement.
            corrected = normalizer.correct_stt_name(candidate, ["Jupiter"], threshold=0.62)
            return f"{match.group(1)}{match.group(2)}{corrected or candidate}"

        def replace_direct(match: re.Match) -> str:
            candidate = match.group(1)
            return normalizer.correct_stt_name(candidate, ["Jupiter"], threshold=0.62) or candidate

        corrected = patterns[0].sub(replace_vocative, text)
        return patterns[1].sub(replace_direct, corrected)

    @staticmethod
    def _confidence(response) -> float | None:
        segments = list(getattr(response, "segments", None) or [])
        logprobs = [
            float(getattr(segment, "avg_logprob", 0)) for segment in segments
            if getattr(segment, "avg_logprob", None) is not None
        ]
        if not logprobs:
            return None
        return max(0.0, min(1.0, math.exp(sum(logprobs) / len(logprobs))))

    # Whisper's `prompt` is PRECEDING-TRANSCRIPT / vocabulary context, not an instruction channel: with audio that
    # contains no speech, Whisper regurgitates it verbatim (the real-WT "Preserve the speaker's language …" row).
    # So the prompt carries only vocabulary (Jupiter's name) plus caller-supplied context, never instructions.
    WHISPER_VOCABULARY = "Jupiter."
    NO_SPEECH_PROBABILITY = 0.6

    @staticmethod
    def _tokens(value: str) -> list[str]:
        import re as _re
        return _re.findall(r"[\w']+", str(value or "").casefold())

    @classmethod
    def _prompt_echo(cls, text: str, prompt: str) -> bool:
        """True when the 'transcript' is essentially the prompt we sent (no speech was actually recognised)."""
        words = cls._tokens(text)
        if len(words) < 3:
            return False
        vocabulary = set(cls._tokens(prompt))
        return sum(1 for word in words if word in vocabulary) / len(words) >= 0.8

    # Languages a one- or two-word take is trusted in (Jupiter's users: English and the major Indian languages).
    SHORT_TAKE_LANGUAGES = frozenset({
        "english", "en", "hindi", "hi", "bengali", "bn", "urdu", "ur", "marathi", "mr", "tamil", "ta", "telugu", "te",
        "gujarati", "gu", "punjabi", "pa", "kannada", "kn", "malayalam", "ml", "nepali", "ne", "assamese", "as",
        "odia", "oriya", "or", "",
    })

    @classmethod
    def _unlikely_short_take_language(cls, text: str, response) -> bool:
        words = cls._tokens(text)
        language = str(getattr(response, "language", "") or "").strip().casefold()
        return 0 < len(words) <= 2 and language not in cls.SHORT_TAKE_LANGUAGES

    @classmethod
    def _vocabulary_only(cls, text: str, prompt: str) -> bool:
        """A one- or two-word 'transcript' made only of the prompt's vocabulary (a short prompt echo)."""
        words = cls._tokens(text)
        return 0 < len(words) <= 2 and set(words) <= set(cls._tokens(prompt))

    @classmethod
    def _no_speech(cls, response) -> bool:
        """Whisper's own segment evidence: high no-speech probability with low decoding confidence."""
        segments = list(getattr(response, "segments", None) or [])
        probabilities = [float(getattr(seg, "no_speech_prob", None) if not isinstance(seg, dict) else seg.get("no_speech_prob", 0) or 0)
                         for seg in segments]
        probabilities = [value for value in probabilities if value is not None]
        if not probabilities:
            return False
        confidence = cls._confidence(response)
        return (sum(probabilities) / len(probabilities) >= cls.NO_SPEECH_PROBABILITY
                and (confidence is None or confidence < 0.5))

    # The whole-clip check above misses speech FOLLOWED by room noise: Whisper then invents a trailing sentence for
    # the noise (real emulator 30-Sep: "...speech to text test. I'm going to go to the next slide."). Each segment is
    # judged by the energy of its own audio; one laid over near-silence (absolute floor, or far below the loudest
    # segment) is dropped. Segments with speech energy are always kept.
    SEGMENT_SILENCE_RMS = 0.004
    SEGMENT_RELATIVE_RMS = 0.1

    @staticmethod
    def _segment_value(segment, name):
        return segment.get(name) if isinstance(segment, dict) else getattr(segment, name, None)

    @classmethod
    def _speech_segments_text(cls, response, samples, rate: int) -> str | None:
        """Text of the segments whose audio carries speech energy; None when segments/timestamps are unusable."""
        segments = list(getattr(response, "segments", None) or [])
        if not segments or not samples or rate <= 0:
            return None
        spans = []
        for segment in segments:
            try:
                start = max(0.0, float(cls._segment_value(segment, "start") or 0.0))
                end = float(cls._segment_value(segment, "end") or 0.0)
            except (TypeError, ValueError):
                return None
            low, high = int(start * rate), min(len(samples), int(end * rate))
            if high <= low:
                return None
            window = samples[low:high:4] or samples[low:high]
            rms = math.sqrt(sum(value * value for value in window) / len(window)) / 32768.0
            spans.append((" ".join(str(cls._segment_value(segment, "text") or "").split()), rms))
        floor = max(cls.SEGMENT_SILENCE_RMS, max(rms for _, rms in spans) * cls.SEGMENT_RELATIVE_RMS)
        return " ".join(text for text, rms in spans if text and rms >= floor).strip()

    # Steady noise with no speech (a fan, headset hiss) makes Whisper invent sentences ("I'm going to go.", real
    # emulator 30-Sep, 3.5 s at ~-26 dBFS). Speech always swings — pauses between words — so its loud frames are far
    # above its median and a minority of the take; steady noise sits near its own peak. Measured on real takes and
    # test phrases: noise p95/p50 = 1.59 with 64% of frames within 6 dB of p95; every speech sample >= 1.93 and <= 50%.
    STEADY_NOISE_RATIO = 1.75
    STEADY_NOISE_LOUD_SHARE = 0.55

    @classmethod
    def _steady_noise_only(cls, samples, rate: int) -> bool:
        if not samples or rate <= 0:
            return False
        size = max(1, int(rate * 0.032))
        step = 2 if len(samples) < rate * 60 else 8
        levels = []
        for start in range(0, len(samples) - size + 1, size):
            window = samples[start:start + size:step]
            if max(abs(value) for value in window) < 3:
                continue                                   # digital silence / dither is not sound
            levels.append(math.sqrt(sum(value * value for value in window) / len(window)))
        if len(levels) < 20:
            return False
        levels.sort()
        p50, p95 = levels[len(levels) // 2], levels[min(len(levels) - 1, int(len(levels) * 0.95))]
        if p50 <= 0:
            return False
        loud_share = sum(1 for level in levels if level >= p95 * 0.5) / len(levels)
        return p95 / p50 < cls.STEADY_NOISE_RATIO and loud_share > cls.STEADY_NOISE_LOUD_SHARE

    def transcribe(self, wav_audio: bytes, *, context_prompt: str = "") -> STTResult:
        if not wav_audio.startswith(b"RIFF") or len(wav_audio) < 48:
            raise STTUnavailable("Processed microphone audio is invalid.", category="INVALID_AUDIO")
        try:
            with wave.open(io.BytesIO(wav_audio), "rb") as audio_file:
                audio_seconds = audio_file.getnframes() / max(1, audio_file.getframerate())
                pcm = audio_file.readframes(audio_file.getnframes())
                silent_value = 128 if audio_file.getsampwidth() == 1 else 0
                if not pcm or all(value == silent_value for value in pcm):
                    raise STTUnavailable("No speech audio was captured.", category="EMPTY_AUDIO")
                samples, sample_rate = None, audio_file.getframerate()
                if audio_file.getsampwidth() == 2 and audio_file.getnchannels() == 1 and len(pcm) % 2 == 0:
                    samples = array("h", pcm)
                    if sys.byteorder != "little":
                        samples.byteswap()
        except (wave.Error, EOFError):
            raise STTUnavailable("Processed microphone audio is invalid.", category="INVALID_AUDIO") from None
        if samples is not None and self._steady_noise_only(samples, sample_rate):
            raise STTUnavailable("No speech was detected.", category="NO_SPEECH")
        from services.quota_governor import quota_governor

        last_category = "STT_ROUTES_FAILED"
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if api_key:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1", timeout=35.0)
            try:
                for model_id in self._eligible_model_ids():
                    model = ProviderModel(
                        provider="groq",
                        model_id=model_id,
                        display_name=model_id,
                        credential_scope=CredentialScope.JUPITER_SHARED_GROQ,
                        credential_owner_id="*",
                        capabilities=frozenset({"audio_transcription"}),
                        metadata={"audio_seconds": audio_seconds},
                    )
                    governor = quota_governor()
                    admission = governor.reserve(model, 1)
                    if not admission.allowed:
                        last_category = "FREE_QUOTA_EXHAUSTED"
                        continue
                    try:
                        whisper_prompt = (self.WHISPER_VOCABULARY + " " + str(context_prompt or "")[:600]).strip()
                        response = client.audio.transcriptions.create(
                            model=model_id,
                            file=("jupiter-dictation.wav", wav_audio, "audio/wav"),
                            response_format="verbose_json",
                            temperature=0,
                            prompt=whisper_prompt,
                        )
                        raw_text = " ".join(str(getattr(response, "text", "") or "").split())
                        if self._vocabulary_only(raw_text, whisper_prompt):
                            # Only the prompt's own words came back (real room-noise take 30-Sep: "Jupiter."; Groq
                            # reports no_speech_prob 0.0 even then). Ask once more without the prompt: noise returns
                            # nothing usable, a genuinely spoken "Jupiter" still returns "Jupiter".
                            if not governor.reserve(model, 1).allowed:
                                raise STTUnavailable("No speech was detected.", category="NO_SPEECH")
                            response = client.audio.transcriptions.create(
                                model=model_id,
                                file=("jupiter-dictation.wav", wav_audio, "audio/wav"),
                                response_format="verbose_json",
                                temperature=0,
                            )
                            whisper_prompt = ""
                            raw_text = " ".join(str(getattr(response, "text", "") or "").split())
                        if self._unlikely_short_take_language(raw_text, response):
                            # One or two words tagged Icelandic/Japanese/... ("Hi." -> "Hæ.", "はい。", real runs
                            # 30-Sep): language ID on a single syllable is unstable. Re-read once as English.
                            if governor.reserve(model, 1).allowed:
                                retry = client.audio.transcriptions.create(
                                    model=model_id,
                                    file=("jupiter-dictation.wav", wav_audio, "audio/wav"),
                                    response_format="verbose_json",
                                    temperature=0,
                                    language="en",
                                )
                                retry_text = " ".join(str(getattr(retry, "text", "") or "").split())
                                if re.search(r"\w", retry_text):
                                    response, raw_text = retry, retry_text
                        confidence = self._confidence(response)
                        speech_text = self._speech_segments_text(response, samples, sample_rate)
                        text = self._correct_assistant_name(raw_text if speech_text is None else speech_text, confidence)
                        if (not re.search(r"\w", text) or self._no_speech(response)
                                or self._prompt_echo(raw_text, whisper_prompt)):
                            raise STTUnavailable("No speech was detected.", category="NO_SPEECH")
                        governor.success(model, audio_seconds=audio_seconds, admission=admission)
                        from services.local_model_registry import LocalModelRegistry
                        LocalModelRegistry().success(model)
                        return STTResult(
                            text=text,
                            provider="groq",
                            model_id=model_id,
                            language=str(getattr(response, "language", "") or ""),
                            confidence=confidence,
                            raw_text=raw_text,
                        )
                    except STTUnavailable as error:
                        governor.failure(model, error.category)
                        last_category = error.category
                        if error.category == "NO_SPEECH":
                            raise
                    except Exception as error:
                        last_category = VerifiedFreeTTSService._failure_category(error)
                        from core.provider_adapters import GroqProvider
                        governor.failure(model, GroqProvider().normalize_error(error))
            finally:
                client.close()
        else:
            last_category = "AUTH_FAILED"

        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        cloudflare_token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
        if account_id and cloudflare_token:
            for model_id in self._eligible_cloudflare_model_ids():
                model = ProviderModel(
                    provider="cloudflare",
                    model_id=model_id,
                    display_name=model_id,
                    credential_scope=CredentialScope.JUPITER_SHARED_CLOUDFLARE,
                    credential_owner_id="*",
                    capabilities=frozenset({"audio_transcription"}),
                    metadata={"audio_seconds": audio_seconds},
                )
                governor = quota_governor()
                admission = governor.reserve(model, 1)
                if not admission.allowed:
                    last_category = "FREE_QUOTA_EXHAUSTED"
                    continue
                try:
                    response = httpx.post(
                        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model_id}",
                        headers={
                            "Authorization": f"Bearer {cloudflare_token}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "audio": base64.b64encode(wav_audio).decode("ascii"),
                            "task": "transcribe",
                            "vad_filter": True,
                            "condition_on_previous_text": False,
                            "initial_prompt": (self.WHISPER_VOCABULARY + " " + str(context_prompt or "")[:360]).strip(),
                        },
                        timeout=35.0,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    result = payload.get("result") if isinstance(payload, dict) else None
                    raw_text = " ".join(str((result or {}).get("text") or "").split())
                    text = self._correct_assistant_name(raw_text, None)
                    cf_prompt = (self.WHISPER_VOCABULARY + " " + str(context_prompt or "")[:360]).strip()
                    if not text or self._prompt_echo(raw_text, cf_prompt):
                        raise STTUnavailable("No speech was detected.", category="NO_SPEECH")
                    governor.success(model, audio_seconds=audio_seconds, admission=admission,
                                     actual_shared_units=governor.actual_shared_units(payload))
                    from services.local_model_registry import LocalModelRegistry
                    LocalModelRegistry().success(model)
                    return STTResult(
                        text=text,
                        provider="cloudflare",
                        model_id=model_id,
                        language=str((result or {}).get("language") or ""),
                        confidence=None,
                        raw_text=raw_text,
                    )
                except STTUnavailable as error:
                    governor.failure(model, error.category)
                    last_category = error.category
                    if error.category == "NO_SPEECH":
                        raise
                except Exception as error:
                    last_category = VerifiedFreeTTSService._failure_category(error)
                    from core.provider_adapters import CloudflareProvider
                    governor.failure(model, CloudflareProvider().normalize_error(error))
        raise STTUnavailable("All processed-audio STT routes failed.", category=last_category)


class TTSUnavailable(RuntimeError):
    def __init__(self, message: str, *, category: str = "TTS_ROUTES_FAILED"):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class TTSResult:
    audio: bytes
    mime_type: str
    provider: str
    model_id: str
    language: str = "other"
    latency_ms: int | None = None
    duration_ms: int | None = None
    silence: dict = field(default_factory=dict)


SPEECH_CONFIG_VERSION = "gemini-natural-voice-v4"
NATURAL_VOICE_MODE = "natural_provider_tts"


def optimize_wav_silence(
    wav_audio: bytes,
    *,
    threshold_dbfs: float = -45.0,
    frame_ms: int = 20,
) -> tuple[bytes, dict]:
    """Measure PCM silence and trim only clearly abnormal excess.

    Gemini supplies the natural prosody.  The conservative limits below leave
    ordinary breaths and sentence cadence intact: only boundary silence above
    800 ms or internal silence above 1.8 s is shortened.  Kept silence is
    intentionally longer than a phoneme/breath so trimming cannot clip speech.
    """

    try:
        with wave.open(io.BytesIO(wav_audio), "rb") as source:
            params = source.getparams()
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getcomptype() != "NONE"
                or source.getframerate() <= 0
            ):
                raise ValueError("unsupported PCM shape")
            sample_rate = source.getframerate()
            pcm = source.readframes(source.getnframes())
    except (EOFError, ValueError, wave.Error) as error:
        raise TTSUnavailable(
            "The TTS provider returned invalid WAV audio.", category="INVALID_AUDIO"
        ) from error

    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise TTSUnavailable(
            "The TTS provider returned empty WAV audio.", category="INVALID_AUDIO"
        )

    block_samples = max(1, round(sample_rate * frame_ms / 1000))
    threshold = (10 ** (threshold_dbfs / 20.0)) * 32768.0

    def silence_segments(values: array) -> list[tuple[int, int]]:
        quiet_blocks: list[bool] = []
        for start in range(0, len(values), block_samples):
            block = values[start : start + block_samples]
            rms = math.sqrt(sum(value * value for value in block) / max(1, len(block)))
            quiet_blocks.append(rms < threshold)
        segments: list[tuple[int, int]] = []
        segment_start: int | None = None
        for block_index, quiet in enumerate([*quiet_blocks, False]):
            if quiet and segment_start is None:
                segment_start = block_index * block_samples
            elif not quiet and segment_start is not None:
                segments.append((segment_start, min(block_index * block_samples, len(values))))
                segment_start = None
        return segments

    def duration_ms(sample_count: int) -> int:
        return round(sample_count * 1000 / sample_rate)

    before_segments = silence_segments(samples)
    leading_before = (
        duration_ms(before_segments[0][1])
        if before_segments and before_segments[0][0] == 0
        else 0
    )
    trailing_before = (
        duration_ms(len(samples) - before_segments[-1][0])
        if before_segments and before_segments[-1][1] == len(samples)
        else 0
    )
    internal_before = [
        duration_ms(end - start)
        for start, end in before_segments
        if start > 0 and end < len(samples)
    ]

    cuts: list[tuple[int, int]] = []
    boundary_limit = round(sample_rate * 0.8)
    boundary_keep = round(sample_rate * 0.24)
    internal_limit = round(sample_rate * 1.8)
    internal_keep_each_side = round(sample_rate * 0.3)
    for index, (start, end) in enumerate(before_segments):
        length = end - start
        if start == 0 and length > boundary_limit:
            cuts.append((0, max(0, end - boundary_keep)))
        elif end == len(samples) and length > boundary_limit:
            cuts.append((min(end, start + boundary_keep), end))
        elif start > 0 and end < len(samples) and length > internal_limit:
            cuts.append((start + internal_keep_each_side, end - internal_keep_each_side))

    optimized = samples
    if cuts:
        rebuilt = array("h")
        cursor = 0
        for cut_start, cut_end in sorted(cuts):
            if cut_end <= cut_start:
                continue
            rebuilt.extend(samples[cursor:cut_start])
            cursor = cut_end
        rebuilt.extend(samples[cursor:])
        optimized = rebuilt

    after_segments = silence_segments(optimized)
    leading_after = (
        duration_ms(after_segments[0][1])
        if after_segments and after_segments[0][0] == 0
        else 0
    )
    trailing_after = (
        duration_ms(len(optimized) - after_segments[-1][0])
        if after_segments and after_segments[-1][1] == len(optimized)
        else 0
    )
    internal_after = [
        duration_ms(end - start)
        for start, end in after_segments
        if start > 0 and end < len(optimized)
    ]
    report = {
        "threshold_dbfs": threshold_dbfs,
        "frame_ms": frame_ms,
        "duration_ms": duration_ms(len(optimized)),
        "leading_before_ms": leading_before,
        "leading_after_ms": leading_after,
        "trailing_before_ms": trailing_before,
        "trailing_after_ms": trailing_after,
        "longest_internal_before_ms": max(internal_before, default=0),
        "longest_internal_after_ms": max(internal_after, default=0),
        "trimmed_ms": duration_ms(len(samples) - len(optimized)),
    }
    if not cuts:
        return wav_audio, report

    output = io.BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setparams(params)
        output_samples = array("h", optimized)
        if sys.byteorder != "little":
            output_samples.byteswap()
        destination.writeframes(output_samples.tobytes())
    return output.getvalue(), report


class VerifiedFreeTTSService:
    """Execute only currently verified-free TTS models, with scoped credentials."""

    # Backwards-compatible class default. Per-request instances may use an
    # explicit, validated user preference without changing global state.
    VOICE_NAME = DEFAULT_JUPITER_TTS_VOICE
    _MODEL_PRIORITY = (
        "gemini-3.1-flash-tts-preview",
        "gemini-2.5-flash-preview-tts",
    )
    _EXPLICITLY_INELIGIBLE_MARKERS = (
        "zero-or-unconfirmed",
        "zero-quota",
        "unsupported-modality",
        "unsupported-model",
        "invalid-model",
        "model-not-found",
        "deprecated",
    )

    def __init__(self, voice_name: str | None = None):
        self.voice_name = normalize_jupiter_voice(voice_name)
    _DEVANAGARI = re.compile(r"[\u0900-\u097f]")
    _ROMAN_HINDI_WORDS = frozenset({
        "aaj", "abhi", "achha", "accha", "ap", "aap", "apka", "aapka",
        "batao", "bahut", "bilkul", "chal", "chalo", "hai", "hain", "haan",
        "ho", "hoon", "ka", "kaafi", "kaise", "karti", "karna", "kya",
        "main", "mai", "mera", "meri", "nahi", "raha", "rahi", "sahi",
        "theek", "thik", "tum", "tumhara", "ye", "yeh",
    })
    _ENGLISH_WORDS = frozenset({
        "a", "actually", "am", "are", "check", "doing", "good", "great",
        "hello", "hey", "how", "i", "idea", "is", "it", "that", "the",
        "this", "today", "what", "you", "your",
    })

    @classmethod
    def detect_spoken_language(cls, text: str) -> str:
        """Classify TTS delivery without changing the displayed response text."""

        source = str(text or "")
        words = re.findall(r"[A-Za-z']+", source.casefold())
        hindi_hits = sum(word in cls._ROMAN_HINDI_WORDS for word in words)
        english_hits = sum(word in cls._ENGLISH_WORDS for word in words)
        has_devanagari = bool(cls._DEVANAGARI.search(source))
        if has_devanagari:
            return "hinglish" if english_hits >= 2 else "hindi"
        if hindi_hits >= 2:
            return "hinglish" if english_hits >= 2 else "hindi"
        if words:
            return "english"
        return "other"

    @classmethod
    def _speech_prompt(cls, speakable: str, language: str) -> str:
        language_direction = {
            "english": (
                "Use contemporary conversational English pronunciation, natural contractions, "
                "and punctuation-led pauses."
            ),
            "hindi": (
                "Use natural contemporary spoken Hindi pronunciation and prosody, not an "
                "English-accented, textbook, Sanskritized, or newsreader delivery."
            ),
            "hinglish": (
                "Preserve the Hindi-English code-switching exactly as written. Move between the "
                "languages fluidly without translating words or resetting the speaking rhythm."
            ),
            "other": (
                "Use the natural pronunciation and conversational prosody of the transcript's language."
            ),
        }.get(language, "Use natural conversational pronunciation and prosody.")
        return (
            "Perform one continuous, single-speaker read-aloud. "
            "The voice is a warm, modern young-adult assistant: friendly, relaxed, "
            "confident, and expressive without sounding theatrical, childish, announcer-like, "
            "or robotic. Use a natural conversational pace, smooth intonation, and brief pauses "
            "guided by punctuation; do not exaggerate pitch or emotion. "
            f"{language_direction} "
            "Read only the transcript after the TRANSCRIPT marker. Preserve its wording and do "
            "not speak these directions or the marker.\n\nTRANSCRIPT:\n"
            + speakable
        )

    @staticmethod
    def _candidates() -> list[dict]:
        from services.local_model_registry import LocalModelRegistry

        rows = []
        for row in LocalModelRegistry().load().get("models", []):
            if str(row.get("provider") or "").casefold() != "google":
                continue
            capability = str(row.get("capability") or "").casefold()
            if "tts" not in set(row.get("roles") or []) and capability not in {"tts", "text-to-speech"}:
                continue
            model_id = str(row.get("model_id") or "")
            if model_id not in VerifiedFreeTTSService._MODEL_PRIORITY:
                continue
            confidence = str(row.get("free_status_confidence") or "").casefold()
            if "positive" not in confidence and "verified" not in confidence:
                continue
            if any(marker in confidence for marker in ("unverified", "unknown", "unconfirmed", "paid", "zero")):
                continue
            discovery = row.get("discovery") or {}
            if discovery.get("available") is False or discovery.get("catalog_free_status") == "paid":
                continue
            normalized_error = str(row.get("normalized_error") or "").casefold()
            if any(marker in normalized_error for marker in VerifiedFreeTTSService._EXPLICITLY_INELIGIBLE_MARKERS):
                continue
            active_validated_route = (
                str(row.get("status") or "").casefold() in {"active", "backup"}
                and row.get("output_valid") is True
            )
            # Older registry audits treated a successful non-text response as
            # output_invalid even when the TTS endpoint returned HTTP 2xx with
            # no normalized provider error. The runtime audio parser now
            # validates output_audio/inline_data and rejects malformed audio
            # per request, so that transport-verified TTS route may be tried
            # independently without activating unknown or explicitly blocked
            # models.
            transport_verified_tts_route = (
                isinstance(row.get("http_result"), int)
                and 200 <= int(row["http_result"]) < 300
                and not normalized_error
            )
            if not active_validated_route and not transport_verified_tts_route:
                continue
            rows.append(row)
            
        def sort_key(row):
            model_id = str(row.get("model_id") or "")
            try:
                return VerifiedFreeTTSService._MODEL_PRIORITY.index(model_id)
            except ValueError:
                return len(VerifiedFreeTTSService._MODEL_PRIORITY)
            
        rows.sort(key=sort_key)
        return rows

    @staticmethod
    def _credential_attempts(model_id: str) -> list[tuple[CredentialScope, str, str, str, str]]:
        from config.credential_policy import managed_google_policies

        attempts: list[tuple[CredentialScope, str, str, str, str]] = []
        context = current_request_context()
        uses_jupiter_user_google = bool(context.managed_google_credentials)
        # Always prefer the requesting account's own key when one is eligible.
        user_key = resolve_request_credential(CredentialScope.USER_GOOGLE.value)
        if user_key:
            user_id = context.user_id or "unknown"
            attempts.append((
                CredentialScope.USER_GOOGLE,
                user_key,
                "User credential",
                user_id,
                request_google_quota_group(),
            ))
        seen_keys = {user_key} if user_key else set()
        for policy in managed_google_policies(
            model_id,
            "tts",
            include_jupiter_user_google=uses_jupiter_user_google,
        ):
            shared_key = policy.secret()
            if not shared_key or shared_key in seen_keys:
                continue
            seen_keys.add(shared_key)
            attempts.append((
                CredentialScope.JUPITER_SHARED_GOOGLE,
                shared_key,
                policy.label,
                policy.credential_id,
                policy.quota_group_key(),
            ))
        return attempts

    @classmethod
    def cache_identity(cls, text: str, voice_name: str | None = None) -> dict:
        candidates = cls._candidates()
        if not candidates:
            raise TTSUnavailable("No verified-free TTS model is active.", category="NO_FREE_ROUTE")
        return {
            "provider": "google",
            "model": str(candidates[0].get("model_id") or ""),
            "voice": normalize_jupiter_voice(voice_name),
            "language": cls.detect_spoken_language(text),
            "config_version": SPEECH_CONFIG_VERSION,
            "voice_mode": NATURAL_VOICE_MODE,
        }

    @classmethod
    def cache_identities(cls, text: str, voice_name: str | None = None) -> list[dict]:
        """Return each independently eligible model identity in runtime order."""

        candidates = cls._candidates()
        if not candidates:
            raise TTSUnavailable("No verified-free TTS model is active.", category="NO_FREE_ROUTE")
        common = {
            "provider": "google",
            "voice": normalize_jupiter_voice(voice_name),
            "language": cls.detect_spoken_language(text),
            "config_version": SPEECH_CONFIG_VERSION,
            "voice_mode": NATURAL_VOICE_MODE,
        }
        return [{**common, "model": str(row.get("model_id") or "")} for row in candidates]

    @classmethod
    def cache_identity_for_model(
        cls, text: str, model_id: str, voice_name: str | None = None
    ) -> dict:
        return next(
            (identity for identity in cls.cache_identities(text, voice_name)
             if identity["model"] == str(model_id or "")),
            {},
        )

    @staticmethod
    def _wav_audio(pcm: bytes, mime_type: str = "") -> bytes:
        if pcm.startswith(b"RIFF"):
            return pcm
        rate_match = re.search(r"(?:rate=|rate:)(\d+)", str(mime_type or ""), re.IGNORECASE)
        sample_rate = int(rate_match.group(1)) if rate_match else 24000
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        return output.getvalue()

    @staticmethod
    def _audio_payload(interaction) -> tuple[bytes, str]:
        output_audio = getattr(interaction, "output_audio", None)
        data = getattr(output_audio, "data", None)
        mime_type = str(getattr(output_audio, "mime_type", "") or "")
        if data is None:
            candidates = getattr(interaction, "candidates", None) or []
            parts = getattr(getattr(candidates[0], "content", None), "parts", None) if candidates else []
            inline = getattr(parts[0], "inline_data", None) if parts else None
            data = getattr(inline, "data", None)
            mime_type = mime_type or str(getattr(inline, "mime_type", "") or "")
        if isinstance(data, str):
            data = base64.b64decode(data)
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise TTSUnavailable("The TTS provider returned no audio.", category="INVALID_AUDIO")
        return bytes(data), mime_type

    @staticmethod
    def _failure_category(error: Exception) -> str:
        message = str(error).casefold()
        if "quota" in message or "resource_exhausted" in message:
            return "FREE_QUOTA_EXHAUSTED"
        if "429" in message or "rate limit" in message or "rate_limit" in message:
            return "RATE_LIMIT"
        if "401" in message or "403" in message or "api key" in message or "permission" in message:
            return "AUTH_FAILED"
        if "timeout" in message:
            return "TIMEOUT"
        if "unsupported" in message and ("language" in message or "voice" in message):
            return "UNSUPPORTED_LANGUAGE_OR_VOICE"
        if "400" in message or "invalid argument" in message or "invalid_request" in message:
            return "INVALID_REQUEST"
        return "PROVIDER_FAILURE"

    @staticmethod
    def _status_for_category(category: str) -> int | None:
        return {
            "AUTH_FAILED": 401,
            "FREE_QUOTA_EXHAUSTED": 429,
            "RATE_LIMIT": 429,
            "TIMEOUT": 504,
            "INVALID_REQUEST": 400,
            "UNSUPPORTED_LANGUAGE_OR_VOICE": 400,
            "PROVIDER_FAILURE": 503,
        }.get(category)

    @staticmethod
    def _record_attempt(
        *,
        provider: str,
        model_id: str,
        latency_ms: int,
        success: bool,
        category: str | None,
        scope: CredentialScope,
    ) -> None:
        """Record only model-scoped, redacted speech health evidence."""

        try:
            from services.api_health_center import api_health_center

            api_health_center().record_attempt(
                user_id=current_request_context().user_id or "unknown",
                provider=provider,
                requested_model=model_id,
                actual_model=model_id if success else None,
                task_type="TTS",
                latency_ms=latency_ms,
                success=success,
                status_code=VerifiedFreeTTSService._status_for_category(category or ""),
                error_class=category,
                reason_code=category,
                safe_message=("Speech rendering succeeded." if success else "Speech rendering route failed."),
                fallback_number=0,
                details={"credential_scope": scope.value},
            )
        except Exception:
            # Health telemetry must never break speech rendering.
            pass

    def synthesize(self, text: str) -> TTSResult:
        speakable = " ".join(str(text or "").split())[:6000]
        if not speakable:
            raise TTSUnavailable("There is no speakable response text.", category="EMPTY_TEXT")
        spoken_language = self.detect_spoken_language(speakable)
        speech_prompt = self._speech_prompt(speakable, spoken_language)
        candidates = self._candidates()
        if not candidates:
            raise TTSUnavailable("No verified-free TTS model is active.", category="NO_FREE_ROUTE")

        last_category = "TTS_ROUTES_FAILED"
        user_auth_blocked = False
        managed_auth_blocked: set[str] = set()
        exhausted_quota_groups: set[tuple[str, str]] = set()
        credential_fallback_categories = {"FREE_QUOTA_EXHAUSTED", "RATE_LIMIT"}
        for row in candidates:
            model_id = str(row.get("model_id") or "")
            # Accept the former 3-field test/extension shape while keeping the
            # production 5-field credential identity and quota-group contract.
            credentials = [
                (
                    attempt[0], attempt[1], attempt[2],
                    attempt[3] if len(attempt) > 3 else str(attempt[2]),
                    attempt[4] if len(attempt) > 4 else f"managed-google:{attempt[2]}",
                )
                for attempt in self._credential_attempts(model_id)
                if len(attempt) >= 3
            ]
            if not credentials:
                last_category = "AUTH_FAILED"
                continue
            user_attempt = next((item for item in credentials if item[0] == CredentialScope.USER_GOOGLE), None)
            managed_attempts = [item for item in credentials if item[0] == CredentialScope.JUPITER_SHARED_GOOGLE]
            allow_managed_same_model = user_attempt is None or user_auth_blocked
            quota_token = lambda attempt: (attempt[4], model_id)
            route_attempts = ([user_attempt] if user_attempt and not user_auth_blocked else []) + (
                [
                    attempt for attempt in managed_attempts
                    if attempt[3] not in managed_auth_blocked
                    and quota_token(attempt) not in exhausted_quota_groups
                ]
            )
            for scope, api_key, credential_label, credential_id, quota_group in route_attempts:
                if scope == CredentialScope.JUPITER_SHARED_GOOGLE and not allow_managed_same_model:
                    continue
                quota_model = ProviderModel(
                    provider="google",
                    model_id=model_id,
                    display_name=str(row.get("requested_model_id") or model_id),
                    credential_scope=scope,
                    credential_owner_id=credential_id,
                    metadata={
                        "credential_label": credential_label,
                        "google_quota_group": quota_group,
                        "paid_usage_allowed": False,
                    },
                )
                from services.quota_governor import quota_governor
                governor = quota_governor()
                admission = governor.reserve(quota_model, max(1, len(speakable) // 4))
                if not admission.allowed:
                    last_category = "FREE_QUOTA_EXHAUSTED"
                    if scope == CredentialScope.USER_GOOGLE:
                        allow_managed_same_model = True
                    else:
                        exhausted_quota_groups.add((quota_group, model_id))
                    continue
                started_at = time.perf_counter()
                try:
                    from google import genai
                    from google.genai import types

                    client = genai.Client(api_key=api_key)
                    try:
                        interaction = client.models.generate_content(
                            model=model_id,
                            contents=speech_prompt,
                            config=types.GenerateContentConfig(
                                response_modalities=["AUDIO"],
                                speech_config=types.SpeechConfig(
                                    voice_config=types.VoiceConfig(
                                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                            voice_name=self.voice_name
                                        )
                                    )
                                ),
                            ),
                        )
                    finally:
                        client.close()
                    pcm, source_mime = self._audio_payload(interaction)
                    wav = self._wav_audio(pcm, source_mime)
                    wav, silence = optimize_wav_silence(wav)
                    latency_ms = round((time.perf_counter() - started_at) * 1000)
                    self._record_attempt(
                        provider="google",
                        model_id=model_id,
                        latency_ms=latency_ms,
                        success=True,
                        category=None,
                        scope=scope,
                    )
                    governor.success(quota_model, input_tokens=max(1, len(speakable) // 4), admission=admission)
                    recorder = current_request_context().credential_status_recorder
                    if recorder and scope == CredentialScope.USER_GOOGLE:
                        recorder(scope.value, "ACTIVE")
                    return TTSResult(
                        wav,
                        "audio/wav",
                        "google",
                        model_id,
                        language=spoken_language,
                        latency_ms=latency_ms,
                        duration_ms=int(silence.get("duration_ms") or 0),
                        silence=silence,
                    )
                except Exception as error:
                    # Empty/malformed audio is a failure of this model route,
                    # not a terminal failure for every eligible TTS model.
                    last_category = (
                        error.category
                        if isinstance(error, TTSUnavailable)
                        else self._failure_category(error)
                    )
                    from core.provider_adapters import GoogleProvider
                    quota_failure = last_category if isinstance(error, TTSUnavailable) else GoogleProvider(scope).normalize_error(error)
                    latency_ms = round((time.perf_counter() - started_at) * 1000)
                    self._record_attempt(
                        provider="google",
                        model_id=model_id,
                        latency_ms=latency_ms,
                        success=False,
                        category=last_category,
                        scope=scope,
                    )
                    Logger.warning(
                        f"TTS route provider=google model={model_id} scope={scope.value} "
                        f"category={last_category}"
                    )
                    recorder = current_request_context().credential_status_recorder
                    if recorder and scope == CredentialScope.USER_GOOGLE and last_category == "AUTH_FAILED":
                        recorder(scope.value, "AUTH_FAILED")
                    if scope == CredentialScope.USER_GOOGLE:
                        governor.failure(quota_model, quota_failure)
                        if last_category == "AUTH_FAILED":
                            user_auth_blocked = True
                            allow_managed_same_model = True
                            continue
                        elif last_category in credential_fallback_categories:
                            # Only credential-scoped quota/rate pressure permits
                            # the same logical model to use Jupiter's managed pool.
                            allow_managed_same_model = True
                            continue
                        # Invalid request/model/voice and generic provider
                        # failures advance the model, never the credential.
                        continue

                    governor.failure(quota_model, quota_failure)
                    if last_category == "AUTH_FAILED":
                        # Authentication is credential-scoped: retire only the
                        # affected managed key and let another configured key
                        # prove itself without changing provider/model health.
                        managed_auth_blocked.add(credential_id)
                        continue
                    if last_category in credential_fallback_categories:
                        exhausted_quota_groups.add((quota_group, model_id))
                        continue
                    # Invalid request/model/voice is not repaired by rotating
                    # credentials; advance to the next eligible TTS model.
                    break
        raise TTSUnavailable("All verified-free TTS routes failed.", category=last_category)
