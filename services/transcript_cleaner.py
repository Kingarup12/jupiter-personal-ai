"""Deterministic, context-bounded cleanup for live-speech transcripts.

Raw recognition text is always retained in provenance.  This module never calls
an AI provider and deliberately leaves ambiguous wording unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import re

from services.voice_pipeline import PronunciationNormalizer


@dataclass(frozen=True)
class TranscriptCorrection:
    original: str
    replacement: str
    reason: str
    confidence: float


@dataclass(frozen=True)
class TranscriptCleanup:
    raw: str
    cleaned: str
    confidence: float | None
    corrections: tuple[TranscriptCorrection, ...]
    uncertain: bool

    def as_dict(self) -> dict:
        correction_confidence = (
            min(item.confidence for item in self.corrections)
            if self.corrections else None
        )
        return {
            "raw": self.raw,
            "cleaned": self.cleaned,
            "raw_transcript": self.raw,
            "cleaned_transcript": self.cleaned,
            "confidence": self.confidence,
            "correction_confidence": correction_confidence,
            "correction_reason": "; ".join(
                dict.fromkeys(item.reason for item in self.corrections)
            ),
            "corrections": [asdict(item) for item in self.corrections],
            "uncertain": self.uncertain,
        }


class ContextualTranscriptCleaner:
    MAX_RECENT_TURNS = 8
    MAX_KNOWN_ENTITIES = 40

    @staticmethod
    def _bounded_text(items) -> str:
        return " ".join(
            " ".join(str(item.get("content") or item).split())[:500]
            for item in list(items or [])[-ContextualTranscriptCleaner.MAX_RECENT_TURNS:]
        ).casefold()

    @staticmethod
    def _replace_word(text: str, original: str, replacement: str) -> str:
        return re.sub(rf"(?i)\b{re.escape(original)}\b", replacement, text, count=1)

    @staticmethod
    def _semantic_punctuation(text: str) -> str:
        """Repair common pause-generated comma fragments conservatively."""

        cleaned = re.sub(r"\s+([,.;:?!])", r"\1", text)
        cleaned = re.sub(r"([,.;:?!]){2,}", r"\1", cleaned)
        # Device/ASR engines often emit commas between a subject and its verb,
        # or immediately after a preposition, solely because the speaker
        # hesitated. These are not semantic clause boundaries.
        cleaned = re.sub(
            r"(?i)\b(i|you|we|they|he|she|it),\s+"
            r"(?=(?:am|is|are|was|were|have|has|had|do|does|did|want|wanted|"
            r"need|needed|think|thought|know|knew|can|could|will|would|should)\b)",
            r"\1 ",
            cleaned,
        )
        cleaned = re.sub(
            r"(?i)\b(to|about|for|with|from|of|at|in|on|the|a|an),\s+",
            r"\1 ",
            cleaned,
        )
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        if cleaned and cleaned[-1] not in ".?!":
            interrogative = bool(re.match(
                r"(?i)^(?:who|what|when|where|why|how|which|is|are|am|do|does|"
                r"did|can|could|would|will|should)\b",
                cleaned,
            ))
            cleaned += "?" if interrogative else "."
        return cleaned

    def clean(
        self,
        raw: str,
        *,
        recent_turns: list[dict] | None = None,
        known_entities: list[str] | None = None,
        recognition_aliases: dict[str, str] | None = None,
        stt_confidence: float | None = None,
    ) -> TranscriptCleanup:
        cleaned = " ".join(str(raw or "").split())
        if not cleaned:
            return TranscriptCleanup("", "", stt_confidence, (), True)

        recent = self._bounded_text(recent_turns)
        corrections: list[TranscriptCorrection] = []
        confidence = float(stt_confidence) if isinstance(stt_confidence, (int, float)) else None
        low_acoustic_confidence = confidence is not None and confidence < 0.78

        # Previously confirmed recognition aliases are operational speech data,
        # not biography.  They remain bounded by the lexicon's own expiry.
        for heard, canonical in list((recognition_aliases or {}).items())[:40]:
            if not heard or not canonical or not re.search(rf"(?i)\b{re.escape(heard)}\b", cleaned):
                continue
            cleaned = self._replace_word(cleaned, heard, canonical)
            corrections.append(TranscriptCorrection(heard, canonical, "confirmed recognition alias", 0.96))

        # "Peter"-like renderings become Jupiter only in an explicit vocative
        # grammar. This endpoint is scoped to a Jupiter conversation, so the
        # greeting/address construction itself is strong addressee evidence;
        # ordinary occurrences of Peter or honorifics remain untouched.
        normalizer = PronunciationNormalizer()
        vocative = re.compile(
            r"(?i)\b(hey|hi|hello|okay|ok|thanks|thank you|dear)(\s+)([a-z]{4,10})\b"
        )

        def replace_assistant(match: re.Match) -> str:
            candidate = match.group(3)
            corrected = normalizer.correct_stt_name(candidate, ["Jupiter"], threshold=0.62)
            if not corrected:
                return match.group(0)
            corrections.append(TranscriptCorrection(
                candidate, corrected, "explicit Jupiter address context", 0.94
            ))
            return f"{match.group(1)}{match.group(2)}{corrected}"

        cleaned = vocative.sub(replace_assistant, cleaned)

        # Context-specific homophones require a strong semantic cue.
        if re.search(r"(?i)\bmile\b", cleaned) and re.search(r"\b(?:gender|male|female|man|woman)\b", recent):
            cleaned = self._replace_word(cleaned, "mile", "male")
            corrections.append(TranscriptCorrection("mile", "male", "recent gender context", 0.93))

        topic_shift = bool(re.search(r"(?i)\b(?:new topic|now about|actually|i mean|switch(?:ing)? to)\s+malda\b", cleaned))
        haldia_mentions = recent.count("haldia")
        if (
            re.search(r"(?i)\bmalda\b", cleaned)
            and not topic_shift
            and (low_acoustic_confidence or haldia_mentions >= 2)
            and "malda" not in recent
        ):
            cleaned = self._replace_word(cleaned, "Malda", "Haldia")
            corrections.append(TranscriptCorrection("Malda", "Haldia", "repeated recent entity context", 0.9))

        # Use the existing pronunciation normalizer only for explicit known
        # names/entities and only when a near-match is unambiguous.
        entities = []
        for entity in list(known_entities or [])[: self.MAX_KNOWN_ENTITIES]:
            normalized = " ".join(str(entity or "").split())
            if len(normalized) >= 2 and normalized.casefold() not in {item.casefold() for item in entities}:
                entities.append(normalized)
        tokens = re.findall(r"[A-Za-z][A-Za-z'-]{1,40}", cleaned)
        for token in tokens:
            if token.casefold() in {entity.casefold() for entity in entities}:
                continue
            match = normalizer.correct_stt_name(token, entities, threshold=0.88)
            if not match:
                continue
            relevance = (
                match.casefold() in recent
                or recent.count(match.split()[0].casefold()) >= 2
                or low_acoustic_confidence
            )
            similarity = SequenceMatcher(None, token.casefold(), match.casefold()).ratio()
            if not relevance or (not low_acoustic_confidence and similarity < 0.93):
                continue
            cleaned = self._replace_word(cleaned, token, match)
            corrections.append(TranscriptCorrection(token, match, "known contextual entity", round(similarity, 3)))

        cleaned = self._semantic_punctuation(cleaned)
        uncertain = bool(
            (confidence is not None and confidence < 0.78)
            or any(item.confidence < 0.92 for item in corrections)
        )
        return TranscriptCleanup(
            raw=" ".join(str(raw or "").split()),
            cleaned=cleaned,
            confidence=confidence,
            corrections=tuple(corrections),
            uncertain=uncertain,
        )


def likely_playback_echo(
    candidate: str,
    playback_references: list[str] | tuple[str, ...],
    *,
    playback_active: bool,
    milliseconds_since_playback: int = 0,
) -> bool:
    """Reject conservative near-verbatim playback during speech and a short tail."""

    if not playback_active and milliseconds_since_playback > 1800:
        return False
    heard = " ".join(re.findall(r"[a-z0-9]+", str(candidate or "").casefold()))
    if len(heard.split()) < 4:
        return False
    for reference in list(playback_references or [])[-6:]:
        spoken = " ".join(re.findall(r"[a-z0-9]+", str(reference or "").casefold()))
        if not spoken:
            continue
        ratio = SequenceMatcher(None, heard, spoken).ratio()
        containment = heard in spoken or spoken in heard
        heard_words = heard.split()
        spoken_words = set(spoken.split())
        overlap = sum(word in spoken_words for word in heard_words) / max(1, len(heard_words))
        if containment or ratio >= 0.88 or overlap >= 0.9:
            return True
    return False
