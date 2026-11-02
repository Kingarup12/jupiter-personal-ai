"""Generic "does this question need CURRENT external information?" detector.

Deterministic and product-agnostic: it never names a company, model or product. Real WT 29-Sep-2026 22:42:
"tell me about claude code new agent opus 5.5" was classified FAST_CODING (the word "code") and answered from stale
model knowledge with no web search. A request that asks ABOUT something new/recent/released must reach the
provider-grounded web path instead, while imperative build/fix requests ("write a new function …") stay coding.
"""

from __future__ import annotations

import re

# Asking about something, not asking Jupiter to produce something.
_INFO_FRAME = re.compile(
    r"(?i)\b(?:tell\s+me\s+(?:more\s+)?about|what(?:'s|\s+is|\s+are|\s+was|\s+were)|who(?:'s|\s+is|\s+are|\s+won|\s+leads)"
    r"|when\s+(?:is|was|did|does|will)|is\s+there|are\s+there|have\s+you\s+heard|any\s+(?:news|updates?|info)"
    r"|explain|info(?:rmation)?\s+(?:on|about)|details?\s+(?:on|about)|overview\s+of|summari[sz]e\s+the"
    r"|how\s+(?:good|fast|much|big)\s+is|compare)\b")
_QUESTION = re.compile(r"\?\s*$")
_BUILD_IMPERATIVE = re.compile(
    r"(?i)^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?(?:write|create|generate|build|implement|fix|debug|refactor"
    r"|optimi[sz]e|convert|add|make|code|design|draft|translate|rewrite|correct|solve|calculate)\b")
# Cues that essentially always mean "current information".
_STRONG_CUE = re.compile(
    r"(?i)\b(?:latest|newest|just\s+(?:released|announced|launched|dropped|came\s+out)|release\s+date|release\s+notes"
    r"|upcoming|what'?s\s+new|headlines?|trending|this\s+(?:week|month|year)'?s?|as\s+of\s+(?:today|now|20\d\d)"
    r"|current\s+(?:version|release|price|status|ceo|president|prime\s+minister|leader|champion|model|events?"
    r"|situation|standings)|(?:was|were|been|got|is|are)\s+(?:released|announced|launched|unveiled))\b")
# Cues that are current only inside an information-seeking question ("good news, I passed!" is not a search).
_SOFT_CUE = re.compile(r"(?i)\b(?:news|today|tonight|yesterday|recent(?:ly)?|right\s+now|currently|announced|launched"
                       r"|released|unveiled)\b")
# "new" is ambiguous ("a new variable"). It signals freshness next to a release-type noun, a version or a year.
_NEW_RELEASE = re.compile(
    r"(?i)\bnew\s+(?:\w+\s+){0,2}?(?:version|release|model|models|agent|agents|feature|features|update|updates|launch"
    r"|product|products|series|generation|gen|chip|phone|laptop|device|flagship|announcement|policy|law|rules|season"
    r"|episode|album|movie|film|game|app|tool|tools|framework|api|standard)\b"
    r"|\bnew\b[^.?!]{0,40}\bv?\d+(?:\.\d+)+\b|\bv?\d+(?:\.\d+)+\b[^.?!]{0,40}\bnew\b")
_RECENT_YEAR = re.compile(r"\b20(?:2[5-9]|[3-9]\d)\b")
# The user's OWN data ("check my latest email", "my recent messages") is answered by its tool, never by web search.
_PERSONAL_DATA = re.compile(
    r"(?i)\b(?:e-?mails?|inbox|gmail|mails?|messages?|texts?|dms?|chats?|calendar|meetings?|appointments?|reminders?"
    r"|notifications?|to-?dos?)\b|\bmy\s+(?:\w+\s+){0,2}?(?:files?|documents?|docs|notes|photos?|pictures?|images?"
    r"|orders?|account|contacts?|tasks?|schedule|plans?|memories|history|settings|profile)\b")


# Advice for the user ("what should I eat for dinner tonight?", "suggest a movie for today", "aaj kya pehnu?") is answered
# from knowledge; "tonight" / "today" there is not a request for current news (marker 221: auto-test WT 28 Oct, the
# dinner question fell to web search and replied "couldn't get verified web results").
_ADVICE = re.compile(
    r"(?i)^(?:so\s+|okay\s+|ok\s+|thanks\.?\s+|hey\s+|jupiter,?\s+)?(?:what\s+(?:should|can|could|shall)\s+i\b|what\s+do\s+you\s+"
    r"(?:suggest|recommend)|suggest|recommend|give\s+me\s+(?:some\s+)?(?:ideas?|tips?|suggestions?)|any\s+(?:ideas?|tips?|"
    r"suggestions?)\s+for|how\s+(?:should|can|do)\s+i\b|should\s+i\b|aaj\s+kya\s+(?:karu|karun|khau|khaun|pehnu|banau|"
    r"dekhu)|kya\s+(?:karu|karun|khau|khaun|pehnu|banau|dekhu)\b)")


def is_fresh_information_request(text: str) -> bool:
    """True when answering well needs current external information (web search), not model memory."""
    message = " ".join(str(text or "").split())
    if not message or _BUILD_IMPERATIVE.search(message) or _PERSONAL_DATA.search(message):
        return False
    asking = bool(_INFO_FRAME.search(message) or _QUESTION.search(message))
    if _STRONG_CUE.search(message):
        return asking or len(message.split()) <= 10
    if _ADVICE.search(message):
        return False
    if _NEW_RELEASE.search(message) or _RECENT_YEAR.search(message) or _SOFT_CUE.search(message):
        return asking
    return False
