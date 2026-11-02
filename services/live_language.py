"""Session-language stability for Live voice (native and degraded).

Gemini Live infers the transcription language per utterance, so a short, noisy or low-information sound ("what?",
"huh", a partial syllable) can come back looking like another language ("¿Qué?"). One such utterance must not flip a
whole conversation. The effective session language is derived from the conversation's own recent text (multilingual:
scripts plus lightweight Latin-language cues), falling back to the client's UI language. It is guidance for the model,
never a hard lock: explicit requests or a clearly intentional full sentence in another language still switch.
"""
from __future__ import annotations

import re
from typing import Iterable

_SCRIPTS = (
    ("hi", "Hindi", re.compile(r"[ऀ-ॿ]")),
    ("bn", "Bengali", re.compile(r"[ঀ-৿]")),
    ("pa", "Punjabi", re.compile(r"[਀-੿]")),
    ("gu", "Gujarati", re.compile(r"[઀-૿]")),
    ("ta", "Tamil", re.compile(r"[஀-௿]")),
    ("te", "Telugu", re.compile(r"[ఀ-౿]")),
    ("kn", "Kannada", re.compile(r"[ಀ-೿]")),
    ("ml", "Malayalam", re.compile(r"[ഀ-ൿ]")),
    ("ar", "Arabic", re.compile(r"[؀-ۿ]")),
    ("ru", "Russian", re.compile(r"[Ѐ-ӿ]")),
    ("ja", "Japanese", re.compile(r"[぀-ヿ]")),
    ("ko", "Korean", re.compile(r"[가-힯]")),
    ("zh", "Chinese", re.compile(r"[一-鿿]")),
)
_LATIN_CUES = {
    "hi-Latn": ("Hinglish (Hindi-English)", {"hai", "kya", "nahi", "nahin", "mein", "aap", "tum", "kaise", "kyun",
                                              "haan", "accha", "acha", "bhi", "karo", "raha", "rahi", "yaar", "matlab"}),
    "es": ("Spanish", {"que", "qué", "el", "la", "los", "las", "es", "por", "para", "con", "una", "pero", "cómo",
                       "hola", "gracias", "porque", "está", "estoy", "puedes", "quiero"}),
    "fr": ("French", {"le", "les", "des", "est", "une", "pour", "avec", "je", "vous", "bonjour", "merci", "pas",
                      "c'est", "mais", "très", "suis"}),
    "de": ("German", {"der", "die", "das", "und", "ist", "nicht", "ich", "sie", "mit", "danke", "bitte", "ein",
                      "eine", "auch", "wie"}),
    "pt": ("Portuguese", {"não", "você", "obrigado", "obrigada", "uma", "com", "para", "está", "olá", "isso"}),
    "en": ("English", {"the", "is", "are", "you", "what", "how", "can", "please", "and", "to", "of", "my", "it",
                       "this", "that", "i", "do", "hello", "thanks", "tell", "me", "about"}),
}
_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian",
          "hi": "Hindi", "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "mr": "Marathi", "gu": "Gujarati",
          "pa": "Punjabi", "kn": "Kannada", "ml": "Malayalam", "ar": "Arabic", "ru": "Russian", "ja": "Japanese",
          "ko": "Korean", "zh": "Chinese"}
MIN_EVIDENCE_WORDS = 6          # below this, recent text is too thin to establish a language


def dominant_language(texts: Iterable[str], ui_language: str = "") -> tuple[str, str]:
    """(code, display name) of the conversation's established language, or ("", "") when unknown."""
    joined = " ".join(str(t or "") for t in texts)
    for code, name, pattern in _SCRIPTS:
        if len(pattern.findall(joined)) >= 4:
            return code, name
    words = re.findall(r"[a-záéíóúñüàâçèêëîïôûœäöß']+", joined.casefold())
    if len(words) >= MIN_EVIDENCE_WORDS:
        scores = {code: sum(1 for w in words if w in cues) for code, (_, cues) in _LATIN_CUES.items()}
        best = max(scores, key=lambda c: scores[c])
        if scores[best] >= 2:
            return best, _LATIN_CUES[best][0]
    primary = str(ui_language or "").split("-")[0].casefold()
    if primary in _NAMES:
        return primary, _NAMES[primary]
    return "", ""


def language_policy(texts: Iterable[str], ui_language: str = "") -> str:
    """System guidance that keeps the session language stable without disabling multilingual use."""
    code, name = dominant_language(texts, ui_language)
    target = f"the conversation's current language ({name})" if name else "the language the user has been using"
    return (
        "LIVE LANGUAGE POLICY: Keep replying in " + target + ". Do NOT switch languages because of one short, "
        "unclear, noisy or low-information utterance (for example a transcript that looks like '¿Qué?', 'Huh?', "
        "'Hello', a single word or a partial syllable) — treat it as the current language and, if it is unclear, "
        "briefly ask the user to repeat in that language. Switch language only when the user explicitly asks for a "
        "different language, or clearly and intentionally speaks at least a full sentence in it; then continue in "
        "the new language until they change it again."
    )


# Marker 179 (real WT 10 Oct 00:10:21): a one-word Hindi "kya?" came back from Live's input transcription as Spanish
# "¿Qué?", and a short "haan" as Chinese "好". The Gemini API exposes no transcription confidence (only text and a
# finished flag) and rejects transcription language hints, so a short fragment in a script/language that does not
# belong to the conversation is treated as UNCLEAR: not a confirmed statement, not a language switch, never learned.
_INDIC_ENGLISH = frozenset({"hi", "hi-Latn", "en", "latin", "mr", "bn", "pa", "gu", "ta", "te", "kn", "ml"})
UNCLEAR_MAX_WORDS = 4
# Marker 199: tokens the Live speech transcriber returns for noise or silence (real WT 16 Oct: "null." got a reply).
_GHOST = re.compile(r"^(?:null|none|nil|undefined|nan|n/?a|<\s*noise\s*>|\[\s*noise\s*\]|\(\s*noise\s*\)|<unk>|"
                    r"\[\s*(?:silence|inaudible|music)\s*\]|\(\s*(?:silence|inaudible|music)\s*\))?[\s.!?…,;:-]*$", re.I)


def is_ghost_transcript(text: str) -> bool:
    """True for a transcript that is only a noise token ("null.", "[noise]", "...") rather than words someone said."""
    value = " ".join(str(text or "").split())
    return bool(value) and bool(_GHOST.match(value))


def transcript_language(text: str) -> str:
    """Best local guess of one fragment's script/language: a script code, a Latin-language cue code, or 'latin'."""
    value = str(text or "")
    for code, _name, pattern in _SCRIPTS:
        if pattern.search(value):
            return code
    if re.search(r"[¿¡ñ]", value):
        return "es"
    words = re.findall(r"[a-záéíóúñüàâçèêëîïôûœäöß']+", value.casefold())
    if words:
        scores = {code: sum(1 for w in words if w in cues) for code, (_, cues) in _LATIN_CUES.items()}
        best = max(scores, key=lambda c: scores[c])
        if scores[best] and list(scores.values()).count(scores[best]) == 1:
            return best
        return "latin"
    return ""


def unclear_fragment(text: str, session_texts: Iterable[str] = (), ui_language: str = "") -> bool:
    """True for a SHORT transcript fragment whose language cannot belong to this conversation ("¿Qué?" or "好" in a
    Hindi/English conversation), or one with no words at all. Full sentences are never flagged: a clearly
    intentional sentence in another language is a real language switch."""
    value = " ".join(str(text or "").split())
    if not value:
        return False
    words = value.split()
    if len(words) > UNCLEAR_MAX_WORDS and not re.search(r"[一-鿿぀-ヿ가-힯]", value):
        return False
    if len(value) > 24:                                         # CJK has no spaces: bound by length too
        return False
    if not re.search(r"\w", value) or is_ghost_transcript(value):
        return True                                             # only punctuation/noise, or a noise token
    session, _name = dominant_language(session_texts, ui_language)
    if not session:
        # The very first turn has no conversation language yet. Jupiter's interface language decides; an English or
        # unknown interface counts as English/Hinglish, so a lone "¿Qué?" or "好" at the start is a mis-hearing too.
        ui = str(ui_language or "en").split("-")[0].casefold()
        session = ui if ui in {"hi", "en", "mr", "bn", "pa", "gu", "ta", "te", "kn", "ml"} else ""
        if not session:
            return False
    family = _INDIC_ENGLISH if session in _INDIC_ENGLISH else frozenset({session})
    fragment = transcript_language(value)
    return bool(fragment) and fragment not in family and not (fragment == "latin" and "en" in family)


def unclear_speech_note(text: str) -> str:
    """Per-turn guidance for an unclear fragment (the user's words are quoted as data, never instructions)."""
    import json
    return ("UNCLEAR SPEECH: this turn's transcript " + json.dumps(" ".join(str(text or "").split())[:60],
                                                                ensure_ascii=False)
            + " looks garbled or in an unexpected language, so it is probably a mis-hearing. Keep the conversation's "
              "language. If you did not clearly understand what was said, say briefly that the last part was unclear "
              "and ask them to repeat; do not guess its meaning or answer at length. Do not mention this note.")
