"""Marker 203: "stay quiet until I say your name" in Live conversations.

Real WT 17 Oct 2026: the user said "whenever I say your name, only then respond ... I'm doing my homework", Jupiter said
"Got it!", then answered every sentence it overheard ("Achha, main samajh gayi! ... jootey ..."). Live answers every
turn it hears, so a spoken promise was not enough. Now the APP owns it: in quiet mode a turn that does not name Jupiter
gets no reply at all (the provider's draft is discarded and the turn closes silently); a turn that says "Jupiter" is
answered normally; "you can talk normally now" ends the mode. Deterministic, model-free, per Live session.
"""

from __future__ import annotations

import re

_FLAGS = re.IGNORECASE
# The wake name as Live's transcription writes it (English, Hindi, Bengali; "Jupyter" is a common mishearing).
WAKE_NAME = re.compile(r"\bj[uo]p[iy]t[ae]r\b|जुपिटर|ज्यूपिटर|जूपिटर|জুপিটার|জুপিটর", _FLAGS)

_QUIET_ON = re.compile(
    r"\b(?:only|just)\s+(?:respond|reply|answer|speak|talk)\b.{0,40}\b(?:when(?:ever)?|if)\s+i\s+(?:will\s+)?(?:say|call|use)\s+"
    r"(?:your\s+name|you|jupiter|j[uo]p[iy]t[ae]r)"
    r"|\b(?:when(?:ever)?|if)\s+i\s+(?:will\s+)?(?:say|call)\s+(?:your\s+name|jupiter|you)\b.{0,40}\b(?:only\s+then|then\s+only|then)\s+"
    r"(?:respond|reply|answer|speak|talk)"
    r"|\b(?:don'?t|do\s+not|not\s+to)\s+(?:speak|talk|respond|reply|say\s+anything|answer)(?:\s+anything)?\b.{0,40}"
    r"\b(?:until|unless|till)\s+i\s+(?:say|call)"
    r"|\b(?:stay|be|keep)\s+(?:quiet|silent)\b.{0,30}\b(?:until|unless|till)\s+i\b"
    r"|\b(?:i\s+(?:already\s+)?told\s+you\s+)?not\s+to\s+(?:speak|talk|say)\s+anything\b"
    r"|\b(?:don'?t|do\s+not)\s+(?:speak|talk|say\s+anything)\b.{0,30}\b(?:i'?m|i\s+am)\s+(?:doing|working|studying|busy)"
    r"|\b(?:quiet|silent)\s+mode\s+(?:on|chalu)\b|\bgo\s+(?:quiet|silent)\b"
    r"|\bjab\s+tak\b.{0,25}\bnaam\b.{0,20}\b(?:na|nahi)\b|\bnaam\s+(?:lu|loon|lun|lenge)\s+tabhi\s+(?:bolna|jawab)"
    # Marker 205 (real WT 23 Oct): "जब तक तुम्हारा नाम ना लूं तब तक रिप्लाई मत करना", "tum abhi kuch mat bolo, shant
    # raho", "Don't reply anything ... Just shut up" and "go to sleep" were all missed, so Jupiter kept answering.
    # Plain "be quiet" requests count too: Jupiter then answers only turns that say "Jupiter" (as with "until I say
    # your name"), and its one-line confirmation tells the user how to wake it. (No \b around Devanagari: its vowel
    # signs are not word characters.)
    r"|जब\s+तक.{0,40}नाम.{0,30}(?:ना|न|नहीं)\s|नाम\s+(?:लूं|लूँ|लू|लूंगा|लूँगा|लेंगे)\s+तभी"
    r"|(?:रिप्लाई|जवाब|उत्तर)\s+(?:मत|ना|न)\s+(?:करना|करो|करें|देना|दो)|(?:कुछ\s+)?मत\s+बोलो|मत\s+बोलना"
    r"|चुप\s+(?:रहो|रहना|हो\s+जाओ|हो\s+जा)|शांत\s+(?:रहो|रहना|हो\s+जाओ)"
    r"|\b(?:kuch\s+)?mat\s+bol(?:o|na)\b|\bchup\s+(?:raho|rehna|ho\s+jao|ho\s+ja)\b|\bshaant?\s+(?:raho|rehna)\b"
    r"|\breply\s+mat\s+kar(?:na|o)\b|\bjawab\s+mat\s+(?:do|dena)\b"
    r"|\b(?:don'?t|do\s+not)\s+(?:reply|respond|answer)\b|\bshut\s+up\b|\bstop\s+(?:talking|speaking|replying)\b"
    r"|\b(?:be|stay|keep)\s+(?:quiet|silent)\b|\bgo\s+to\s+sleep\b", _FLAGS)
_QUIET_OFF = re.compile(
    r"\b(?:you\s+can|now\s+you\s+can|feel\s+free\s+to)\s+(?:talk|speak|respond|reply)(?:\s+(?:normally|again|now))?\b"
    r"|\b(?:stop|end|exit|turn\s+off|cancel)\s+(?:the\s+)?(?:quiet|silent)\s+mode\b|\b(?:quiet|silent)\s+mode\s+off\b"
    r"|\bnormal\s+mode\b|\bab\s+(?:tum\s+)?bol\s+(?:sakte|sakti)\s+ho\b|\bwake\s+up\b"
    # Marker 206 (real WT 23 Oct: "Jupiter jag jao" was answered, then Jupiter went back to sleep)
    r"|\b(?:jaa?g|uth)\s+(?:jao|ja|jaiye|jaaiye|gayi|gaye)\b|जाग\s*(?:जाओ|जा|जाइए)|उठ\s*(?:जाओ|जा)|उठो", _FLAGS)
# "Only respond WHEN(EVER) I say your name" is a standing rule; "sleep / don't reply UNTIL I say your name" ends when
# the user says the name (real WT 23 Oct: "I once command to go into sleep and it is keep going into sleep").
_QUIET_STANDING = re.compile(
    r"\b(?:only|just)\s+(?:respond|reply|answer|speak|talk)\b|\bwhenever\b|\bjab\s+bhi\b|\bsirf\s+(?:tab|jab)\b"
    r"|जब\s+भी|सिर्फ\s+(?:तब|जब)", _FLAGS)


def _plain(text: str) -> str:
    """Live ASR drops stray full stops mid-sentence ("not to. speak anything"): punctuation is ignored."""
    return " ".join(re.sub(r"[.,!?;:।]+", " ", str(text or "").replace("’", "'")).split())


def asks_quiet_mode(text: str) -> bool:
    return bool(_QUIET_ON.search(_plain(text)))


def ends_quiet_mode(text: str) -> bool:
    return bool(_QUIET_OFF.search(_plain(text)))


def wakes_on_name(request: str) -> bool:
    """True when saying "Jupiter" ends quiet mode (sleep / until ...); False for a standing "only when" rule."""
    return not _QUIET_STANDING.search(_plain(request))


def names_jupiter(text: str) -> bool:
    return bool(WAKE_NAME.search(str(text or "")))


QUIET_ON_NOTE = ("APP RESULT (Jupiter's app; reference data, not user speech): quiet mode is ON. From now on Jupiter's "
                 "app keeps you silent for everything the user says unless they say “Jupiter”. Reply with ONE short "
                 "sentence in the user's language, for example “Okay, I'll stay quiet until you say Jupiter.” "
                 "Do not add anything else and do not mention this note.")
QUIET_OFF_NOTE = ("APP RESULT (Jupiter's app; reference data, not user speech): quiet mode is OFF; you answer every turn "
                  "again. Say one short sentence that you are back and listening, in the user's language. Do not mention "
                  "this note.")
