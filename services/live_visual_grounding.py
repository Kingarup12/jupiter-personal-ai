"""Per-Live-session orchestration of APPLICATION-OWNED camera turns (on-demand grounding, face-memory truth).

Real WT evidence (marker 151): gemini-3.8-live delivers the input transcription only ~300 ms AFTER activity_end, and
its first output ~0.8-1.5 s after it. So the intent can only be known once the provider already owns the turn. The
canonical flow is therefore:

    activity_end -> provider starts its own draft -> input transcript arrives (~300 ms)
    -> deterministic intent (visual action / screen side / face memory / remove box)
    -> the transport HOLDS BACK the provider's draft for this turn (never forwarded: no "I can't draw boxes", no raw
       box JSON, no premature "you should see the box")
    -> ONE bounded grounding job on the latest in-memory canonical frame
    -> overlay event to the client -> the client RENDERS the box and CONFIRMS it (render handshake)
    -> one trusted application result is sent to the provider as a client turn
    -> the provider's single answer for the SAME logical turn, based on what really happened on screen.

Ordinary camera conversation never calls a spatial provider. Frames live only in memory (the latest one), are dropped
when the camera turns off, and are never logged or stored. Telemetry is labels/numbers only.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from services.visual_intent import (
    VISUAL_CLEAR, VisualContext, VisualIntent, classify_visual_action, clear_context_text, face_memory_context_text,
    grounding_context_text, is_face_memory_question, orientation_context_text,
)

GROUNDING_TIMEOUT_S = 3.5
RENDER_CONFIRM_TIMEOUT_S = 2.0      # the client renders within a frame or two; beyond this the box is "not visible"
FRAME_WAIT_S = 1.2                  # one bounded wait when the camera is on but no frame has arrived yet
FRAME_FRESH_S = 6.0
MAX_TURN_RESULTS = 32
_VISUAL_WORDS = re.compile(r"\b(?:mark|box|label|level|tag|highlight|circle|outline|point|locate|find|where|show|"
                           r"which (?:side|one)|screen|(?:on|to) the (?:left|right)|left or right)\b", re.I)
# Spatial left/right phrasing only ("all right", "right?" and "that's right" are not spatial questions).
_SIDE_WORDS = re.compile(r"\b(?:which side|left or right|right or left|(?:on|to|at) the (?:left|right)|"
                         r"(?:left|right)(?:-hand)? side|(?:top|bottom|upper|lower)[- ](?:left|right))\b", re.I)

APP_VISUAL, APP_CLEAR, APP_FACE_MEMORY, APP_ORIENTATION = "visual", "clear", "face_memory", "orientation"
APP_PERSONAL = "personal"            # marker 174: remember / recall / forget / reminders spoken in Live
APP_STOP = "stop"                    # marker 179: a turn that is ONLY "stop / be quiet / chup / bas karo"
APP_DEVICE = "device"                # marker 197: volume / media / screen marks / phone task, done by the Windows app
APP_DEVICE_UNRECOGNISED = "device_unrecognised"   # sounds like an action but matched none: nothing was done
APP_WEB = "web_search"               # marker 203: "search the web for X" / questions about something current
APP_QUIET_ON, APP_QUIET_OFF = "quiet_on", "quiet_off"   # marker 203: "only respond when I say your name"
APP_SILENT = "silent"                # quiet mode: a turn that does not name Jupiter gets no reply at all
APP_ADDRESSEE = "addressee"          # marker 213: might be overheard: checked on the whole sentence before any reply
APP_SIDE_TALK_ON = "side_talk_on"    # marker 213: "we're talking to each other; reply when we talk to you"
def join_continuation(text: str) -> str:
    """"Jupiter, can you open. Spotify." -> "Jupiter, can you open Spotify." (a sentence that ended on its verb, then
    the rest after a pause). Other sentences are left as they are."""
    parts = re.split(r"(?<=[.!?…])\s+", " ".join(str(text or "").split()))
    out: list[str] = []
    for part in parts:
        if out and _AWAITS_OBJECT.match(out[-1]):
            out[-1] = out[-1].rstrip(".!?… ") + " " + part
        else:
            out.append(part)
    return " ".join(out)
APP_WAIT_MORE = "wait_more"          # marker 217: "Jupiter, can you open." - the user is still saying what
WAIT_MORE_S = 3.0
# Marker 217 (real WT 25 Oct: "Jupiter, can you open." was answered "What would you like me to open?" while the user
# was still saying "the settings"): native Live transcribes only after the turn ends, so the client cannot hear that the
# sentence is unfinished. A request ending on its verb waits a moment; more speech continues the SAME turn.
_AWAITS_OBJECT = re.compile(r"^(?:[\w',]+\s+){0,8}?(?:open|play|close|launch|start|search(?:\s+for)?|look\s+up|find|"
                            r"show\s+me|tell\s+me\s+about|type|write|call|message|send|maximi[sz]e|minimi[sz]e|"
                            r"go\s+to|switch\s+to|turn\s+(?:on|off)|set|increase|decrease)(?:\s+(?:the|a|an|my))?"
                            r"\s*[.!?…]*$", re.IGNORECASE)
_DO_IT = re.compile(r"^(?:(?:okay|ok|haan|han|yes|yeah|ji|so|now|then|please|jaldi)[\s,.!]+)*"
                    r"(?:do\s+it|go\s+ahead|please\s+do|proceed|kar\s*do|karo|kar\s+dijiye|kijiye|chalo\s+karo|"
                    r"karke\s+dikhao|करो|कर\s*दो|कीजिए)(?:\b|$).{0,40}$", re.IGNORECASE)
# Marker 217 (real WT 25 Oct: "Play play play. I'm waiting for you." right after the request was dropped): the user
# insisting means the previous request was for Jupiter, even if it had been taken for side talk.
_INSIST = re.compile(r"^(?:(?:play|okay|ok|hello|jupiter|arre|are|abe|please)[\s,.!]+)*(?:play|chalao|bajao|lagao)"
                     r"(?:[\s,.!]+(?:play|chalao|bajao|lagao|it|na|now|please))*"
                     r"(?:[\s,.!]+(?:i'?m|i\s+am)\s+waiting(?:\s+for\s+(?:you|it))?)?$|"
                     r"^(?:(?:hello|okay|ok|jupiter)[\s,.!]+)*(?:i'?m|i\s+am)\s+(?:still\s+)?waiting(?:\s+for\s+(?:you|it))?"
                     r"|^(?:kab\s+karoge|kab\s+karogi|jaldi\s+karo|kya\s+hua|abhi\s+tak\s+nahi\s+hua)$", re.IGNORECASE)
_OPEN_SETTINGS = re.compile(r"\b(?:open|launch|start|show|kholo|khol\s*do)\s+(?:up\s+)?(?:the\s+|my\s+)?settings?\b"
                            r"(?!\s+(?:of|for|in|inside)\b)(?:\s+(?:app|window|page|please|for\s+me|now))*\s*[.!?]*$"
                            r"|\bsettings?\s+(?:kholo|khol\s*do|open\s+karo)\b", re.IGNORECASE)
_JUPITER_SETTINGS = re.compile(r"\b(?:jupiter|your|app'?s?|chat|account|profile|voice|live)\s+settings?\b", re.IGNORECASE)
_TRY_AGAIN = re.compile(r"^(?:(?:okay|ok|so|now|then|please|yes|yeah|haan|han|arre|achha)[\s,.!]+)*"
                        r"(?:just\s+)?(?:do\s+it|try\s+(?:it\s+)?again|go\s+ahead|retry|kar\s*do|phir\s+se\s+(?:try\s+)?"
                        r"kar\w*|dobara\s+kar\w*)\b", re.IGNORECASE)
# Marker 217: while Jupiter's music plays, a clear command naming its target is said to Jupiter (lyrics never name
# Spotify, a tab or the volume); only the rest goes to the side-talk check.
_NAMED_TARGET = re.compile(r"\b(?:spotify|youtube|chrome|edge|browser|window|tab|volume|awaaz|aawaz|brightness|song|"
                           r"songs|gaana|gaane|gana|music|track|playlist|jupiter)\b", re.IGNORECASE)
WEB_SEARCH_TIMEOUT_S = 10.0
DEVICE_RESULT_TIMEOUT_S = 15.0
# Marker 217: a computer task is given up to 20 s to FINISH inside the turn, so Live says what really happened
# ("Done: 10 apps are open...") instead of "the results will appear on your screen".
COMPUTER_TASK_RESULT_TIMEOUT_S = 26.0
FOLLOW_UP_SECONDS = 90.0             # "now set it to 40%" counts as a volume request this long after one       # a screen mark = one capture + one vision call; phone tasks report "started"

# The WHOLE utterance must be a stop request (optionally with "okay", "Jupiter", "please", "yaar"). "Stop, and tell me
# about X" or "wait, what?" are new requests and stay ordinary turns that the model answers.
_STOP_ONLY = re.compile(
    r"^(?:(?:ok(?:ay)?|jupiter|please|plz|hey|arre|acha|achha)[\s,.!]+)*"
    r"(?:stop(?:\s+(?:it|talking|speaking|now|please))?|please stop|be quiet|quiet|shut up|(?:that'?s\s+)?enough|"
    r"hold on|wait|pause|silence|shh+|ruko|ruk jao|ruk ja|thoda ruko|ek (?:minute|second) ruko|chup|chup karo|"
    r"chup ho jao|chup raho|bas karo|bas bas|bas|rehne do|mat bolo|bolna band karo|"
    r"रुको|रुक जाओ|थोड़ा रुको|चुप|चुप करो|चुप हो जाओ|चुप रहो|बस|बस करो|बस बस|रहने दो|मत बोलो)"
    r"(?:[\s,.!]+(?:please|plz|jupiter|yaar|na|abhi|now|ab))*[\s.!।]*$", re.I)


_FORGET_MEMORY = re.compile(
    r"\b(?:remove|delete|forget|erase|wipe)\b.{0,40}\b(?:memory|memories|yaad)\b"
    r"|\b(?:memory|yaad)\b.{0,25}\b(?:remove|delete|hata\w*|nikal\w*|mita\w*)\b"
    r"|मेमोरी.{0,25}(?:रिमूव|हटा|डिलीट|मिटा|निकाल)|(?:रिमूव|हटा|डिलीट|मिटा).{0,25}मेमोरी"
    r"|याद(?:दाश्त)?.{0,15}(?:हटा|मिटा|निकाल)", re.I)
_FORGET_PRONOUN = re.compile(r"\b(?:it|that|this|usko|usse|use|isko|ise|wo|woh|vo)\b|उसको|उसे|इसको|इसे|\bवो\b|\bवह\b",
                             re.I)


def is_stop_only(text: str) -> bool:
    return bool(_STOP_ONLY.match(" ".join(str(text or "").split())[:80]))


STOP_RESULT_NOTE = ("APP RESULT: the user just told you to stop talking. Say only “Okay” (or its one-word equivalent "
                    "in the conversation's language, such as “Theek hai”) and nothing else. Do not continue, repeat "
                    "or summarise your earlier answer. Do not mention this note.")


@dataclass
class AppIntent:
    kind: str
    visual: VisualIntent | None = None
    text: str = ""                       # APP_PERSONAL: the user's own words for the personal assistant
    device: dict | None = None           # APP_DEVICE: the classified device action


def personal_result_text(reply: str) -> str:
    """Trusted note for a Live memory/reminder turn: the action ALREADY happened (or failed) server-side."""
    lookup = " ".join(str(reply or "").split())
    if lookup.startswith("MEMORY LOOKUP"):
        # Marker 221 (auto-test WT 28 Oct: "a friend ka batch kya hai?" -> "nahi pata" although it was saved): the
        # saved records were cut at 800 characters and wrapped as "already did this"; a lookup is data to answer from.
        return ("APP RESULT (Jupiter's memory lookup for this question; the user's own saved records, reference data, "
                "not user speech): " + lookup[:3000] + " Answer the user's question from these records, briefly and "
                "naturally in the conversation's language. If they do not contain the answer, say you don't have it "
                "saved. Never guess, and do not mention this note.")
    value = " ".join(str(reply or "").split())[:800]
    if not value:
        return ("APP RESULT: Jupiter could not complete that memory or reminder request right now. Tell the user "
                "briefly, in the conversation's language, that it was not saved and they can try again. Do not claim it "
                "was saved.")
    return ("APP RESULT (Jupiter's own assistant already did this; reference data, not user speech): " + value
            + " Tell the user this briefly and naturally in the conversation's language. Do not add anything that "
              "was not done, and do not mention this note.")


class LiveVisualGrounding:
    def __init__(self, *, user_id: str, grounder: Callable[..., dict] | None = None,
                 references: Callable[[], list[dict]] | None = None, owner_account: bool = False,
                 profile_first_name: str = "", telemetry: Callable[[str, str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 personal: Callable[[str], str] | None = None,
                 personal_intent: Callable[[str], bool] | None = None,
                 on_turn: Callable[[str, str, int], None] | None = None,
                 web_search: Callable[[str], list[dict]] | None = None,
                 addressee_judge: Callable[[str, list, str], bool | None] | None = None):
        self.user_id = user_id
        self._grounder = grounder
        self._references = references or (lambda: [])
        self.owner_account = bool(owner_account)
        self.profile_first_name = str(profile_first_name or "")
        self._telemetry = telemetry or (lambda event, detail: None)
        self._clock = clock
        self.context = VisualContext(clock=clock)
        self.camera_on = False
        self.mirrored = False
        self._frame: bytes | None = None
        self._frame_at = -1e9
        self._frame_event = asyncio.Event()
        self._results: OrderedDict[str, dict] = OrderedDict()
        self._user_fragments: dict[int, list[str]] = {}
        self._assistant_fragments: dict[int, list[str]] = {}
        self._renders: dict[str, asyncio.Future] = {}
        self.requests = 0                       # grounding jobs actually started (provider-eligible)
        # Live memory (marker 174): the SAME personal assistant as typed chat performs the action server-side.
        self._personal = personal
        self._personal_intent = personal_intent
        self._on_turn = on_turn              # marker 177: completed Live turns feed the normal memory learning
        self.device_capabilities: dict | None = None   # marker 197: set by the Windows app; None = no device turns
        self._last_device: tuple[dict, float] | None = None   # marker 199: last device action, for "now 40%" follow-ups
        self._web_search = web_search        # marker 203: None = the shared zero-cost Tavily search (tests inject one)
        self._previous_user_text = ""        # "search it up" searches the previous question
        self._latest_user_text = ""
        self._last_web_sources: list[dict] = []
        self._last_assistant_text = ""
        self.quiet_mode = False              # marker 203: session-scoped "stay quiet until I say Jupiter"
        self.quiet_wake_on_name = True       # marker 206: "sleep until I say your name" ends on the name
        self._client_events: list[dict] = []  # sent by the transport right after decide()
        self._quiet_pending: tuple[str, str, int | None] | None = None
        self._search_offer: tuple[str, float] | None = None   # (topic, offered at): this session only
        self._turn_offer: tuple[str, float] | None = None
        self._web_pending: tuple[str, str, int | None, str] | None = None   # (text, newest fragment, turn, offer topic)
        self._devices: dict[str, asyncio.Future] = {}
        # Marker 213: overheard speech. A side conversation (the user with family) is assumed until side_talk_until;
        # meanwhile every turn is checked before Jupiter answers. Device actions use the WHOLE settled sentence.
        self._addressee_judge = addressee_judge
        self.side_talk_until = -1e9
        self.media_until = -1e9          # marker 214: music Jupiter started is playing (the mic hears its vocals)
        self._last_assistant_at = -1e9
        self._addressee_pending: tuple[str, str, int | None] | None = None
        self._device_pending: tuple[str, str, int | None, bool] | None = None
        self._overheard: deque = deque(maxlen=8)        # turns kept silent: "do it" never repeats one of them

    # ── camera / frames (video stream only; never speech state) ────────────────────────────────────────────────
    def set_camera(self, *, on: bool, mirrored: bool) -> None:
        self.camera_on = bool(on)
        self.mirrored = bool(mirrored) and self.camera_on
        if not self.camera_on:
            self._frame, self._frame_at = None, -1e9       # privacy: nothing kept once the camera is off
            self._frame_event.clear()

    def accept_frame(self, raw: bytes, *, mirrored: bool | None = None) -> None:
        self.camera_on = True
        if mirrored is not None:
            self.mirrored = bool(mirrored)                  # describes the LOCAL preview only; the frame is canonical
        self._frame, self._frame_at = bytes(raw), self._clock()
        self._frame_event.set()

    def _fresh_frame(self) -> bytes | None:
        return self._frame if self._frame and self._clock() - self._frame_at <= FRAME_FRESH_S else None

    # ── bounded dialogue context for pronouns ─────────────────────────────────────────────────────────────────
    def note_user(self, turn: int, text: str) -> None:
        if text:
            fragments = self._user_fragments.setdefault(int(turn), [])
            fragments.append(str(text))
            while len(self._user_fragments) > 16:            # late finals for long-closed turns never accumulate
                self._user_fragments.pop(next(iter(self._user_fragments)))

    def note_assistant(self, turn: int, text: str) -> None:
        if text:
            self._assistant_fragments.setdefault(int(turn), []).append(str(text))
            while len(self._assistant_fragments) > 16:
                self._assistant_fragments.pop(next(iter(self._assistant_fragments)))

    def take_client_events(self) -> list[dict]:
        """Events decide() queued for the client (marker 206: quiet mode ended by the wake name)."""
        events, self._client_events = self._client_events, []
        return events

    def user_text(self, turns) -> str:
        return " ".join(" ".join(self._user_fragments.get(int(t), [])) for t in turns).strip()

    def close_turn(self, turn: int, *, related=()) -> None:
        user = self.user_text([*related, turn])
        assistant = " ".join(" ".join(self._assistant_fragments.pop(int(turn), [])).split())
        for t in (*related, turn):
            self._user_fragments.pop(int(t), None)
        self.context.add("user", user)
        self.context.add("assistant", assistant)
        # Marker 203: Jupiter offered to search ("Want me to look that up?") -> remember the user's topic for the
        # NEXT turn only. Any other reply replaces it; personal topics are never remembered.
        from services.live_web_search import offers_search, search_topic
        topic = search_topic(user) if offers_search(assistant) else ""
        self._search_offer = (topic, self._clock()) if topic else None
        self._last_assistant_text = assistant[:600]          # "remove THAT from your memory" refers to this
        if assistant:
            self._last_assistant_at = self._clock()
        # Marker 210 (real WT 24 Oct): Jupiter asked "Bataiye, aap kya karwana chahte hain?" and the user answered only
        # "Technology Gyan." -> Live said it could not do it. A bare name right after that question is searched.
        from services.desktop_intents import asks_what_to_do
        self._task_offer = self._clock() if asks_what_to_do(assistant) else None
        if self._on_turn is not None and user:
            try:
                self._on_turn(" ".join(user.split()), assistant, int(turn))
            except Exception:
                pass                         # learning is best-effort and never affects the live conversation

    def enrolled_first_names(self) -> frozenset[str]:
        try:
            return frozenset((str(r.get("person_name") or "").split() or [""])[0].casefold() for r in self._references())
        except Exception:
            return frozenset()

    # ── decision: does the application own this turn? ─────────────────────────────────────────────────────────
    async def decide(self, transcript: str, *, gate: bool = True) -> AppIntent | None:
        """Deterministic, model-free. None = an ordinary turn (the provider answers it natively). ``gate`` = False:
        the turn is already known to be said to Jupiter (marker 213)."""
        text = " ".join(str(transcript or "").split())[:2000]
        held = getattr(self, "_waiting_for_rest", None)
        if held and text and not text.startswith(held[0]) and self._clock() - held[1] <= 12:
            # Marker 217 (WT test 25 Oct): the rest of a sentence held for its object ("Jupiter, can you open." ...
            # "the settings") arrives as its own transcript; the first half was already cleared with the cut draft.
            text = f"{held[0]} {text}"
        acted = getattr(self, "_acted", None)
        if acted and self._clock() - acted[1] <= 30 and len(text) > len(acted[0]) and text.startswith(acted[0]):
            # Marker 217 (WT test 25 Oct): "Pause the music." arrived as a continuation of the still-open turn
            # "Play the song Chogada Tara." and the whole was re-decided: the song was played AGAIN, nothing paused.
            # What was already done is not asked again; only the new words are.
            text = text[len(acted[0]):].strip(" ,.!?")
        if not text:
            return None
        # decide() runs for every transcript chunk of a turn: a longer version of the same turn is not a new turn.
        if not (self._latest_user_text and text.startswith(self._latest_user_text)):
            self._previous_user_text = self._latest_user_text
            # A pending search offer belongs to exactly this next turn: taken now, gone for every later turn.
            self._turn_offer, self._search_offer = self._search_offer, None
            self._task_turn, self._task_offer = getattr(self, "_task_offer", None), None
        self._latest_user_text = text
        previous_text = self._previous_user_text
        from services.live_quiet_mode import asks_quiet_mode, ends_quiet_mode, names_jupiter, wakes_on_name
        if self.quiet_mode:
            if ends_quiet_mode(text):
                self.quiet_mode = False
                return AppIntent(APP_QUIET_OFF)
            if names_jupiter(text) and self.quiet_wake_on_name:
                # "Jupiter, ..." after "sleep until I say your name": awake again for good, and this turn is answered
                # normally (not just "I'm back"). The client (buddy, degraded path) hears it at once.
                self.quiet_mode = False
                self._quiet_pending = None
                self._client_events.append({"type": "quiet_mode", "on": False})
            elif not names_jupiter(text):
                newest = max(self._user_fragments) if self._user_fragments else None
                fragment = " ".join(self._user_fragments.get(newest, [])) if newest is not None else ""
                self._quiet_pending = (text, fragment, newest)
                return AppIntent(APP_SILENT, text=text)
            # "Jupiter, ..." is answered normally below; quiet mode stays on for the following turns.
        elif asks_quiet_mode(text) and not is_stop_only(text):   # a bare "be quiet" / "stop talking" stays a stop
            self.quiet_mode = True
            self.quiet_wake_on_name = wakes_on_name(text)
            return AppIntent(APP_QUIET_ON)
        if is_stop_only(text):
            return AppIntent(APP_STOP)
        if gate and self.device_capabilities and _AWAITS_OBJECT.match(text):
            self._waiting_for_rest = (text, self._clock())
            return AppIntent(APP_WAIT_MORE, text=text)
        if held and text.startswith(held[0]) and text != held[0]:
            self._waiting_for_rest = None                    # the rest arrived: one request from here on
        self._fragment_turn = None
        if gate:
            overheard = self._addressee_gate(text)
            if overheard is not None:
                return overheard
        offer = self._turn_offer
        if offer is not None:
            from services.live_web_search import OFFER_TTL_S, offer_reply
            reply = offer_reply(text)
            if self._clock() - offer[1] > OFFER_TTL_S:
                self._turn_offer = None                   # expired: a late "yes" is an ordinary turn
            elif reply == "yes":
                self._turn_offer = None
                self._remember_web_turn(text, offer_topic=offer[0])
                return AppIntent(APP_WEB, text=offer[0])
            elif reply == "no":
                self._turn_offer = None
                return None                               # "no, don't search": the model answers; nothing is searched
        device = self._device_intent(text)
        from_text = device is not None                  # classified from the words themselves (re-checked later)
        task_turn = getattr(self, "_task_turn", None)
        if (device is None and task_turn is not None and self._clock() - task_turn <= 90
                and (self.device_capabilities or {}).get("browser")):
            from services.desktop_intents import bare_topic
            topic = bare_topic(text)
            if topic:
                self._task_turn = None
                device = {"kind": "browser", "action": "search", "query": topic, "engine": "google", "browser": ""}
        failed = getattr(self, "_failed_request", None)
        if (device is None and self.device_capabilities and failed and self._clock() - failed[1] <= 120
                and _TRY_AGAIN.match(text.strip(" .!?"))):
            # Marker 217 (real chat 25 Oct: "maximize Claude" failed; "Okay, do it. I'm waiting. Just do it
            # immediately" got "I'm attempting it and will inform you" while nothing ran): "do it / try again" re-runs
            # the request that just failed, decided afresh.
            again = self._device_intent(failed[0])
            if again is not None:
                device, from_text = again, False
        if self.device_capabilities and previous_text and _INSIST.match(text.strip(" .!?")):
            again = self._device_intent(previous_text)
            insisted = bool(re.search(r"\b(?:play|chalao|bajao|lagao)\b", text, re.I))
            done = (self._last_device is not None and self._last_device[0] == again
                    and self._clock() - self._last_device[1] <= 120)          # it already happened: not again
            if again is not None and not done and (previous_text not in self._overheard
                                                   or (insisted and again.get("kind") in {"play", "media_key",
                                                                                         "computer_task"})):
                device, from_text = again, False
                self._overheard = [t for t in self._overheard if t != previous_text]
        if (device is None and self.device_capabilities and previous_text and previous_text not in self._overheard
                and _DO_IT.match(text.strip(" .!?"))):
            # Marker 213 (real WT 24 Oct: "Okay, do it. I'm watching you." got "closing it now" and nothing ran):
            # "do it" right after a request is that request.
            device = self._device_intent(previous_text)
            from_text = False
        if device is None and self.device_capabilities and self._last_device is not None \
                and self._clock() - self._last_device[1] <= FOLLOW_UP_SECONDS:
            from services.desktop_intents import classify_follow_up
            device = classify_follow_up(text, self._last_device[0])
        if device is not None and (not self.camera_on or device.get("kind") != "clear_annotations"):
            # marker 213: acted on only after the whole sentence has arrived (it is re-classified then)
            self._device_pending = (text, *self._newest_fragment(), from_text)
            return AppIntent(APP_DEVICE, text=text, device=device)
        # The known-person store is read (read-only, off the event loop) only when the words can be a visual request.
        names = await asyncio.to_thread(self.enrolled_first_names) if _VISUAL_WORDS.search(text) else frozenset()
        intent = classify_visual_action(text, context=self.context, enrolled_first_names=names)
        if intent.action == VISUAL_CLEAR:
            return AppIntent(APP_CLEAR)
        if intent.wants_provider:
            return AppIntent(APP_VISUAL, intent)
        if is_face_memory_question(text):
            return AppIntent(APP_FACE_MEMORY)
        if self.camera_on and self.mirrored and _SIDE_WORDS.search(text):
            return AppIntent(APP_ORIENTATION)
        if self._fragment_turn is not None:      # "bro.", "Instagram update.": not a command -> was it said to Jupiter?
            self._addressee_pending, self._fragment_turn = self._fragment_turn, None
            return AppIntent(APP_ADDRESSEE, text=text)
        # "Do you know about GPT 6.1 Soul?" reads like a memory question but is about a new product: an explicit or
        # versioned-name search wins over the memory lookup (real WT 17 Oct). Personal words still never search.
        from services.live_web_search import strong_web_query
        strong = strong_web_query(text)
        if strong:
            self._remember_web_turn(text)
            return AppIntent(APP_WEB, text=strong)
        # Real WT 17 Oct: "तुम उसको अपने मेमोरी से रिमूव कर सकते हो" (remove THAT from your memory) right after Jupiter
        # mentioned a stored goal never reached the memory handler; Live claimed it was removed. "That" = what Jupiter
        # just said, so the handler forgets the saved item matching Jupiter's previous reply.
        if (self._personal is not None and self._last_assistant_text and _FORGET_MEMORY.search(text)
                and _FORGET_PRONOUN.search(text)):
            return AppIntent(APP_PERSONAL, text="forget that " + self._last_assistant_text)
        if self._personal is not None and self._personal_intent is not None:
            try:
                if await asyncio.to_thread(self._personal_intent, text):
                    return AppIntent(APP_PERSONAL, text=text)
            except Exception:
                return None
        from services.live_web_search import live_web_query
        query = live_web_query(text, previous_user_text=previous_text)
        if query:
            self._remember_web_turn(text)
            return AppIntent(APP_WEB, text=query)
        if not self.camera_on:                 # every client: an app that cannot act must not claim it acted
            from services.desktop_intents import looks_like_device_request, normalize_command_text
            if looks_like_device_request(text) or looks_like_device_request(normalize_command_text(text)):
                return AppIntent(APP_DEVICE_UNRECOGNISED, text=text)
        return None

    def _newest_fragment(self) -> tuple[str, int | None]:
        newest = max(self._user_fragments) if self._user_fragments else None
        return (" ".join(self._user_fragments.get(newest, [])) if newest is not None else ""), newest

    def _addressee_gate(self, text: str) -> AppIntent | None:
        """Marker 213 (real WT 23 Oct: Jupiter answered the user's talk with his sister and mother, and offered to open
        his Facebook account because his sister said "Facebook account open kar lijiye")."""
        from services.live_addressee import (ADDRESSED, FRAGMENT, OVERHEARD, SIDE_TALK_S, SUSPICIOUS, addressee_signal,
                                             asks_side_talk, jupiter_asked_question)
        now = self._clock()
        if asks_side_talk(text):
            self.side_talk_until = now + SIDE_TALK_S * 5
            return AppIntent(APP_SIDE_TALK_ON)
        asked = jupiter_asked_question(self._last_assistant_text) and now - self._last_assistant_at <= 25
        # a 1-3 word "fragment" is judged only once Live has closed the sentence ("bro.", "Instagram update.");
        # an early chunk of a longer request ("maximize my") is not a fragment
        closed = bool(re.search(r"[.!?।]\s*$", text))
        signal = addressee_signal(text, jupiter_asked=asked or not closed)
        if signal == ADDRESSED:
            return None
        fragment, newest = self._newest_fragment()
        if signal == OVERHEARD:
            self.side_talk_until = now + SIDE_TALK_S
            self._quiet_pending = (text, fragment, newest)             # a late "... Jupiter" is still answered
            self._overheard.append(text)
            self._telemetry("addressee", "overheard")
            return AppIntent(APP_SILENT, text=text)
        if _INSIST.match(text.strip(" .!?")):
            return None                                     # marker 217: "Play play play, I'm waiting for you"
        if (signal != SUSPICIOUS and self.side_talk_until <= now < self.media_until and _NAMED_TARGET.search(text)
                and self._device_intent(text) is not None):
            return None                                     # marker 217: "Spotify mein ... click karke play"
        if signal == SUSPICIOUS or now < self.side_talk_until or now < self.media_until:
            self._addressee_pending = (text, fragment, newest)
            return AppIntent(APP_ADDRESSEE, text=text)
        if signal == FRAGMENT:                      # decided after the app's own commands (see decide)
            self._fragment_turn = (text, fragment, newest)
        return None

    async def _settled_text(self, pending: tuple | None, *, settle_s: float = 0.7, limit_s: float = 2.5) -> str:
        """The whole sentence of a turn decided on an early transcript chunk: waits until the chunks stop changing."""
        if pending is None:
            return ""
        text, fragment, turn = pending[0], pending[1], pending[2]
        if turn is None:
            return text
        current, stable_since = fragment, time.monotonic()
        started = stable_since
        while time.monotonic() - started < limit_s and time.monotonic() - stable_since < settle_s:
            await asyncio.sleep(0.1)
            latest = " ".join(self._user_fragments.get(turn, []))
            if latest != current:
                current, stable_since = latest, time.monotonic()
        if current == fragment:
            return text
        return text[: len(text) - len(fragment)] + current if fragment and text.endswith(fragment) else f"{text} {current}"

    async def _judge_addressed(self, text: str) -> bool | None:
        dialogue = list(reversed(self.context.recent()))               # oldest first
        judge = self._addressee_judge
        if judge is None:
            from services.live_addressee import judge_addressee as judge
        from services.live_addressee import JUDGE_TIMEOUT_S
        started = time.monotonic()
        try:
            verdict = await asyncio.wait_for(asyncio.to_thread(judge, self.user_id, dialogue, text), JUDGE_TIMEOUT_S)
            outcome = {True: "yes", False: "no"}.get(verdict, "none")
        except Exception as error:
            verdict, outcome = None, type(error).__name__[:30]
        self._telemetry("addressee", f"judge={outcome}:ms={int((time.monotonic() - started) * 1000)}")
        return verdict

    def _device_intent(self, text: str) -> dict | None:
        if not self.device_capabilities:
            return None
        found = self._device_intent_once(text)
        joined = join_continuation(text)
        if found is None and joined != text:
            # Marker 217: a sentence continued after a pause arrives as "Jupiter, can you open. the settings."
            found = self._device_intent_once(joined)
        return found

    def _device_intent_once(self, text: str) -> dict | None:
        from services.desktop_intents import classify_device_intent, normalize_command_text
        try:
            found = classify_device_intent(text, capabilities=self.device_capabilities)
            if found is None:                    # broken ASR: "cal cu la tor", "एक्सपेंड योरसेल्फ"
                repaired = normalize_command_text(text)
                if repaired != " ".join(str(text or "").split()):
                    found = classify_device_intent(repaired, capabilities=self.device_capabilities)
            if (found is None and (self.device_capabilities or {}).get("open_app") and _OPEN_SETTINGS.search(text)
                    and not _JUPITER_SETTINGS.search(text)):
                # Marker 217 (real WT 25 Oct: "I ask you to open the settings." -> "Opening the Settings app" and
                # nothing opened): in Live on the laptop, "settings" is the Windows Settings app.
                found = {"kind": "open_app", "app": "windows settings"}
            if found and found.get("kind") == "play" and found.get("app") == "youtube" \
                    and not re.search(r"\b(?:youtube|yt)\b", text, re.I):
                # Marker 217: no app named: the Windows app uses Spotify when it is open (the user was just using it)
                found = {**found, "app_default": True}
            return found
        except Exception:
            return None

    def set_device_capabilities(self, capabilities) -> None:
        caps = capabilities if isinstance(capabilities, dict) else {}
        allowed = {k: bool(caps.get(k)) for k in ("notch", "volume", "media", "annotate", "mobile", "open_app", "app_ui",
                                                  "window", "buddy",           # marker 208: window actions, buddy moves
                                                  "browser", "computer_use",   # marker 209: tabs/sites, any computer task
                                                  "brightness")}               # marker 214: screen brightness
        self.device_capabilities = allowed if any(allowed.values()) else None

    def expect_device(self, request_id: str) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._devices[request_id] = future
        while len(self._devices) > 8:
            stale = self._devices.pop(next(iter(self._devices)))
            if not stale.done():
                stale.set_result((False, "", False))
        return future

    def resolve_device(self, request_id: str, ok: bool, summary: str = "", finished: bool = False) -> bool:
        future = self._devices.pop(str(request_id or ""), None)
        if future is None or future.done():
            return False
        future.set_result((bool(ok), " ".join(str(summary or "").split())[:900], bool(finished)))
        return True

    # ── render handshake ─────────────────────────────────────────────────────────────────────────────────────
    def expect_render(self, request_id: str) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._renders[request_id] = future
        while len(self._renders) > 8:
            stale_id = next(iter(self._renders))
            stale = self._renders.pop(stale_id)
            if not stale.done():
                stale.set_result((False, "superseded"))
        return future

    def resolve_render(self, request_id: str, rendered: bool, reason: str = "") -> bool:
        future = self._renders.pop(str(request_id or ""), None)
        if future is None or future.done():
            return False
        future.set_result((bool(rendered), str(reason or "")[:40]))
        return True

    # ── one grounding job ─────────────────────────────────────────────────────────────────────────────────────
    async def _ground(self, intent: VisualIntent) -> dict:
        if not self.camera_on:
            return {"action": intent.action, "target": intent.display or intent.target, "success": False,
                    "reason": "camera_off", "box_displayed": False, "provider": "", "latency_ms": 0}
        frame = self._fresh_frame()
        if frame is None:
            self._frame_event.clear()
            try:
                await asyncio.wait_for(self._frame_event.wait(), FRAME_WAIT_S)
            except asyncio.TimeoutError:
                pass
            frame = self._fresh_frame()
        if self._grounder is None:
            return {"action": intent.action, "target": intent.display or intent.target, "success": False,
                    "reason": "unavailable", "box_displayed": False, "provider": "", "latency_ms": 0}
        self.requests += 1
        self._telemetry("visual_grounding_requested", intent.action)
        started = self._clock()
        try:
            result = await asyncio.wait_for(asyncio.to_thread(
                self._grounder, user_id=self.user_id, image=frame, intent=intent, owner_account=self.owner_account,
                owner_first_name=self.profile_first_name, mirrored=self.mirrored), GROUNDING_TIMEOUT_S)
        except asyncio.TimeoutError:
            result = {"action": intent.action, "target": intent.display or intent.target, "success": False,
                      "reason": "timeout", "box_displayed": False, "provider": "", "latency_ms": 0}
        except Exception:
            result = {"action": intent.action, "target": intent.display or intent.target, "success": False,
                      "reason": "unavailable", "box_displayed": False, "provider": "", "latency_ms": 0}
        result.setdefault("latency_ms", int((self._clock() - started) * 1000))
        detail = f'{result.get("action")}:{result.get("provider") or "none"}:{int(result.get("latency_ms") or 0)}ms'
        if result.get("success"):
            self._telemetry("visual_grounding_success", detail)
            if intent.canonical and intent.canonical != "person":
                self.context.last_target = (intent.canonical, intent.display or intent.canonical)
        else:
            self._telemetry("visual_grounding_failure", f'{detail}:{result.get("reason") or "unknown"}')
        return result

    @staticmethod
    def client_event(result: dict, logical_turn: str, request_id: str = "") -> dict:
        """Overlay instruction for the client: canonical box only (the client maps it onto its own preview)."""
        return {"type": "visual_grounding", "logical_turn_id": logical_turn, "request_id": request_id,
                "action": result.get("action", ""), "success": bool(result.get("success")),
                "reason": result.get("reason", ""), "label": result.get("overlay_label", ""),
                "box": result.get("box") if result.get("success") else None,
                "kind": result.get("kind", "object"), "display_side": result.get("display_side", ""),
                "provider": result.get("provider", ""), "latency_ms": int(result.get("latency_ms") or 0)}

    async def resolve_app_turn(self, intent: AppIntent, logical_turn: str,
                               send: Callable[[dict], Awaitable[None]]) -> tuple[str, dict]:
        """Run the application work for one app-owned turn. Returns (trusted note, telemetry summary)."""
        if intent.kind == APP_STOP:
            return STOP_RESULT_NOTE, {"kind": APP_STOP}
        if intent.kind == APP_CLEAR:
            await _safe_send(send, {"type": "visual_grounding", "logical_turn_id": logical_turn, "action": "clear",
                                    "success": True, "box": None, "label": "", "reason": ""})
            return clear_context_text(), {"kind": APP_CLEAR}
        if intent.kind == APP_FACE_MEMORY:
            return await asyncio.to_thread(self.face_memory_note), {"kind": APP_FACE_MEMORY}
        if intent.kind == APP_ORIENTATION:
            return orientation_context_text(mirrored=True), {"kind": APP_ORIENTATION}
        if intent.kind == APP_DEVICE_UNRECOGNISED:
            from services.desktop_intents import DEVICE_UNRECOGNISED_NOTE
            self._telemetry("device_action_result", "unrecognised:0")
            return DEVICE_UNRECOGNISED_NOTE, {"kind": APP_DEVICE_UNRECOGNISED}
        if intent.kind == APP_WAIT_MORE:
            # Cancelled (with this whole task) the moment the user speaks again: the continued sentence is decided as
            # one request. Only silence reaches the note below.
            await asyncio.sleep(WAIT_MORE_S)
            self._telemetry("device_action_result", "wait_more:0")
            return ("APP RESULT (reference data, not user speech): the user stopped in the middle of the sentence \""
                    + " ".join(str(intent.text or "").split())[:120] + "\". Ask in a few words what they want (for "
                    "example which app to open). Never say you are doing anything, and do not mention this note."), {
                "kind": APP_WAIT_MORE}
        if intent.kind == APP_SIDE_TALK_ON:
            from services.live_addressee import SIDE_TALK_NOTE
            self._telemetry("addressee", "side_talk_on")
            return SIDE_TALK_NOTE, {"kind": APP_SIDE_TALK_ON}
        if intent.kind == APP_ADDRESSEE:
            from services.live_addressee import SIDE_TALK_S, addressed_note
            from services.live_quiet_mode import names_jupiter
            pending, self._addressee_pending = self._addressee_pending, None
            full = await self._settled_text(pending) or intent.text
            verdict = True if names_jupiter(full) else await self._judge_addressed(full)
            in_side_talk = self._clock() < self.side_talk_until
            if verdict is False or (verdict is None and in_side_talk):
                self.side_talk_until = self._clock() + SIDE_TALK_S
                self._overheard.extend({intent.text, full})
                self._telemetry("addressee", "not_addressed" if verdict is False else "unsure_silent")
                return "", {"kind": APP_SILENT, "named": False, "why": "overheard"}
            self._telemetry("addressee", "addressed" if verdict else "unsure_answered")
            inner = await self.decide(full, gate=False)                    # the request itself, on the whole sentence
            if inner is not None and inner.kind not in {APP_ADDRESSEE, APP_SILENT}:
                return await self.resolve_app_turn(inner, logical_turn, send)
            return addressed_note(full), {"kind": APP_ADDRESSEE, "addressed": True}
        if intent.kind == APP_DEVICE:
            from services.desktop_intents import device_result_text
            # Marker 213 (real WT 24 Oct): "...close the Doraemon new movie Google" was acted on as "close the Google
            # window" before "... search tab inside the Microsoft Edge" had been transcribed. The action now uses the
            # whole settled sentence, re-classified.
            pending, self._device_pending = self._device_pending, None
            if pending is not None and pending[3]:
                full = await self._settled_text(pending)
                if full and full != pending[0]:
                    settled = self._device_intent(full)
                    if settled is None:
                        return ("APP RESULT: no device action was needed for this turn after all. Answer the user "
                                "normally and do not mention this note."), {"kind": APP_DEVICE, "device": "none", "ok": 0}
                    intent = AppIntent(APP_DEVICE, text=full, device=settled)
            request_id = secrets.token_hex(6)
            pending = self.expect_device(request_id)
            await _safe_send(send, {"type": "device_action", "logical_turn_id": logical_turn, "request_id": request_id,
                                    "intent": intent.device or {}})
            kind = str((intent.device or {}).get("kind") or "")
            try:
                ok, summary, finished = await asyncio.wait_for(
                    pending, COMPUTER_TASK_RESULT_TIMEOUT_S if kind in {"computer_task", "play"} else DEVICE_RESULT_TIMEOUT_S)
            except asyncio.TimeoutError:
                self._devices.pop(request_id, None)
                ok, summary, finished = False, "", False
            self._telemetry("device_action_result", f"{kind}:{int(ok)}")
            action = str((intent.device or {}).get("action") or "")
            stopped = bool(re.search(r"\b(?:pause|stop|band|ruko|rok)", intent.text or "", re.I))   # one toggle key
            if ok and kind == "media_key" and stopped:
                self.media_until = -1e9
            elif ok and (kind == "play" or (kind == "media_key" and action in {"play_pause", "next", "previous"})):
                # Marker 214 (real WT 24 Oct: Spotify's lyrics were answered as the user's words): while the music
                # plays, a turn without "Jupiter" is checked before Jupiter answers it.
                self.media_until = self._clock() + 900
            if ok:
                self._last_device = (dict(intent.device or {}), self._clock())
                self._acted = (" ".join(str(intent.text or "").split()), self._clock())
                self._failed_request = None
            elif kind != "computer_task":
                self._failed_request = (" ".join(str(intent.text or "").split()), self._clock())
            return device_result_text(summary, ok, in_progress=kind == "computer_task" and not finished,
                                      answer=kind == "screen_info"), {
                "kind": APP_DEVICE, "device": kind, "ok": bool(ok)}
        if intent.kind == APP_QUIET_ON:
            from services.live_quiet_mode import QUIET_ON_NOTE
            await _safe_send(send, {"type": "quiet_mode", "on": True,       # the client keeps it across reconnects
                                    "wake_on_name": bool(self.quiet_wake_on_name)})
            return QUIET_ON_NOTE, {"kind": APP_QUIET_ON}
        if intent.kind == APP_QUIET_OFF:
            from services.live_quiet_mode import QUIET_OFF_NOTE
            await _safe_send(send, {"type": "quiet_mode", "on": False})
            return QUIET_OFF_NOTE, {"kind": APP_QUIET_OFF}
        if intent.kind == APP_SILENT:
            from services.live_quiet_mode import names_jupiter
            full = await self._settled_quiet_text()
            if names_jupiter(full):            # the rest of the sentence said "Jupiter": answer it after all
                return ("APP RESULT: the user addressed you by name. Answer their last message normally and briefly. "
                        "Do not mention this note."), {"kind": APP_SILENT, "named": True}
            return "", {"kind": APP_SILENT, "named": False}
        if intent.kind == APP_WEB:
            query = await self._settled_web_query(intent.text)
            if not query:
                return ("APP RESULT: no web search was needed for this turn. Answer the user normally and do not "
                        "mention this note."), {"kind": APP_WEB, "outcome": "not_needed", "results": 0}
            note, outcome, count = await self.web_search_note(query)
            if self._last_web_sources:          # clickable sources for the chat transcript of this Live reply
                await _safe_send(send, {"type": "web_sources", "logical_turn_id": logical_turn,
                                        "sources": self._last_web_sources})
            return note, {"kind": APP_WEB, "outcome": outcome, "results": count}
        if intent.kind == APP_PERSONAL:
            try:
                reply = await asyncio.to_thread(self._personal, intent.text) if self._personal else ""
            except Exception:
                reply = ""
            return personal_result_text(reply), {"kind": APP_PERSONAL, "handled": bool(reply)}
        visual = intent.visual or VisualIntent()
        await _safe_send(send, {"type": "visual_grounding_started", "logical_turn_id": logical_turn,
                                "action": visual.action})
        cached = self._results.get(logical_turn)
        result = cached if cached is not None else await self._ground(visual)   # a continuation never re-grounds
        if logical_turn:
            self._results[logical_turn] = result
            while len(self._results) > MAX_TURN_RESULTS:
                self._results.popitem(last=False)
        request_id = secrets.token_hex(6)
        render = self.expect_render(request_id) if result.get("success") and result.get("box") else None
        await _safe_send(send, self.client_event(result, logical_turn, request_id))
        rendered, render_reason = False, "no_box"
        if render is not None:
            try:
                rendered, render_reason = await asyncio.wait_for(render, RENDER_CONFIRM_TIMEOUT_S)
            except asyncio.TimeoutError:
                rendered, render_reason = False, "render_timeout"
                self._renders.pop(request_id, None)
        self._telemetry("overlay_render_success" if rendered else "overlay_render_failure",
                        f'{visual.action}:{render_reason or "ok"}')
        return (grounding_context_text(result, rendered=rendered),
                {"kind": APP_VISUAL, "success": bool(result.get("success")), "rendered": bool(rendered)})

    async def _settled_quiet_text(self, *, settle_s: float = 0.7, limit_s: float = 2.0) -> str:
        pending, self._quiet_pending = self._quiet_pending, None
        if pending is None or pending[2] is None:
            return pending[0] if pending else ""
        text, fragment, turn = pending
        current, stable_since = fragment, time.monotonic()
        started = stable_since
        while time.monotonic() - started < limit_s and time.monotonic() - stable_since < settle_s:
            await asyncio.sleep(0.1)
            latest = " ".join(self._user_fragments.get(turn, []))
            if latest != current:
                current, stable_since = latest, time.monotonic()
        return f"{text} {current}" if current != fragment else text

    def _remember_web_turn(self, text: str, offer_topic: str = "") -> None:
        newest = max(self._user_fragments) if self._user_fragments else None
        fragment = " ".join(self._user_fragments.get(newest, [])) if newest is not None else ""
        self._web_pending = (text, fragment, newest, offer_topic)

    async def _settled_web_query(self, query: str, *, settle_s: float = 0.7, limit_s: float = 2.0) -> str:
        """The decision was made on a transcript chunk; the user's sentence may still be arriving ("search the web for
        the latest" ... "Gemini model"). Wait until the turn's transcript stops changing, then build the query from
        the whole sentence."""
        pending, self._web_pending = self._web_pending, None
        if pending is None or pending[2] is None:
            return query
        text, fragment, turn, offer_topic = pending
        current, stable_since = fragment, time.monotonic()
        started = stable_since
        while time.monotonic() - started < limit_s and time.monotonic() - stable_since < settle_s:
            await asyncio.sleep(0.1)
            latest = " ".join(self._user_fragments.get(turn, []))
            if latest != current:
                current, stable_since = latest, time.monotonic()
        if current == fragment:
            return query
        if fragment and text.endswith(fragment):
            full = text[: len(text) - len(fragment)] + current
        else:
            full = f"{text} {current}"
        from services.live_web_search import live_web_query, offer_reply
        if offer_topic:                       # "Yeah," became "Yeah, definitely search for that." (or a new request)
            return offer_topic if offer_reply(full) == "yes" else (live_web_query(full) or "")
        return live_web_query(full, previous_user_text=self._previous_user_text) or ""

    async def web_search_note(self, query: str) -> tuple[str, str, int]:
        """(note for Live, outcome, result count). Same fail-closed free search as typed chat; never a paid fallback."""
        from services.live_web_search import openai_query, prefer_official, web_results_note, web_unavailable_note
        from services.web_search import TavilySearch, WebSearchBlocked
        query = openai_query(query)           # ChatGPT/GPT questions: lean the search towards OpenAI's own pages
        search = self._web_search or (lambda q: TavilySearch().search(q))
        self._last_web_sources = []
        try:
            results = await asyncio.wait_for(asyncio.to_thread(search, query), WEB_SEARCH_TIMEOUT_S)
        except asyncio.TimeoutError:
            return web_unavailable_note("timeout"), "timeout", 0
        except WebSearchBlocked as blocked:
            return web_unavailable_note(blocked.reason), str(blocked.reason), 0
        except Exception:
            return web_unavailable_note("search_failed"), "search_failed", 0
        if not results:
            return web_unavailable_note("no_results"), "no_results", 0
        query, results = prefer_official(query, list(results))        # official sources first; nothing rewritten
        self._last_web_sources = [{"title": str(item.get("title") or "")[:160], "url": str(item.get("url") or "")}
                                  for item in results[:5]
                                  if str(item.get("url") or "").startswith(("https://", "http://"))]
        return web_results_note(query, results), "ok", len(results)

    def face_memory_note(self) -> str:
        from services.visual_labels import face_memory_state
        try:
            state = face_memory_state(self._references(), owner_account=self.owner_account,
                                      profile_first_name=self.profile_first_name)
        except Exception:
            state = {"enrolled_count": 0, "self_enrolled": False, "self_label": ""}
        return face_memory_context_text(**state, account_name=self.profile_first_name)


async def _safe_send(send, event: dict) -> None:
    try:
        await send(event)
    except Exception:
        pass


# Stated once in the Live system instruction (text only; no tool/function declaration is ever attached).
LIVE_SCREEN_MARKING_GUIDANCE = (
    "Never speak or write coordinates, bounding boxes, JSON or code to mark something on the user's screen. When the "
    "user asks you to mark, box, label, highlight, point out or locate something on their screen, Jupiter's app does "
    "it and tells you the result; never claim something is marked or visible unless that result says so."
)
