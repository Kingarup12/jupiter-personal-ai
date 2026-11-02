"""Camera Live visual-action intent, bounded pronoun/target resolution and preview-relative spatial wording.

Deterministic and model-free. It decides whether the user's CURRENT Live utterance explicitly asks Jupiter to put
something on the screen (mark / box / label / highlight / point out / locate), resolves "it" / "that" / "the one I'm
holding" from the last few Live utterances, and phrases the one-shot grounding result as trusted application context
for the SAME Live turn. Ordinary vision questions ("what am I holding?", "what colour is this?") are NONE, so they
never reach a paid spatial provider.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass, field

VISUAL_MARK = "visual_mark"
VISUAL_LOCATE = "visual_locate"
VISUAL_LABEL = "visual_label"
VISUAL_HIGHLIGHT = "visual_highlight"
VISUAL_POINT = "visual_point"
VISUAL_CLEAR = "visual_clear"          # "remove the box" — local only, never a provider call
NONE = "none"
PROVIDER_ACTIONS = frozenset({VISUAL_MARK, VISUAL_LOCATE, VISUAL_LABEL, VISUAL_HIGHLIGHT, VISUAL_POINT})

# Physical things a camera can plausibly locate. canonical -> surface forms (longest first when matching).
OBJECT_LEXICON: dict[str, tuple[str, ...]] = {
    "phone": ("mobile phone", "cell phone", "cellphone", "smartphone", "smart phone", "iphone", "android phone",
              "mobile", "phone"),
    "bottle": ("water bottle", "bottle", "flask"),
    "mouse": ("computer mouse", "mouse"),
    "cup": ("coffee cup", "cup", "glass of water"),
    "mug": ("mug",),
    "laptop": ("laptop", "notebook computer", "macbook"),
    "keyboard": ("keyboard",),
    "monitor": ("monitor", "screen monitor"),
    "television": ("television", "tv"),
    "remote": ("remote control", "remote"),
    "pen": ("pen", "pencil", "marker pen"),
    "book": ("book", "notebook", "diary"),
    "glasses": ("sunglasses", "spectacles", "glasses"),
    "headphones": ("headphones", "headphone", "headset", "earphones", "earbuds"),
    "charger": ("charger", "charging cable", "cable"),
    "watch": ("wristwatch", "smartwatch", "watch"),
    "wallet": ("wallet", "purse"),
    "keys": ("keys", "key"),
    "apple": ("apple",),
    "banana": ("banana",),
    "orange": ("orange",),
    "fruit": ("fruit",),
    "plant": ("plant", "flower pot", "flower"),
    "chair": ("chair",),
    "lamp": ("lamp",),
    "bag": ("backpack", "handbag", "bag"),
    "shoe": ("shoes", "shoe", "sneaker"),
    "hat": ("cap", "hat"),
    "ball": ("ball",),
    "toy": ("teddy bear", "toy"),
    "clock": ("clock",),
    "tablet": ("ipad", "tablet"),
    "camera": ("camera",),
    "scissors": ("scissors",),
    "knife": ("knife",),
    "spoon": ("spoon",),
    "fork": ("fork",),
    "plate": ("plate", "bowl"),
    "box": ("cardboard box", "carton", "package"),
    "pencil case": ("pencil case", "pencil box", "pencil pouch"),
    "stapler": ("stapler",),
    "calculator": ("calculator",),
    "notebook": ("notepad",),
    "eraser": ("eraser", "rubber"),
    "ruler": ("ruler",),
    "toothbrush": ("toothbrush",),
    "comb": ("comb", "hairbrush"),
    "towel": ("towel",),
    "pillow": ("pillow", "cushion"),
    "candle": ("candle",),
    "person": ("person", "man", "woman", "boy", "girl", "face"),
}
_SURFACES = sorted(((surface, canonical) for canonical, forms in OBJECT_LEXICON.items() for surface in forms),
                   key=lambda item: -len(item[0]))
_COLOURS = {"red", "blue", "green", "black", "white", "yellow", "pink", "purple", "orange", "grey", "gray", "brown",
            "silver", "gold", "golden", "dark", "light", "small", "big", "large", "little", "new", "old"}

# "it", "that", "the one I'm holding", "what I'm holding", "the phone you just mentioned" -> resolve from context.
_PRONOUN = re.compile(
    r"^(?:it|that|this|them|these|those|that one|this one|the one|that thing|this thing|the thing|the object|"
    r"that object|this object|the item|which one|which one you(?:'re| are) talking about|what you(?:'re| are) "
    r"talking about|the one you(?:'re| are) talking about|the one you (?:just )?mentioned|what you (?:just )?"
    r"mentioned|what you see|everything|all of them|all|those things)$")
_HELD = re.compile(r"^(?:what|the (?:thing|object|item|one)|whatever)\s+(?:i(?:'m| am)|i'm)\s+(?:holding|showing(?: you)?"
                   r"|carrying)(?: (?:up|now|right now|in my hand))?$|^(?:the (?:thing|object|one) in my hand)$")
_TRAILING = re.compile(
    r"\s+(?:(?:that|which)\s+)?(?:you (?:just )?(?:mentioned|said|named|talked about|described|see|can see)|"
    r"i(?:'m| am) (?:holding|showing(?: you)?)|in my hand|in the (?:frame|camera|picture|video|image|preview)|"
    r"on (?:the|my|your) (?:screen|display|preview|camera)|for me|please|now|right now|there|here|again|"
    r"so i can see(?: it)?|if you can|with a box|in a box)$")
_DETERMINER = re.compile(r"^(?:my|the|a|an|our|your|that|this|his|her|their|those|these)\s+")

_POLITE = r"(?:(?:hey |ok |okay |so |and |now |also |then |jupiter[, ]*)*(?:can|could|would|will) you (?:please )?" \
          r"|(?:please )|(?:go ahead and )|(?:i want you to )|(?:i'd like you to )|(?:jupiter[, ]+)|(?:now )|(?:and )|(?:then ))*"
_SHAPES = r"(?:box|bounding box|square|rectangle|circle|frame|outline|border|marker|highlight|label|tag)"

_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (VISUAL_CLEAR, re.compile(rf"^{_POLITE}(?:remove|clear|hide|delete|get rid of|take (?:away|off))\s+(?:the |that |all |those |these )?"
                              r"(?:boxes|box|marks?|markers?|labels?|highlights?|outlines?|overlay)\b")),
    (VISUAL_MARK, re.compile(rf"^{_POLITE}(?:make|put|draw|add|place|show|give me)\s+(?:a |an |the )?{_SHAPES}\s+"
                             r"(?:around|on|over|onto|for|at|near)\s+(?P<target>.+)$")),
    (VISUAL_MARK, re.compile(rf"^{_POLITE}(?:box|circle|outline|frame)\s+(?P<target>(?!around\b).+)$")),
    (VISUAL_MARK, re.compile(rf"^{_POLITE}(?:box|circle|outline)\s+around\s+(?P<target>.+)$")),
    (VISUAL_MARK, re.compile(rf"^{_POLITE}mark\s+(?:out\s+)?(?P<target>.+)$")),
    (VISUAL_HIGHLIGHT, re.compile(rf"^{_POLITE}(?:highlight|high light)\s+(?P<target>.+)$")),
    (VISUAL_LABEL, re.compile(rf"^{_POLITE}(?:label|tag|name)\s+(?P<target>.+?)(?:\s+on (?:the|my|your) (?:screen|display|preview))?$")),
    (VISUAL_POINT, re.compile(rf"^{_POLITE}point\s+(?:to|at|toward|towards)\s+(?P<target>.+)$")),
    (VISUAL_POINT, re.compile(rf"^{_POLITE}point\s+(?P<target>.+?)\s+out$")),
    (VISUAL_POINT, re.compile(rf"^{_POLITE}point out\s+(?P<target>.+)$")),
    (VISUAL_POINT, re.compile(rf"^{_POLITE}show me (?P<target>which one you(?:'re| are) (?:talking about|referring to)|"
                              r"which one you mean|the one you(?:'re| are) talking about|what you mean|"
                              r"which (?:\w+ )?you(?:'re| are) talking about)$")),
    (VISUAL_LOCATE, re.compile(rf"^{_POLITE}show me where\s+(?P<target>.+?)\s+(?:is|are)$")),
    (VISUAL_LOCATE, re.compile(rf"^{_POLITE}(?:locate|find|spot)\s+(?P<target>.+)$")),
    (VISUAL_LOCATE, re.compile(r"^(?:where(?:'s| is| are)|wheres)\s+(?P<target>.+?)(?:\s+(?:right now|now))?$")),
    (VISUAL_LOCATE, re.compile(rf"^{_POLITE}show me\s+(?P<target>(?:my|the)\s+.+)$")),
)
# Capability/meta questions and negations are NOT a request to mark something right now.
_NOT_A_REQUEST = re.compile(
    r"\b(?:how (?:do|can|would|could) (?:i|you)|what does|meaning of|don't|do not|never|stop|did you|have you|"
    r"were you able|are you able|is it possible)\b")
_GENERIC_OBJECTS = re.compile(r"^(?:things|objects|stuff|items|anything|something|boxes|stuff on (?:the )?screen)$")
_SIDE_QUERY = re.compile(
    r"\b(?:which|what) (?:side|part|corner)\b|\b(?:is|are) (?:it|that|this|they|the \w+|my \w+) (?:on|at|in) the "
    r"(?:left|right)\b|\bleft or (?:the )?right\b|\bright or (?:the )?left\b")
_FACE_MEMORY = re.compile(
    r"\b(?:remember|recogni[sz]e|know|memori[sz]e|save|store|enrol+(?:ed)?|registered)\b.{0,30}\b(?:my face|faces|"
    r"me|who i am|what i look like|my photo|this face|her face|his face|their faces?)\b|\bface recognition\b|"
    r"\bdo you know who (?:i am|this is)\b|\b(?:is|am) (?:my face|i) (?:enrolled|registered|saved)\b")
_ASR_FIXES = (
    # "Can you level it on screen?" -> "label it" (only in an object/pronoun + screen context).
    (re.compile(r"\blevel\s+(?=(?:it|that|this|them|the \w+|my \w+|what i(?:'m| am) holding)\b)"), "label "),
    (re.compile(r"\bbooks? (?=around\b)"), "box "),
    (re.compile(r"\bhigh light\b"), "highlight"),
    (re.compile(r"\bbounding box\b"), "box"),
)


def _normalize(text: str) -> str:
    value = " ".join(str(text or "").replace("’", "'").casefold().split())
    value = re.sub(r"[“”\"]", "", value)
    for pattern, replacement in _ASR_FIXES:
        value = pattern.sub(replacement, value)
    return value


def _clauses(text: str) -> list[str]:
    """Newest clause first: "What am I holding? Mark it." -> ["mark it", "what am i holding"]."""
    parts = [p.strip(" ,;:.!?") for p in re.split(r"(?<=[.!?;])\s+|\s*,\s*(?:and |then |now )?(?=(?:can|could|please|mark|box|label|highlight|point|show|where|locate|find)\b)", text)]
    return [p for p in reversed(parts) if p]


def clean_target(raw: str) -> str:
    value = raw.strip(" ,.!?;:")
    for _ in range(4):
        if _PRONOUN.match(value) or _HELD.match(value):
            return value
        value = _TRAILING.sub("", value).strip(" ,.!?;:")
    value = re.sub(r"\s+(?:on|in) (?:the |my |your )?(?:screen|display|preview|frame|camera)$", "", value)
    if _PRONOUN.match(value) or _HELD.match(value):
        return value
    value = _DETERMINER.sub("", value)
    value = _DETERMINER.sub("", value)
    return value[:60].strip()


def lexicon_object(phrase: str) -> tuple[str, str]:
    """-> (canonical, display phrase) of the LAST object noun in phrase, with its adjectives/brand words."""
    text = " ".join(str(phrase or "").split())
    lowered = text.casefold()
    best: tuple[int, str, str, int] | None = None
    for surface, canonical in _SURFACES:
        for match in re.finditer(rf"(?<![\w-]){re.escape(surface)}(?![\w-])", lowered):
            end = match.start() + len(surface)
            # The LAST mention wins; for overlapping forms the longest ("water bottle" over "bottle").
            if best is None or end > best[0] or (end == best[0] and len(surface) > best[3]):
                best = (end, canonical, surface, len(surface))
    if best is None:
        return "", ""
    end, canonical, surface, length = best
    start = end - length
    # Up to two preceding descriptive words: capitalised brand ("Motorola") or a colour/size adjective.
    before = text[:start].split()
    lead: list[str] = []
    for word in reversed(before[-2:]):
        bare = word.strip(",.;:!?\"'()")
        if bare and (bare[:1].isupper() and bare.casefold() not in {"i", "a", "an", "the", "it", "that", "this", "your", "my"}
                     or bare.casefold() in _COLOURS):
            lead.insert(0, bare)
        else:
            break
    display = " ".join(lead + [text[start:start + length]]).strip()
    return canonical, display


@dataclass
class VisualIntent:
    action: str = NONE
    target: str = ""               # cleaned phrase as spoken ("it", "water bottle", "Baishali")
    canonical: str = ""            # detector-facing noun ("phone"); "" for a person name / held object
    display: str = ""              # human label when context supplied one ("Motorola smartphone")
    held: bool = False             # "what I'm holding": the most prominent held object
    person: str = ""               # an explicitly named person (matched only against enrolled people)
    side_query: bool = False
    resolved_from_context: bool = False
    unresolved: bool = False

    @property
    def wants_provider(self) -> bool:
        return self.action in PROVIDER_ACTIONS and not self.unresolved


@dataclass
class VisualContext:
    """Bounded recent Live dialogue for pronoun resolution (never persisted; ~6 utterances / 3 minutes)."""
    max_items: int = 6
    ttl_s: float = 180.0
    clock: object = time.monotonic
    items: deque = field(default_factory=deque)
    last_target: tuple[str, str] = ("", "")

    def add(self, role: str, text: str) -> None:
        value = " ".join(str(text or "").split())[:600]
        if not value:
            return
        self.items.append((role, value, self.clock()))
        while len(self.items) > self.max_items:
            self.items.popleft()

    def recent(self) -> list[tuple[str, str]]:
        now = self.clock()
        return [(role, text) for role, text, at in reversed(self.items) if now - at <= self.ttl_s]

    def resolve(self) -> tuple[str, str]:
        """Most recent concrete object mentioned by Jupiter or the user (assistant first within a pair)."""
        for role, text in self.recent():
            canonical, display = lexicon_object(text)
            if canonical:
                return canonical, display
            if role == "assistant":
                canonical, display = named_object(text)
                if canonical:
                    return canonical, display
        return self.last_target


_NAMED_OBJECT = re.compile(
    r"(?:^|\b(?:is|it's|that's|this is|holding|showing|see|looks like|like)\s+)(?:a|an|your|the|some)\s+"
    r"(?P<np>(?:[\w'-]+\s+){0,3}?[\w'-]+)\s*(?:[.!?,]|$|\s+(?:with|in|on|that|which|and)\b)", re.I)
_NOT_OBJECT_WORDS = {"lot", "bit", "few", "moment", "second", "minute", "question", "way", "thing", "kind", "sort",
                     "look", "good", "great", "nice", "person", "man", "woman", "picture", "photo", "image", "video"}


def named_object(text: str) -> tuple[str, str]:
    """Generic fallback for a SHORT answer that names what the user holds ("A pencil case.", "That's a blue mug")."""
    value = " ".join(str(text or "").split())
    if not value or len(value.split()) > 12:
        return "", ""
    match = _NAMED_OBJECT.search(value)
    if not match:
        return "", ""
    words = [w.strip("'") for w in match.group("np").split()]
    while words and words[0].casefold() in _COLOURS:
        words.pop(0)                                    # "blue mug" -> detector target "mug", display keeps colour
    if not words or words[-1].casefold() in _NOT_OBJECT_WORDS:
        return "", ""
    return " ".join(words).casefold(), match.group("np")


def classify_visual_action(text: str, *, context: VisualContext | None = None,
                           enrolled_first_names: frozenset[str] = frozenset()) -> VisualIntent:
    normalized = _normalize(text)
    if not normalized:
        return VisualIntent()
    for clause in _clauses(normalized):
        if _NOT_A_REQUEST.search(clause):
            continue
        for action, pattern in _INTENT_PATTERNS:
            match = pattern.match(clause)
            if not match:
                continue
            if action == VISUAL_CLEAR:
                return VisualIntent(action=VISUAL_CLEAR)
            target = clean_target(match.group("target"))
            if not target or _GENERIC_OBJECTS.match(target):
                break                                   # "can you mark things on the screen?" = capability question
            intent = _resolve(action, target, clause, context, enrolled_first_names)
            if intent is not None:
                return intent
            break
    # "Which side of the screen is it on?" — a location question answered in PREVIEW coordinates.
    for clause in _clauses(normalized):
        if _SIDE_QUERY.search(clause) and not _NOT_A_REQUEST.search(clause):
            match = re.search(r"\b(?:is|are)\s+(?P<t>(?:my |the |your |that |this )?[\w' -]+?)\s+(?:on|at|in)\b", clause)
            target = clean_target(match.group("t")) if match else "it"
            intent = _resolve(VISUAL_LOCATE, target or "it", clause, context, enrolled_first_names, require_object=False)
            if intent is not None:
                intent.side_query = True
                return intent
    return VisualIntent()


def _resolve(action: str, target: str, clause: str, context: VisualContext | None,
             enrolled_first_names: frozenset[str], require_object: bool = True) -> VisualIntent | None:
    intent = VisualIntent(action=action, target=target)
    first = target.split()[0].casefold() if target.split() else ""
    if first and first in enrolled_first_names and len(target.split()) <= 3:
        intent.person = target.split()[0].title()
        return intent
    if target in {"me", "myself", "my face", "you"} and action != VISUAL_LOCATE:
        intent.canonical, intent.display = "person", "You"
        return intent
    if _HELD.match(target):
        intent.held = True
        if context is not None:
            intent.canonical, intent.display = context.resolve()
            intent.resolved_from_context = bool(intent.canonical)
        return intent
    if _PRONOUN.match(target):
        if context is not None:
            intent.canonical, intent.display = context.resolve()
        intent.resolved_from_context = bool(intent.canonical)
        if not intent.canonical:
            # "Mark it" with nothing mentioned yet: the most prominent object in view, never a guess by name.
            intent.held = True
        return intent
    canonical, display = lexicon_object(target)
    if canonical:
        intent.canonical, intent.display = canonical, display
        if context is not None:
            known, known_display = context.resolve()
            if known == canonical and len(known_display) > len(display):
                intent.display = known_display          # "the phone you mentioned" -> "Motorola smartphone"
        return intent
    if action == VISUAL_LOCATE:
        # "Where is Paris?" / "find a restaurant" are not camera requests: bare locate needs a physical object.
        if re.search(r"\b(?:on (?:the |my )?screen|in (?:the )?(?:frame|camera|picture|video|preview))\b", clause):
            intent.canonical, intent.display = target.split()[-1], target
            return intent
        return None
    if not require_object:
        return None
    # An explicit mark/box/label verb with an unlisted noun ("box the stapler") is still an explicit request.
    if re.fullmatch(r"[\w' -]{1,40}", target) and len(target.split()) <= 4:
        intent.canonical, intent.display = target.split()[-1], target
        return intent
    return None


def is_face_memory_question(text: str) -> bool:
    # "Do you know who I am?" asks about the signed-in account (answered from its registered name), not face enrollment.
    from services.identity_context import is_account_identity_question
    if is_account_identity_question(text):
        return False
    return bool(_FACE_MEMORY.search(_normalize(text)))


def screen_side(box: dict | None, *, mirrored: bool) -> tuple[str, str]:
    """(raw_side, display_side) for a canonical normalized box. Selfie preview: raw right <-> displayed left."""
    if not box:
        return "", ""
    cx = float(box.get("x", 0)) + float(box.get("w", 0)) / 2
    cy = float(box.get("y", 0)) + float(box.get("h", 0)) / 2

    def horizontal(x: float) -> str:
        return "left" if x < 0.4 else "right" if x > 0.6 else "center"

    vertical = "top" if cy < 0.33 else "bottom" if cy > 0.67 else ""

    def combine(h: str) -> str:
        if vertical and h != "center":
            return f"{vertical}-{h}"
        if vertical:
            return vertical
        return h

    return combine(horizontal(cx)), combine(horizontal(1 - cx if mirrored else cx))


def display_box(box: dict | None, *, mirrored: bool) -> dict | None:
    """Canonical normalized box -> preview-normalized box (display_x1 = 1 - raw_x2)."""
    if not box:
        return None
    x, w = float(box["x"]), float(box["w"])
    return {"x": round(1 - (x + w), 4) if mirrored else x, "y": box["y"], "w": w, "h": box["h"]}


_ACTION_VERB = {VISUAL_MARK: "marked", VISUAL_HIGHLIGHT: "highlighted", VISUAL_LABEL: "labelled",
                VISUAL_POINT: "pointed out", VISUAL_LOCATE: "marked"}
# Sent as ONE application turn after Jupiter held back the provider's own draft for this request, so the provider's
# single answer is based on what the app really did (never "I can't draw boxes" after a box, never "you should see the
# box" when nothing rendered).
_NOTE_HEAD = ("(Jupiter app result for the user's request just now: trusted application state, not the user speaking. "
              "Answer that request now in one short, natural reply in the conversation's language. Never read this "
              "note aloud and never mention notes, JSON, coordinates, boxes' numbers or tools.)\n")


def grounding_context_text(result: dict, *, rendered: bool | None = None) -> str:
    """Trusted result for the request. `rendered` is the user's screen confirming the box is actually visible."""
    what = result.get("displayed_label") or result.get("target") or "it"
    side = result.get("display_side") or ""
    mirrored = bool(result.get("mirrored"))
    orientation = (" The user's preview is a mirrored selfie view: when you talk about left/right on THEIR screen, "
                   "use the side given here, not the side in your raw camera frame." if mirrored else "")
    reason = result.get("reason") or ""
    where = f" It is on the {side} of the user's screen as they see it." if side else ""
    if result.get("success") and result.get("box_displayed") and rendered:
        verb = _ACTION_VERB.get(result.get("action"), "marked")
        if result.get("side_query"):
            return (_NOTE_HEAD + f"Jupiter measured where the {what} is and a temporary box is now VISIBLE on the user's "
                    f"screen (confirmed).{where}{orientation} Answer with that screen side and briefly say it is marked.")
        return (_NOTE_HEAD + f"A temporary box labelled \"{result.get('overlay_label') or what}\" is now VISIBLE around "
                f"the {what} on the user's screen (the screen confirmed it).{where}{orientation} Confirm briefly, for "
                f"example: \"Done, I've {verb} the {what} on your screen.\" Never say you cannot draw, box, mark, "
                "label or highlight things.")
    if result.get("success") and result.get("box_displayed"):
        sided = (f" Tell the user it is on the {side} of their screen as they see it." if side and result.get("side_query")
                 else "")
        return (_NOTE_HEAD + f"Jupiter found the {what} in the camera frame, but the box did NOT appear on the user's "
                f"screen.{sided}{orientation} Say briefly that you found it but the box didn't render correctly. Do not "
                "say it is marked or that they should see a box.")
    if reason == "camera_off":
        return _NOTE_HEAD + "The camera is off, so Jupiter could not mark anything; NO box is shown. Say so briefly."
    if reason == "not_enrolled":
        return (_NOTE_HEAD + f"\"{result.get('target')}\" is not an explicitly enrolled known person, and Jupiter only "
                "recognises people the user has enrolled. NO box is shown. Say so briefly; never guess who anyone is.")
    if reason == "person_not_found":
        return (_NOTE_HEAD + f"{result.get('target')} is enrolled, but Jupiter did not detect them in the current camera "
                "frame. NO box is shown. Say briefly that you can't see them right now.")
    if reason in {"unavailable", "timeout", "quota", "no_frame", "throttled"}:
        return (_NOTE_HEAD + "Screen marking is not available right now (the spatial marking service could not run). "
                f"NO box is shown.{orientation} You may still describe what you can see, but say briefly that marking "
                "it on the screen isn't available right now. Do not claim anything was marked.")
    unresolved = " Jupiter could not tell which object the user meant; ask which one." if reason == "unresolved" else ""
    return (_NOTE_HEAD + f"Jupiter could NOT confidently locate the {what} in the current camera frame, so NO box is "
            f"shown.{unresolved}{orientation} Say briefly that you couldn't locate it confidently and suggest moving it "
            "a little closer or into view. Do not claim anything was marked, and do not invent a position.")


def clear_context_text() -> str:
    return _NOTE_HEAD + "Jupiter removed the temporary box from the user's screen. Acknowledge briefly."


def face_memory_context_text(*, enrolled_count: int, self_enrolled: bool, self_label: str = "",
                             account_name: str = "") -> str:
    state = (f"The user's own face IS enrolled (shown as \"{self_label}\")." if self_enrolled
             else "The user's own face is NOT currently enrolled.")
    return (_NOTE_HEAD + "Jupiter has an explicit, consent-based known-person recognition feature for people the user "
            f"has enrolled. {state} Enrolled people in total: {int(enrolled_count)}. Answer from this state (for "
            "example \"I don't currently have your face enrolled for recognition\" or \"Yes, you're enrolled in my "
            "known-person recognition\"). Never claim you can never remember faces, and never imply you can recognise "
            "strangers or anyone who is not enrolled."
            + (f" Face enrollment is only about recognising faces on camera: the signed-in account belongs to "
               f"{json.dumps(str(account_name)[:40], ensure_ascii=False)}, so you still know who the account holder is by name "
               "(unless the conversation shows someone else is speaking)." if str(account_name or "").strip() else ""))


def orientation_context_text(*, mirrored: bool) -> str:
    if not mirrored:
        return ""
    return (_NOTE_HEAD + "The user's camera preview is a mirrored selfie view while your camera frame is unmirrored. "
            "Answer the user's left/right question as THEY see their screen: swap the left/right you see in your "
            "frame (your frame's right is the left of the user's screen).")
