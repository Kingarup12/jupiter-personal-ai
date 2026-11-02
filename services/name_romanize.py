"""Rough Latin spelling of Devanagari / Bengali words, only for MATCHING saved names (marker 221).

Real auto-test WT 28 Oct: Live heard "a friend ka batch kya hai?" as "अर्पिता मंडल का बैच क्या है?", so the saved
person "a friend" never matched and Live said it didn't know. "अर्पिता मंडल" -> "a friend mandal" matches closely
enough. This is not a transliteration standard; it is a fuzzy key (inherent "a", final-schwa dropped).
"""

from __future__ import annotations

import re

_VOWELS = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
    "অ": "a", "আ": "a", "ই": "i", "ঈ": "i", "উ": "u", "ঊ": "u", "ঋ": "ri", "এ": "e", "ঐ": "oi", "ও": "o", "ঔ": "ou",
}
_SIGNS = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
    "া": "a", "ি": "i", "ী": "i", "ু": "u", "ূ": "u", "ৃ": "ri", "ে": "e", "ৈ": "oi", "ো": "o", "ৌ": "ou",
}
_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh",
    "स": "s", "ह": "h", "ড়": "r", "ঢ়": "rh", "য়": "y",
    "ক": "k", "খ": "kh", "গ": "g", "ঘ": "gh", "ঙ": "n", "চ": "ch", "ছ": "chh", "জ": "j", "ঝ": "jh", "ঞ": "n",
    "ট": "t", "ঠ": "th", "ড": "d", "ঢ": "dh", "ণ": "n", "ত": "t", "থ": "th", "দ": "d", "ধ": "dh", "ন": "n",
    "প": "p", "ফ": "ph", "ব": "b", "ভ": "bh", "ম": "m", "য": "j", "র": "r", "ল": "l", "শ": "sh", "ষ": "sh", "স": "s",
    "হ": "h",
}
_VIRAMA = {"्", "্"}
_NASAL = {"ं": "n", "ঁ": "n", "ং": "ng", "ँ": "n"}
_SKIP = {"़", "়", "ः", "ঃ"}
_NON_LATIN = re.compile(r"[ऀ-ॿঀ-৿]")


def has_indic(text: str) -> bool:
    return bool(_NON_LATIN.search(str(text or "")))


def _word(word: str) -> str:
    out: list[str] = []
    pending_a = False
    for ch in word:
        if ch in _CONSONANTS:
            if pending_a:
                out.append("a")
            out.append(_CONSONANTS[ch])
            pending_a = True
        elif ch in _SIGNS:
            out.append(_SIGNS[ch])
            pending_a = False
        elif ch in _VIRAMA:
            pending_a = False
        elif ch in _NASAL:
            if pending_a:
                out.append("a")
                pending_a = False
            out.append(_NASAL[ch])
        elif ch in _VOWELS:
            if pending_a:
                out.append("a")
                pending_a = False
            out.append(_VOWELS[ch])
        elif ch in _SKIP:
            continue
        else:
            if pending_a:
                out.append("a")
                pending_a = False
            out.append(ch)
    return "".join(out)                                 # the final inherent "a" is dropped (schwa deletion)


def romanize(text: str) -> str:
    value = str(text or "")
    if not has_indic(value):
        return value
    return " ".join(_word(word) for word in value.split())
