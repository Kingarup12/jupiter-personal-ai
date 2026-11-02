"""The personal-assistant turn handler: explicit memory and reminder commands, owned by the application.

Contract: a reply that says something was saved, shared, forgotten, scheduled or cancelled is produced only *after*
the operation is persisted and read back. Failures produce a short truthful sentence; internals go to the log as
bounded, content-free diagnostics (personal_intent, memory_operation, memory_result, reminder_operation, ...).
Anything that is not an explicit personal command returns `handled=False` and normal chat continues unchanged.
"""

from __future__ import annotations

import re
from dataclasses import replace, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from core.logger import Logger
from services.personal_intents import (
    LAST, MEMORY_FORGET, MEMORY_READ, MEMORY_SHARE, MEMORY_UNSHARE, MEMORY_UPDATE, MEMORY_WRITE, REMINDER_CANCEL,
    ACCOUNT_SELF, CALL_USER, CONTACT_REQUEST, RELAY_MESSAGE, REMINDER_CREATE, REMINDER_LIST, REMINDER_SNOOZE, REMINDER_UPDATE, SELF, TASK_NOTIFY, PersonalIntent,
    EMAIL_ADDRESS, classify_personal_intent, clean_name, identifier_label,
)
from services.reminders import (
    ReminderService, describe_when, next_occurrence, parse_when, reminder_title, second_person, zone,
)

SAVE_FAILED = "I couldn't save that memory right now."
REMINDER_FAILED = "I couldn't create that reminder right now."


@dataclass
class PersonalOutcome:
    handled: bool = False
    reply: str = ""
    kind: str = ""
    continue_with: str = ""                     # a second, non-personal request to run through normal chat
    metadata: dict = field(default_factory=dict)


# A Hinglish reminder subject ("station aa raha hai") reads as "remind you at 1 PM: …", not "… to station aa raha hai".
_HINGLISH_TITLE = re.compile(r"(?i)\b(?:hai|hain|raha|rahi|rahe|lena|leni|lene|peena|pina|karna|jana|jaana|aana|hoga|hogi)\b")


def _diagnostic(**fields: Any) -> None:
    Logger.info("personal_assistant " + " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, "")))


def _pronoun(entry: dict) -> tuple[str, str]:
    """(subject, object) pronouns only from what the user wrote about the person; neutral otherwise."""
    text = " ".join(str(fact.get("text") or "") for fact in entry.get("facts") or [])[:4000].casefold()
    she = len(re.findall(r"\b(she|her|hers|herself)\b", text))
    he = len(re.findall(r"\b(he|him|his|himself)\b", text))
    if she > he and she >= 2:
        return "she", "her"
    if he > she and he >= 2:
        return "he", "him"
    return "they", "them"


def _brief(text: str, limit: int = 240) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    cut = value[:limit]
    sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:sentence_end + 1] if sentence_end > 80 else cut.rsplit(" ", 1)[0] + "…")


def _second_person_fact(text: str, limit: int = 240) -> str:
    value = re.sub(r"(?i)^(?:that\s+)", "", _brief(text, limit))
    value = second_person(value) if value else value
    return value[:1].upper() + value[1:]


# Work that only an outside worker can do (Jupiter does not run Claude Code / Codex / builds / scripts itself).
_EXTERNAL_WORKER = re.compile(
    r"(?i)\b(?:claude\s*code|codex|antigravity|build|compile|deploy|release|ship|publish|run\s+(?:the\s+)?tests?|"
    r"pytest|script|repo(?:sitory)?|commit|merge|pull\s+request|worker|install|apk|msix|"
    # files, folders and device content live on the user's machine, not in a chat reply
    r"files?|folders?|director(?:y|ies)|photos?|pictures?|videos?|export|import|upload|download|rename|move|"
    r"copy|delete|backup|sync|organi[sz]e)\b|\b[\w-]+\.(?:txt|md|py|js|ts|json|csv|docx?|xlsx?|pdf|png|jpe?g)\b")


class PersonalAssistant:
    def __init__(
        self,
        email: str,
        *,
        chat_id: str | None = None,
        message_id: str | None = None,
        client_timezone: str | None = None,
        persist: bool = True,
        find_user: Callable[[str], dict | None] | None = None,
        display_name: Callable[[str], str] | None = None,
        now: datetime | None = None,
        engine=None,
        account_lookup: Callable[[], dict | None] | None = None,
        contact_request: Callable[[str, list], dict] | None = None,
        directed_send: Callable[..., dict] | None = None,
        find_users: Callable[[str], list] | None = None,
        urgent_call: Callable[..., dict] | None = None,
    ):
        from services.initiative_engine import InitiativeEngine
        from services.personal_memory import PersonalMemory
        self.email = str(email or "").strip()
        self.chat_id = chat_id
        self.message_id = message_id
        self.persist = persist
        self.find_user = find_user or (lambda _identity: None)
        self.display_name = display_name or (lambda value: value.split("@")[0])
        self.engine = engine or InitiativeEngine()
        self.reminders = ReminderService(self.engine)
        self.memory = PersonalMemory(self.email)
        self.tz_name = self.reminders.effective_timezone(self.email, client_timezone)
        self.tz = zone(self.tz_name)
        self._now = now
        self.account_lookup = account_lookup      # marker 184: the authenticated session's own account record
        self.contact_request = contact_request    # marker 185: consent request bound to this authenticated requester
        self.directed_send = directed_send        # marker 186: block check + inbox + alert, bound to this sender
        self.find_users = find_users              # every exact username match (usernames may contain spaces)
        self.urgent_call = urgent_call            # rings ANOTHER Jupiter user's devices (server-checked, rate-limited)

    # ── entry point ─────────────────────────────────────────────────────────────────────────────────────────────
    def now(self) -> datetime:
        return (self._now or datetime.now(timezone.utc)).astimezone(self.tz)

    def handle(self, text: str) -> PersonalOutcome:
        if not self.email:
            return PersonalOutcome()
        intent = classify_personal_intent(text)
        if not intent.is_personal:
            follow_up = self._continue_call_target(text)
            if not follow_up.handled:
                follow_up = self._identifier_question(text)
            if not follow_up.handled:
                follow_up = self._continue_call_draft(text)
            if not follow_up.handled:
                follow_up = self._retime_recent_reminder(text)
            return follow_up if follow_up.handled else self._continue_reminder_draft(text)
        _diagnostic(personal_intent=intent.kind, selected_component="personal_assistant")
        # A temporary chat neither saves nor uses memories; reminders and forgetting stay available (explicit,
        # user-owned actions that are visible and cancellable in Reminders / People I Know).
        if not self.persist and intent.kind in {MEMORY_WRITE, MEMORY_UPDATE, MEMORY_SHARE}:
            return PersonalOutcome(True, "This is a temporary chat, so I won't save that here.", intent.kind)
        if not self.persist and intent.kind == MEMORY_READ:
            return PersonalOutcome(True, "This is a temporary chat, so I'm not using your saved memories here.", intent.kind)
        handler = {
            MEMORY_WRITE: self._memory_write, MEMORY_READ: self._memory_read, MEMORY_UPDATE: self._memory_update,
            MEMORY_FORGET: self._memory_forget, MEMORY_SHARE: self._memory_share, MEMORY_UNSHARE: self._memory_unshare,
            REMINDER_CREATE: self._reminder_create, REMINDER_LIST: self._reminder_list,
            REMINDER_UPDATE: self._reminder_update, REMINDER_CANCEL: self._reminder_cancel,
            REMINDER_SNOOZE: self._reminder_snooze, TASK_NOTIFY: self._task_notify, RELAY_MESSAGE: self._relay,
            ACCOUNT_SELF: self._account_self, CONTACT_REQUEST: self._contact_request, CALL_USER: self._call_user,
        }[intent.kind]
        try:
            outcome = handler(intent, text)
        except Exception as error:                                          # never break normal chat
            Logger.warning(f"personal_assistant intent={intent.kind} failed error_class={type(error).__name__}")
            reply = REMINDER_FAILED if intent.kind.startswith("REMINDER") else SAVE_FAILED
            if intent.kind in {MEMORY_READ, REMINDER_LIST}:
                reply = "I couldn't look that up right now."
            outcome = PersonalOutcome(True, reply, intent.kind)
        if outcome.handled and not outcome.kind:
            outcome.kind = intent.kind
        return outcome

    # ── the signed-in account's own email / phone (marker 184) ───────────────────────────────────────────────────
    def _account_self(self, intent: PersonalIntent, text: str) -> PersonalOutcome:
        """Answered ONLY from the server-authenticated session (``self.email`` is the authenticated principal; the
        lookup returns that same account's record). Nothing typed in the message selects the account, so a user can
        only ever see their own details. A phone number is reported only when the account record holds one marked
        verified — Jupiter does not currently collect phone numbers, so today that is always "none saved"."""
        from services.delivery_claims import _language
        details = None
        if self.account_lookup is not None:
            try:
                details = self.account_lookup()
            except Exception:
                details = None
        if details is None and self.email:
            details = {"login_email": self.email}
        login_email = str((details or {}).get("login_email") or "").strip()
        language = _language(text)
        if not login_email or "@" not in login_email:
            _diagnostic(account_self="session_unconfirmed")
            return PersonalOutcome(True, {
                "hi": "मैं अभी पुष्टि नहीं कर पाई कि कौन-सा अकाउंट साइन इन है। कृपया दोबारा साइन इन करें।",
                "hi-Latn": "Main abhi confirm nahi kar paayi ki kaunsa account signed in hai. Please dobara sign in karo.",
            }.get(language, "I couldn't confirm which account is signed in right now. Please sign in again."),
                ACCOUNT_SELF)
        phone = str((details or {}).get("verified_phone") or "").strip()
        parts = []
        if intent.subject in {"email", "both"}:
            parts.append({"hi": f"आप Jupiter में {login_email} से साइन इन हैं।",
                          "hi-Latn": f"Tum Jupiter mein {login_email} se signed in ho."}.get(
                language, f"You're signed in to Jupiter as {login_email}."))
        if intent.subject in {"phone", "both"}:
            if phone:
                parts.append({"hi": f"आपके अकाउंट पर वेरिफ़ाइड मोबाइल नंबर {phone} है।",
                              "hi-Latn": f"Tumhare account par verified mobile number {phone} hai."}.get(
                    language, f"The verified mobile number on your account is {phone}."))
            else:
                parts.append({"hi": "आपके Jupiter अकाउंट पर कोई वेरिफ़ाइड मोबाइल नंबर सेव नहीं है।",
                              "hi-Latn": "Tumhare Jupiter account par koi verified mobile number saved nahi hai."}.get(
                    language, "There's no verified mobile number saved on your Jupiter account."))
        _diagnostic(account_self=intent.subject, phone_on_record=str(bool(phone)).lower())
        return PersonalOutcome(True, " ".join(parts), ACCOUNT_SELF)

    # ── a call between two Jupiter users (marker 186): not supported, never dialled ─────────────────────────────
    # ── "call <user>": an urgent Jupiter call to ANOTHER Jupiter user, only when it really is urgent ─────────────
    _URGENT = re.compile(
        r"(?i)\b(?:emergenc\w*|urgent\w*|asap|immediately|right\s+now|critical|serious(?:ly)?|important|accident|"
        r"hospital|ambulance|injur\w*|hurt|bleed\w*|unconscious|faint\w*|heart\s+attack|chest\s+pain|fire|danger\w*|"
        r"unsafe|police|help\s+me|need\s+help|missing|dying|zaroor\w*|zarur\w*|turant|jaldi|abhi\s+ke\s+abhi|"
        r"madad|khatra|khatre|bimaar|tabiyat)\b")
    _CONFIRM = re.compile(
        r"(?i)^\W*(?:yes|yeah|yep|yup|ok(?:ay)?|sure|haan|han|ji|call(?:\s+(?:him|her|them|now|please|anyway))*|"
        r"please\s+call|do\s+it|go\s+ahead|it'?s\s+urgent|it\s+is\s+urgent|urgent|emergency|kar\s+do|karo|laga\s+do)\b")
    _DECLINE = re.compile(r"(?i)^\W*(?:no|nope|nah|nahi|nahin|mat|don'?t|do\s+not|cancel|never\s*mind|leave\s+it|"
                          r"message\s+(?:instead|them|him|her)|just\s+message|text\s+instead)\b")

    @staticmethod
    def _call_reason(text: str, name: str) -> str:
        """The user's words without the "call <name>" instruction ("Call the maintainer, he had an accident" -> "he had an
        accident"); empty when nothing but the instruction was said."""
        value = " ".join(str(text or "").split())
        index = value.casefold().find(str(name or "").casefold()) if name else -1
        rest = value[index + len(name):] if index >= 0 else ""
        rest = re.sub(r"(?i)^\s*(?:ko\s+)?(?:abhi\s+|turant\s+|jaldi\s+)?(?:video\s+)?(?:call|phone)?\s*"
                      r"(?:karo|kar\s*do|lagao|laga\s*do|milao)?", "", rest)
        return rest.strip(" ,.:;!?-")

    def _call_user(self, intent: PersonalIntent, text: str) -> PersonalOutcome:
        """Jupiter can ring another Jupiter user's devices (an in-app Jupiter call; there is no voice line between the
        two users): when they answer, Jupiter tells them who asked and why. Because that interrupts them like a phone
        call, Jupiter places it only when the request is urgent; otherwise it asks once and offers a message instead."""
        from services.delivery_claims import _language, relay_ambiguous_reply
        from services.personal_intents import _RELAY_PRONOUNS
        hinglish = _language(text) in {"hi", "hi-Latn"}
        name = str(intent.target or "").strip()
        find_users = getattr(self, "find_users", None)
        if getattr(self, "urgent_call", None) is None or find_users is None:
            _diagnostic(call_user="not_supported")
            return PersonalOutcome(True, f"I can't call {name or 'them'} from here. I can send them a Jupiter message "
                                         "instead.", CALL_USER)
        reason = self._call_reason(text, name)
        asked = bool(self._KNOW_QUESTION.search(text))
        if asked:                                  # "call my owner acc. u know my owner acc?": the question is no reason
            reason = self._KNOW_SENTENCE.sub("", reason).strip(" ,.:;!?-")
        given = EMAIL_ADDRESS.search(text)
        label = identifier_label(name) if name.casefold().startswith("my ") else ""
        if given:                                  # "call maintainer@example.invalid" / "call my owner acc maintainer@example.invalid"
            return self._call_identifier(given.group(0), text, label=label, reason="")
        if label:
            found = self._resolve_label(label)
            if found.get("ambiguous"):
                return self._which_person(found["ambiguous"], text)
            if found.get("recipient"):
                lead = self._known_lead(label, found, text) if asked else ""
                return self._call_recipient(found["recipient"], found["display"], reason, text, lead=lead)
            return self._ask_for_call_target(text, label=label, person=found.get("person") or "", reason=reason,
                                             identifier=found.get("identifier") or "")
        if not name or name.casefold() in _RELAY_PRONOUNS:
            return self._ask_for_call_target(text, reason=reason)
        try:
            others = [m for m in (find_users(name) or []) if str(m.get("email") or "").casefold() != self.email.casefold()]
        except Exception:
            others = []
        if len(others) > 1:
            return PersonalOutcome(True, relay_ambiguous_reply(text, name), CALL_USER)
        if not others:
            found = self._resolve_person(name)          # "call a friend": someone in People I Know
            if found.get("ambiguous"):
                return self._which_person(found["ambiguous"], text)
            if found.get("recipient"):
                return self._call_recipient(found["recipient"], found["display"], reason, text)
            if found.get("person"):
                return self._ask_for_call_target(text, person=found["person"], reason=reason,
                                                 identifier=found.get("identifier") or "")
            self.engine.remember_action(self.email, self.chat_id, "call_target", {"label": "", "person": "",
                                                                                  "reason": reason})
            return PersonalOutcome(True, (f"Mujhe {name} naam ka Jupiter account nahi mila, isliye koi call nahi lagi. "
                                          "Unka exact Jupiter username batao.") if hinglish else
                                   (f"I couldn't find a Jupiter account called {name}, so nothing rang. Tell me their "
                                    "exact Jupiter username."), CALL_USER)
        recipient = others[0]
        return self._call_recipient(recipient, str(recipient.get("username") or name), reason, text)

    # ── marker 223: who "my owner acc" / "my girlfriend" / "a friend" is, from what the user told Jupiter ──────────
    _CALL_TARGET_WINDOW = timedelta(seconds=90)
    _KNOW_QUESTION = re.compile(r"(?i)\b(?:(?:you|u|ya|tum|tu)\s+(?:know|remember)|jaante\s+ho|jante\s+ho|janti\s+ho|"
                                r"pata\s+hai)\b")
    _KNOW_SENTENCE = re.compile(r"(?i)[^.!?]*\b(?:know|remember|jaante|jante|janti|pata)\b[^.!?]*[.!?]*")
    _QUESTION_OR_COMMAND = re.compile(
        r"(?i)^\W*(?:what|who|whom|whose|which|when|where|why|how|is|are|can|could|would|will|should|tell|show|open|"
        r"play|set|remind|search|find|check|read|send|write|make|create|kya|kaun|kab|kahan|kyun|kaise)\b")

    def _call_recipient(self, recipient: dict, display: str, reason: str, text: str, *, lead: str = "") -> PersonalOutcome:
        """One resolved account: never the user's own; urgent -> ring now; otherwise ask once (the existing rule)."""
        from services.delivery_claims import _language
        hinglish = _language(text) in {"hi", "hi-Latn"}
        outcome = self._call_resolved(recipient, display, reason, text, hinglish)
        if lead:
            outcome.reply = f"{lead} {outcome.reply}"
        return outcome

    def _call_resolved(self, recipient: dict, display: str, reason: str, text: str, hinglish: bool) -> PersonalOutcome:
        self.engine.clear_action(self.email, self.chat_id, "call_target")        # the target is settled
        if str(recipient.get("email") or "").casefold() == self.email.casefold():
            return PersonalOutcome(True, f"{display} is the account you're signed in with right now, so there's nobody "
                                         "else to ring.", CALL_USER)
        if not self.persist:
            return PersonalOutcome(True, "This is a temporary chat, so I won't call anyone from here.", CALL_USER)
        if self._URGENT.search(text):
            return self._place_urgent_call(recipient, display, reason, text)
        self.engine.remember_action(self.email, self.chat_id, "call_draft",
                                    {"email": str(recipient["email"]), "name": display, "reason": reason})
        _diagnostic(call_user="confirm_urgency")
        if hinglish:
            return PersonalOutcome(True, f"Call karne se {display} ka phone urgent call ki tarah bajega. Kya yeh sach mein "
                                         "urgent hai? Agar haan, toh batao kya hua hai, main abhi call karti hoon. Warna "
                                         "main unhe Jupiter message bhej deti hoon.", CALL_USER)
        return PersonalOutcome(True, f"Calling rings {display}'s devices like an urgent call. Is this urgent? If it is, "
                                     "tell me what's happening and I'll call now. If it can wait, I can send them a "
                                     "Jupiter message instead.", CALL_USER)

    def _lookup_account(self, identifier: str) -> tuple[dict | None, int]:
        """(account, how many matched) for an exact email address or exact Jupiter username; never fuzzy."""
        identifier = str(identifier or "").strip()
        if not identifier:
            return None, 0
        try:
            if "@" in identifier:
                account = self.find_user(identifier.casefold())
                return (account, 1) if account else (None, 0)
            matches = list((self.find_users or (lambda _value: []))(identifier) or [])
        except Exception:
            return None, 0
        others = [m for m in matches if str(m.get("email") or "").casefold() != self.email.casefold()]
        if len(others) > 1:
            return None, len(others)
        if others:
            return others[0], 1
        return (matches[0], 1) if matches else (None, 0)

    @staticmethod
    def _identifier_of(entry: dict) -> str:
        """The Jupiter account the user gave for this person: a linked username, or an address in their facts."""
        linked = str(entry.get("jupiter_username") or "").strip()
        if linked:
            return linked
        for fact in reversed(entry.get("facts") or []):
            if not isinstance(fact, dict) or fact.get("superseded"):
                continue
            if fact.get("predicate") == "jupiter_account" and fact.get("value"):
                return str(fact["value"])
            found = EMAIL_ADDRESS.search(str(fact.get("text") or ""))
            if found:
                return found.group(0)
        return ""

    def _from_person(self, entry: dict) -> dict:
        name = str(entry.get("name") or "")
        identifier = self._identifier_of(entry)
        account, count = self._lookup_account(identifier) if identifier else (None, 0)
        if account is None and not identifier:
            account, count = self._lookup_account(name)                   # their saved name is their exact username
            account = account if count == 1 else None
        display = str((account or {}).get("username") or identifier or name)
        return {"person": name, "identifier": identifier, "recipient": account, "display": display}

    def _resolve_label(self, label: str) -> dict:
        """'owner account' -> the user's own note "My owner account is X"; 'girlfriend' -> the one person saved as the
        user's girlfriend, and that person's Jupiter account."""
        from services.personal_intents import identifier_statement
        try:
            notes = self.memory.self_notes()
        except Exception:
            notes = []
        for note in sorted(notes, key=lambda item: str(item.get("updated_at") or ""), reverse=True):
            parsed = identifier_statement(str(note.get("text") or ""))
            if parsed and parsed[0] == label:
                account, _ = self._lookup_account(parsed[1])
                return {"identifier": parsed[1], "recipient": account,
                        "display": str((account or {}).get("username") or parsed[1])}
        try:
            people = [entry for entry in self.memory.profile.list_people() if entry.get("name")]
        except Exception:
            people = []
        wanted = label.casefold()
        matches = [entry for entry in people if str(entry.get("name")).casefold() == wanted or re.search(
            rf"\b{re.escape(wanted)}\b", " ".join(str(entry.get(key) or "") for key in
                                                  ("relationship_label", "relationship")).casefold())]
        if len(matches) > 1:
            return {"ambiguous": [str(entry["name"]) for entry in matches][:4]}
        return self._from_person(matches[0]) if matches else {}

    def _resolve_person(self, name: str) -> dict:
        try:
            key, candidates = self.memory.find_person(name)
        except Exception:
            return {}
        if key is None and len(candidates) > 1:
            return {"ambiguous": [str(entry.get("name")) for entry in candidates][:4]}
        entry = self.memory.person(key) if key else None
        return self._from_person(entry) if entry else {}

    def _which_person(self, names: list[str], text: str) -> PersonalOutcome:
        listed = ", ".join(names[:-1]) + f" or {names[-1]}" if len(names) > 1 else (names or ["them"])[0]
        return PersonalOutcome(True, f"Which one should I call — {listed}?", CALL_USER)

    def _known_lead(self, label: str, found: dict, text: str) -> str:
        who = f"{found['person']} — " if found.get("person") else ""
        return f"Yes — your {label} is {who}{found.get('identifier') or found.get('display')}."

    def _ask_for_call_target(self, text: str, *, label: str = "", person: str = "", reason: str = "",
                             identifier: str = "") -> PersonalOutcome:
        """Nothing to ring yet: ask, and let the next reply (within 90 seconds) complete this call."""
        from services.delivery_claims import _language
        hinglish = _language(text) in {"hi", "hi-Latn"}
        self.engine.remember_action(self.email, self.chat_id, "call_target",
                                    {"label": label, "person": person, "reason": reason})
        _diagnostic(call_user="ask_target", known_label=str(bool(label)).lower(), known_person=str(bool(person)).lower())
        if identifier:
            return PersonalOutcome(True, f"I have {identifier} saved as your {label or person}, but there's no Jupiter "
                                         "account with that address, so nothing rang. Tell me their exact Jupiter "
                                         "username or email.", CALL_USER)
        if person:
            return PersonalOutcome(True, f"I know {person}, but I don't know their Jupiter account yet. Tell me their "
                                         "email or exact Jupiter username and I'll call them.", CALL_USER)
        if label:
            return PersonalOutcome(True, (f"Mujhe abhi nahi pata tumhara {label} kaunsa hai. Uska email ya exact Jupiter "
                                          "username batao, main yaad rakhungi aur call karungi.") if hinglish else
                                   (f"I don't know which account is your {label} yet. Tell me its email or exact "
                                    "Jupiter username and I'll remember it and call."), CALL_USER)
        return PersonalOutcome(True, "Kisko call karun? Unka exact Jupiter username batao." if hinglish else
                               "Who should I call? Tell me their exact Jupiter username.", CALL_USER)

    def _remember_identifier(self, label: str, identifier: str, person: str = "") -> bool:
        """Save "my owner account is X" (or X as that person's Jupiter account) through the per-turn memory step."""
        if not self.persist or not identifier or not (label or person):
            return False
        from memory.turn_memory import apply
        if person:
            operation = {"op": "person_fact", "name": person, "predicate": "jupiter_account", "value": identifier,
                         "old_value": None, "single": True}
        else:
            from memory.turn_memory import identifier_operations
            people = [{"name": str(entry.get("name") or ""),
                       "relationship": str(entry.get("relationship_label") or entry.get("relationship") or "")}
                      for entry in self.memory.profile.list_people() if entry.get("name")]
            operations = identifier_operations(f"my {label} is {identifier}", people)
            if not operations:
                return False
            operation = operations[0]
        try:
            receipt = apply(self.email, [operation], chat_id=self.chat_id, message_id=self.message_id)
        except Exception as error:
            Logger.warning(f"personal_assistant identifier save failed error_class={type(error).__name__}")
            return False
        _diagnostic(memory_operation="write", memory_result=receipt.kind, memory_scope="call_identifier")
        return receipt.saved

    def _call_identifier(self, identifier: str, text: str, *, label: str = "", person: str = "",
                         reason: str = "") -> PersonalOutcome:
        saved = self._remember_identifier(label, identifier, person)
        lead = (f"Got it — I'll remember {identifier} as {person}'s Jupiter account." if saved and person else
                f"Got it — I'll remember that your {label} is {identifier}." if saved and label else "")
        account, count = self._lookup_account(identifier)
        if count > 1:
            return PersonalOutcome(True, " ".join(part for part in (lead, f"More than one Jupiter account is called "
                                   f"{identifier}, so nothing rang. Tell me their unique Jupiter username or email.") if part),
                                   CALL_USER)
        if account is None:
            return PersonalOutcome(True, " ".join(part for part in (lead, f"I couldn't find a Jupiter account for "
                                   f"{identifier}, so nothing rang.") if part), CALL_USER)
        return self._call_recipient(account, str(account.get("username") or identifier), reason, text, lead=lead)

    def _continue_call_target(self, text: str) -> PersonalOutcome:
        """The reply to "Who should I call?" within 90 seconds: an email, a username, a name or "my <role>"."""
        engine = getattr(self, "engine", None)
        if engine is None or getattr(self, "urgent_call", None) is None:
            return PersonalOutcome()
        pending = engine.last_action(self.email, self.chat_id, "call_target", max_age=self._CALL_TARGET_WINDOW)
        if not pending:
            return PersonalOutcome()
        from services.personal_intents import identifier_statement
        value = " ".join(str(text or "").split()).strip()
        if self._DECLINE.search(value):
            engine.clear_action(self.email, self.chat_id, "call_target")
            return PersonalOutcome(True, "Okay, I won't call anyone.", CALL_USER)
        label, person, reason = (str(pending.get(key) or "") for key in ("label", "person", "reason"))
        given = EMAIL_ADDRESS.search(value)
        statement = identifier_statement(value)
        if statement:                                          # "my owner acc maintainer@example.invalid"
            label, person = statement[0], person if statement[0] == label or not label else ""
            engine.clear_action(self.email, self.chat_id, "call_target")
            return self._call_identifier(statement[1], text, label=label, person=person, reason=reason)
        if given and len(value.split()) <= 8:                  # "maintainer@example.invalid" / "it's maintainer@example.invalid"
            engine.clear_action(self.email, self.chat_id, "call_target")
            return self._call_identifier(given.group(0), text, label=label, person=person, reason=reason)
        if value.endswith("?") or self._QUESTION_OR_COMMAND.match(value) or len(value.split()) > 5:
            return PersonalOutcome()                           # a new request: normal chat
        candidate = re.sub(r"(?i)^(?:call\s+|it'?s\s+|its\s+|it\s+is\s+|this\s+is\s+)", "", value).strip(" .!,")
        if candidate.casefold().startswith(("my ", "mera ", "meri ", "mere ")):
            found = self._resolve_label(identifier_label(candidate))
        else:
            account, count = self._lookup_account(candidate)
            if count > 1:
                engine.clear_action(self.email, self.chat_id, "call_target")
                return PersonalOutcome(True, f"More than one Jupiter account is called {candidate}, so nothing rang. "
                                             "Tell me their unique Jupiter username or email.", CALL_USER)
            if account is not None:
                engine.clear_action(self.email, self.chat_id, "call_target")
                saved = self._remember_identifier(label, str(account.get("username") or candidate), person)
                lead = f"Got it — I'll remember that your {label} is {account.get('username') or candidate}." \
                    if saved and label else ""
                return self._call_recipient(account, str(account.get("username") or candidate), reason, text, lead=lead)
            found = self._resolve_person(candidate)
        if found.get("ambiguous"):
            return self._which_person(found["ambiguous"], text)
        if found.get("recipient"):
            engine.clear_action(self.email, self.chat_id, "call_target")
            return self._call_recipient(found["recipient"], found["display"], reason, text)
        if found.get("person"):
            return self._ask_for_call_target(text, person=found["person"], reason=reason,
                                             identifier=found.get("identifier") or "")
        return PersonalOutcome(True, f"I couldn't find a Jupiter account called {candidate}, so nothing rang. Tell me "
                                     "their exact Jupiter username or email.", CALL_USER)

    def _identifier_question(self, text: str) -> PersonalOutcome:
        """"u know my owner acc?" — answered from what the user told Jupiter; unknown labels go to normal chat unless
        the user is clearly asking about an account."""
        from services.personal_intents import identifier_question_label
        label = identifier_question_label(text)
        if not label or not self.persist:
            return PersonalOutcome()
        found = self._resolve_label(label)
        if found.get("identifier") or found.get("recipient"):
            who = f"{found['person']}, " if found.get("person") else ""
            ident = found.get("identifier") or found.get("display")
            tail = " Say “call my " + label + "” whenever you want me to ring it." if found.get("recipient") else ""
            _diagnostic(identifier_question="known")
            return PersonalOutcome(True, f"Yes — your {label} is {who}{ident}.{tail}", MEMORY_READ)
        if found.get("ambiguous"):
            return PersonalOutcome(True, f"You've told me about more than one {label}: {', '.join(found['ambiguous'])}. "
                                         "Which one do you mean?", MEMORY_READ)
        if not re.search(r"\b(?:account|email|username|address)\b", label):
            return PersonalOutcome()
        _diagnostic(identifier_question="unknown")
        return PersonalOutcome(True, f"No — you haven't told me your {label} yet. Tell me its email or Jupiter username "
                                     f"(for example “my {label} maintainer@example.invalid”) and I'll remember it.", MEMORY_READ)

    def _continue_call_draft(self, text: str) -> PersonalOutcome:
        engine = getattr(self, "engine", None)
        if engine is None:
            return PersonalOutcome()
        draft = engine.last_action(self.email, self.chat_id, "call_draft", max_age=timedelta(minutes=10))
        if not draft or not draft.get("email"):
            return PersonalOutcome()
        name = str(draft.get("name") or "them")
        if self._DECLINE.search(text):
            engine.clear_action(self.email, self.chat_id, "call_draft")
            return PersonalOutcome(True, f"Okay, I won't call {name}. Say \"tell {name} that …\" and I'll send a "
                                         "Jupiter message.", CALL_USER)
        if not (self._CONFIRM.search(text) or self._URGENT.search(text)):
            return PersonalOutcome()
        engine.clear_action(self.email, self.chat_id, "call_draft")
        extra = self._CONFIRM.sub("", text, count=1).strip(" ,.:;!?-")
        reason = " ".join(part for part in (draft.get("reason") or "", extra) if part).strip()
        return self._place_urgent_call({"email": draft["email"], "username": name}, name, reason, text)

    def _place_urgent_call(self, recipient: dict, display: str, reason: str, text: str) -> PersonalOutcome:
        from services.delivery_claims import _language
        hinglish = _language(text) in {"hi", "hi-Latn"}
        try:
            sender = self.find_user(self.email) or {}
        except Exception:
            sender = {}
        body = reason or f"{sender.get('username') or 'Someone'} asked Jupiter to call you urgently."
        result = self.urgent_call(str(recipient["email"]), body, source_message_id=self.message_id or None) or {}
        status = result.get("status")
        _diagnostic(call_user=f"urgent_{status or 'unknown'}")
        quoted = f" ({reason[:200]})" if reason else ""
        if status == "ringing":
            if hinglish:
                return PersonalOutcome(True, f"{display} ko abhi urgent Jupiter call kar rahi hoon{quoted}, aur tumhara "
                                             "message unke Jupiter inbox mein bhi rakh diya hai. Woh uthayenge toh main unhe "
                                             "bataungi kisne aur kyun call kiya. Abhi pata nahi woh uthayenge ya nahi.", CALL_USER)
            return PersonalOutcome(True, f"I'm calling {display} now as an urgent Jupiter call{quoted}, and I've left "
                                         "your message in their Jupiter inbox. If they answer, I'll tell them who asked me "
                                         "to call and why. I can't tell yet whether they'll pick up.", CALL_USER)
        if status in {"suppressed", "missed"}:
            why = "they're already on a Jupiter call" if result.get("end_reason") == "busy" else "their call settings"
            return PersonalOutcome(True, f"{display}'s devices couldn't ring right now ({why}), so I left your urgent "
                                         "message in their Jupiter inbox. They'll see a missed urgent call.", CALL_USER)
        if status == "recent":
            return PersonalOutcome(True, f"I already called {display} urgently in the last few minutes, so I won't ring "
                                         "them again yet. Your message is in their Jupiter inbox.", CALL_USER)
        return PersonalOutcome(True, f"I couldn't reach {display}. Nothing rang and nothing was sent.", CALL_USER)

    # ── another user's contact details: mutual consent only (marker 185) ─────────────────────────────────────────
    def _contact_request(self, intent: PersonalIntent, text: str) -> PersonalOutcome:
        """Never looks anything up about the other person. It files a consent request to exactly one account through the
        router (server-authorised, bound to this requester) and says truthfully what happened. Contact values are never
        put in chat: an approved request is viewed once in Settings › Contact requests."""
        from services.delivery_claims import _language
        language = _language(text)
        fields = [field for field in (intent.content or "email").split(",") if field in {"email", "phone"}] or ["email"]
        if self.contact_request is None:
            return PersonalOutcome()                    # no authenticated request path here: normal chat answers
        try:
            result = self.contact_request(intent.target, fields) or {}
        except Exception:
            result = {"status": "error"}
        status = str(result.get("status") or "error")
        if status == "not_found" and intent.reference != "explicit":
            return PersonalOutcome()                    # not a Jupiter account: maybe the user's own memory/question
        if status == "self":
            return self._account_self(PersonalIntent(ACCOUNT_SELF, subject="both" if len(fields) == 2 else (
                "phone" if fields == ["phone"] else "email")), text)
        name = str(result.get("owner_name") or intent.target)
        what_en = {("email",): "email", ("phone",): "verified mobile number"}.get(tuple(fields),
                                                                                 "email and verified mobile number")
        what_hi = {("email",): "email", ("phone",): "verified mobile number"}.get(tuple(fields),
                                                                                 "email aur verified mobile number")
        _diagnostic(contact_request=status, fields=",".join(fields))
        hinglish = language in {"hi", "hi-Latn"}
        replies = {
            "created": (f"Maine {name} ko Jupiter mein request bhej di hai ki woh apna {what_hi} tumhare saath share "
                        "karein. Unke approve karne tak kuch share nahi hoga; approve hone par tum use Settings › Contact "
                        "requests mein ek baar dekh paoge.") if hinglish else (
                f"I've sent {name} a request in Jupiter to share their {what_en} with you. Nothing is shared unless they "
                "approve; when they do, you can view it once in Settings › Contact requests."),
            "pending": (f"Tumhari request {name} ke paas pehle se hai aur abhi unke approval ka wait kar rahi hai.")
            if hinglish else f"You've already asked {name}; that request is still waiting for their approval.",
            "approved_by_policy": (f"{name} ne trusted connections ke liye yeh share karna chalu rakha hai. Settings › "
                                   "Contact requests mein ise ek baar dekh sakte ho.") if hinglish else (
                f"{name} has chosen to share this with trusted connections. Open Settings › Contact requests to view it "
                "once."),
            "unavailable": "Abhi yeh request nahi bhej paayi." if hinglish else "I couldn't send that request.",
            "approved": (f"{name} ne approve kar diya hai. Settings › Contact requests mein ise ek baar dekh sakte ho.")
            if hinglish else f"{name} approved your request. Open Settings › Contact requests to view it (shown once).",
            "ambiguous": ("Is naam se ek se zyada Jupiter accounts milte hain. Unka exact Jupiter username batao.")
            if hinglish else "More than one Jupiter account matches that name. Tell me their exact Jupiter username.",
            "username_required": ("Email address se request nahi bhej sakti. Unka Jupiter username batao.") if hinglish
            else "I can't request details by email address. Tell me their Jupiter username instead.",
            "not_found": (f"Mujhe {intent.target} naam ka Jupiter account nahi mila. Unka exact Jupiter username batao.")
            if hinglish else f"I couldn't find a Jupiter account for {intent.target}. Tell me their exact Jupiter username.",
        }
        reply = replies.get(status) or ("Abhi request nahi bhej paayi. Thodi der baad try karo." if hinglish else
                                        "I couldn't send that request right now. Please try again in a moment.")
        return PersonalOutcome(True, reply, CONTACT_REQUEST)

    # ── messages for another person (marker 182) ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _relay_username_span(text: str, target: str, find_users) -> tuple[str, list]:
        """The longest exact registered username starting at ``target`` (which the intent captured as one word), e.g.
        "Tell the project creator that …" -> "the project creator". Spans of the target plus up to three following words are tried
        longest first; only exact (case-insensitive) username matches count, never fuzzy ones. Returns (span, matches)
        for the first span with any match, or (target, []) when none matches."""
        import re as _re
        value = " ".join(str(text or "").split())
        index = value.casefold().find(target.casefold())
        if index < 0:
            return target, []
        words = value[index:].split(" ")
        before = value[:index].split()
        candidates = []
        for count in range(min(4, len(words)), 0, -1):          # "Tell the project creator that …": the name starts here
            if any(_re.search(r"[,.:;!?]$", word) for word in words[:count - 1]):
                continue                    # a span never runs past punctuation ("the maintainer, that …" stops at "the maintainer")
            candidates.append(" ".join(words[:count]))
        tail = _re.sub(r"[,.:;!?]+$", "", words[0])
        for count in range(min(3, len(before)), 0, -1):          # "the project creator ko bata do …": the name ENDS here
            lead = before[-count:]
            if any(_re.search(r"[,.:;!?]$", word) for word in lead):
                continue
            candidates.append(" ".join([*lead, tail]))
        spans = [_re.sub(r"[,.:;!?]+$", "", span) for span in sorted(candidates, key=lambda s: -len(s.split()))]
        for span in dict.fromkeys(spans):
            try:
                matches = list(find_users(span) or [])
            except Exception:
                matches = []
            if matches:
                return span, matches
        return target, []

    @staticmethod
    def _relay_display_name(text: str, target: str) -> str:
        """The person's name exactly as the user wrote it, for a "nothing was sent" reply. The intent captures one word,
        so "the project creator ko bata do …" used to be answered "Maine Bairagya ko kuch nahi bheja" (the bare surname).
        Capitalised words directly before / after that word belong to the same name."""
        value = " ".join(str(text or "").split())
        index = value.find(target) if target else -1
        if index < 0 or not target[:1].isupper():
            return target
        before = value[:index].split()
        after = value[index + len(target):].split()
        verbs = {"tell", "message", "msg", "text", "ping", "notify", "alert", "inform", "remind", "warn", "dm", "let",
                 "send", "forward", "please", "again", "also", "now", "quickly"}

        def is_name(word: str) -> bool:
            return word[:1].isupper() and word.isalpha() and word.casefold() not in verbs

        lead = []
        for word in reversed(before[-2:]):
            if not is_name(word):
                break
            lead.insert(0, word)
        trail = []
        if not target.endswith((",", ".", ":", ";", "!", "?")):
            for word in after[:2]:
                if not is_name(word.strip(",.:;!?")):
                    break
                trail.append(word.strip(",.:;!?"))
                if word.endswith((",", ".", ":", ";", "!", "?")):
                    break
        return " ".join([*lead, target, *trail])

    def _relay(self, intent: PersonalIntent, text: str) -> PersonalOutcome:
        """Marker 182, corrected in 185. Jupiter's ONE real user-to-user channel is a directed message to one exact
        registered Jupiter account (stored server-side, shown in that account's Jupiter app). A relay whose target
        resolves to exactly one such account is queued there; anything else (a pronoun, an unknown person, a phone,
        SMS, WhatsApp) is truthfully "not sent". Temporary/testing chats never send (same rule as the existing
        background directed-message path), and the message id deduplicates retries."""
        import sqlite3
        from services.delivery_claims import (relay_ambiguous_reply, relay_body, relay_needs_body_reply,
                                              relay_not_sent_reply, relay_queued_reply, relay_unavailable_reply,
                                              relay_username_required_reply)
        from services.personal_intents import _RELAY_PRONOUNS
        target = str(intent.target or "").strip()
        recipient = None
        find_user = getattr(self, "find_user", None)
        find_users = getattr(self, "find_users", None)
        if "@" in target:
            # Jupiter messages go to usernames. No address lookup (it would reveal whether the address is registered),
            # no send, and the turn is answered here so it can never fall through to Gmail.
            _diagnostic(relay_result="not_sent_username_required")
            return PersonalOutcome(True, relay_username_required_reply(text), RELAY_MESSAGE)
        if (target and find_users is not None and target.casefold() not in _RELAY_PRONOUNS
                and not target.casefold().startswith("my ")):
            span, matches = self._relay_username_span(text, target, find_users)
            others = [m for m in matches if str(m.get("email") or "").casefold() != self.email.casefold()]
            if len(others) > 1:
                _diagnostic(relay_result="not_sent_ambiguous")
                return PersonalOutcome(True, relay_ambiguous_reply(text, span), RELAY_MESSAGE)
            if others:
                recipient, target = others[0], span
            find_user = None                              # exact username resolution is authoritative here
        if target and target.casefold() not in _RELAY_PRONOUNS and find_user is not None:
            lookup = target
            if target.casefold().startswith("my ") and getattr(self, "memory", None) is not None:
                key, _ = self.memory.find_person(target[3:])           # "my mom": only an explicit username link
                linked = str((self.memory.person(key) or {}).get("jupiter_username") or "").strip() if key else ""
                lookup = linked or ""
            try:
                recipient = find_user(lookup) if lookup else None
            except Exception:
                recipient = None
        if not recipient or str(recipient.get("email") or "").casefold() == self.email.casefold():
            _diagnostic(relay_result="not_sent_no_recipient")
            return PersonalOutcome(True, relay_not_sent_reply(text, self._relay_display_name(text, str(intent.target or ""))),
                                   RELAY_MESSAGE)
        name = str(recipient.get("username") or target)
        body = relay_body(text, target)
        if not body:
            return PersonalOutcome(True, relay_needs_body_reply(text, name), RELAY_MESSAGE)
        if not self.persist:
            return PersonalOutcome(True, "This is a temporary chat, so I won't send a Jupiter message from here.",
                                   RELAY_MESSAGE)
        directed_send = getattr(self, "directed_send", None)
        if directed_send is not None:
            result = directed_send(str(recipient["email"]), body, source_message_id=self.message_id or None) or {}
            if result.get("status") not in {"queued", "duplicate"}:
                _diagnostic(relay_result="unavailable")
                return PersonalOutcome(True, relay_unavailable_reply(text, name), RELAY_MESSAGE)
            _diagnostic(relay_result="queued_directed_message", alert=str(result.get("alert") or ""))
            return PersonalOutcome(True, relay_queued_reply(text, name, body, str(result.get("alert") or ""),
                                                            bool(result.get("inbox_enabled", True))), RELAY_MESSAGE)
        sender = find_user(self.email) or {}
        try:
            self.engine.send_directed(self.email, str(recipient["email"]), str(sender.get("username") or "A Jupiter user"),
                                      body, source_message_id=self.message_id or None)
        except sqlite3.IntegrityError:
            pass                                                        # this message id was already queued: no duplicate
        except ValueError:
            return PersonalOutcome(True, relay_not_sent_reply(text, intent.target), RELAY_MESSAGE)
        _diagnostic(relay_result="queued_directed_message")
        return PersonalOutcome(True, relay_queued_reply(text, name, body), RELAY_MESSAGE)

    # ── task completion notifications (marker 159; review repair) ───────────────────────────────────────────────
    def _task_notify(self, intent: PersonalIntent, text: str) -> PersonalOutcome:
        """Attach the user's notification preference to the work it is about and say truthfully what WILL happen.

        * Work + notification ("Write a hello.txt file. Notify me when done."): the work continues through normal
          chat exactly once (``continue_with``) and a tracker is created in AWAITING_WORKER: Jupiter does not start
          Claude Code / Codex / AntiGravity, and the tracker becomes RUNNING only when its worker reports. The same
          request repeated in the same chat while that tracker is open is neither tracked nor dispatched again.
        * Notification only ("Tell me if it fails."): attached to the existing unfinished task it refers to, or
          Jupiter asks which task; no empty tracker is created.
        The worker's report key is never put in chat: its owner issues it for THIS task in Settings."""
        import hashlib
        from services.personal_intents import split_work_and_notification
        from services.task_notifications import TaskNotificationStore, channel_plan, parse_preference
        work, notify_text = split_work_and_notification(text)
        preference = parse_preference(notify_text or text)
        plan = channel_plan(preference)
        usable = [item["channel"] for item in plan["usable"]]
        names = {"app": "here in Jupiter", "windows": "with a Windows notification", "android": "in the Jupiter app",
                 "call": "with a Jupiter call in the app", "email": "by email", "whatsapp": "on WhatsApp",
                 "sms": "by SMS", "phone": "with a call to your phone number"}
        when = {"failure": "if it fails", "needs_attention": "if it needs you",
                "success": "when it finishes"}.get(preference.trigger, "when it finishes, fails or needs you")
        how = (f"{' and '.join(names[c] for c in usable)} {when}" if usable else
               f"here in Jupiter {when}" if plan["fallback"] else "")
        notes = [f"I can't reach you {names.get(item['channel'], item['channel'])} yet: {item['reason']}"
                 for item in plan["unavailable"]]
        if plan["needs_choice"]:
            notes.append("Because you asked for only that channel, I won't switch to another one. Choose another "
                         "channel in Settings › Plugins › Task notifications if you'd like one.")
        if plan["never"]:
            nouns = {"phone": "phone-number calls", "call": "Jupiter calls", "whatsapp": "WhatsApp", "sms": "SMS", "email": "email",
                     "windows": "Windows notifications", "android": "the Android app", "app": "in-app messages"}
            never = [c for c in plan["never"] if c != "phone"] if {"call", "phone"} <= set(plan["never"]) else plan["never"]
            if {"call", "phone"} <= set(plan["never"]):
                nouns = {**nouns, "call": "calls"}                     # "don't call" rules out every kind of call
            notes.append(f"I won't use {' or '.join(nouns.get(c, c) for c in never)}.")
        store = TaskNotificationStore(path=self.engine.path)
        _diagnostic(task_notify_channels=",".join(usable) or "none", task_notify_trigger=preference.trigger,
                    task_notify_has_work=bool(work))

        if not work:
            open_tasks = store.open_tasks(self.email)
            here = [task for task in open_tasks if self.chat_id and task.get("chat_id") == self.chat_id]
            candidates = here or open_tasks
            if len(candidates) != 1:
                if candidates:
                    listed = "; ".join(f"“{task['title'][:60]}”" for task in candidates[:3])
                    ask = f"Which task should I tell you about? You have {len(candidates)} unfinished: {listed}."
                else:
                    ask = ("I don't have an unfinished task to watch yet. Which task should I tell you about? Send "
                           "it with the request, for example “Build the release. Tell me when it's done.”")
                return PersonalOutcome(True, " ".join([ask, *notes]), TASK_NOTIFY)
            task = store.update_preference(self.email, candidates[0]["task_id"], preference)
            lead = f"Okay — for “{task['title'][:80]}” I'll tell you {how}." if how else \
                f"Okay — I've noted that for “{task['title'][:80]}”."
            return PersonalOutcome(True, " ".join([lead, *notes]), TASK_NOTIFY,
                                   metadata={"task_notification": {"task_id": task["task_id"], "run_id": task["run_id"],
                                                                   "status": task["status"]}})

        title = work.rstrip(" .!?;")[:120] or "Your task"
        # Marker 174: work Jupiter can do itself runs in the background now; Claude Code / Codex / builds / scripts
        # still wait for that worker to connect and report.
        from services.background_jobs import JUPITER_CHECK
        jupiter_runs = bool(self.persist) and not _EXTERNAL_WORKER.search(work)
        request_key = hashlib.sha256(
            f"{self.chat_id or ''}|{' '.join(work.casefold().split())}".encode("utf-8")).hexdigest()[:40]
        created = store.create_task(self.email, title, preference, chat_id=self.chat_id or "",
                                    request_key=request_key, issue_key=False,
                                    completion_check=JUPITER_CHECK if jupiter_runs else "")
        metadata = {"task_notification": {"task_id": created["task_id"], "run_id": created["run_id"],
                                           "status": created["status"],
                                           "setup": "jupiter" if jupiter_runs else "settings"}}
        if created.get("duplicate"):
            reply = (f"I'm already tracking “{created['title'][:80]}” (waiting for a worker to report), so I haven't "
                     "started or tracked it a second time.")
            return PersonalOutcome(True, reply, TASK_NOTIFY, metadata=metadata)
        self.engine.remember_action(self.email, self.chat_id, "task_notify", {"task_id": created["task_id"]})
        if jupiter_runs:
            metadata["task_notification"]["runner"] = "jupiter"
            metadata["_background_work"] = work                    # consumed by the API (never sent to the client)
            lead = f"On it — I'm working on “{title[:80]}” in the background, so you can close Jupiter."
            follow = f"When it's done I'll tell you {how}." if how else "I'll save the result in this chat."
            return PersonalOutcome(True, " ".join(part for part in [lead, follow, *notes] if part), TASK_NOTIFY,
                                   metadata=metadata)
        lead = (f"I'm tracking “{title[:80]}”, but nothing has started it yet: it's waiting for a worker (Claude Code, "
                "Codex or your own script) to connect in Settings › Plugins › Task notifications › Connect worker.")
        follow = (f"When that worker reports a result that passes the check you register there, I'll tell you {how}."
                  if how else "")
        return PersonalOutcome(True, " ".join(part for part in [lead, follow, *notes] if part), TASK_NOTIFY,
                               continue_with=work, metadata=metadata)

    # ── memory ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _person_or_question(self, name: str) -> tuple[str | None, PersonalOutcome | None]:
        key, candidates = self.memory.find_person(name)
        if key is None and len(candidates) > 1:
            names = [str(entry.get("name")) for entry in candidates][:4]
            listed = ", ".join(names[:-1]) + f" or {names[-1]}"
            return None, PersonalOutcome(True, f"I know more than one {name} — {listed}? Tell me which one.")
        return key, None

    def _memory_write(self, intent: PersonalIntent, _text: str) -> PersonalOutcome:
        content = intent.content
        if intent.subject and intent.subject != SELF:
            key, question = self._person_or_question(intent.subject)
            if question:
                return question
            result = self.memory.remember_about_person(intent.subject if key is None else (self.memory.person(key) or {}).get("name", intent.subject),
                                                       content, chat_id=self.chat_id, message_id=self.message_id)
            if result["status"] == "ambiguous":
                names = result["candidates"][:4]
                return PersonalOutcome(True, f"I know more than one {intent.subject} — {', '.join(names)}. Tell me which one.")
            _diagnostic(memory_operation="write", memory_result=result["status"], memory_scope="person",
                        memory_dedupe_action=result["status"] if result["status"] in {"reinforced", "updated"} else "")
            if result["status"] == "failed":
                return PersonalOutcome(True, SAVE_FAILED)
            self.engine.remember_action(self.email, self.chat_id, "memory",
                                        {"type": "person", "key": result["key"], "fact_id": result["fact_id"], "name": result["name"]})
            name = result["name"].split()[0] if len(result["name"].split()) > 1 and self.memory.find_person(result["name"].split()[0])[0] else result["name"]
            reply = {
                "reinforced": f"Got it — I already knew that about {name}, so I've kept it up to date.",
                "updated": f"Got it — I've updated what I know about {name}.",
            }.get(result["status"], f"Got it — I'll remember that about {name}.")
            if result.get("birthday"):
                birthday = result["birthday"]
                reply += f" I've noted {name}'s birthday: {birthday['day']} {datetime(2000, birthday['month'], 1).strftime('%B')}."
            outcome = PersonalOutcome(True, reply, MEMORY_WRITE, metadata={"memory": {"scope": "person", "name": result["name"]}})
        else:
            result = self.memory.remember_about_self(content, chat_id=self.chat_id, message_id=self.message_id)
            _diagnostic(memory_operation="write", memory_result=result["status"], memory_scope="self",
                        memory_dedupe_action=result["status"] if result["status"] in {"reinforced", "updated"} else "")
            if result["status"] == "failed":
                return PersonalOutcome(True, SAVE_FAILED)
            self.engine.remember_action(self.email, self.chat_id, "memory", {"type": "self", "memory_id": result["memory_id"]})
            reply = {"reinforced": "Got it — I already had that, so I've kept it up to date.",
                     "updated": "Got it — I've updated that."}.get(result["status"], "Got it — I'll remember that.")
            outcome = PersonalOutcome(True, reply, MEMORY_WRITE, metadata={"memory": {"scope": "self"}})
        if intent.remainder:
            outcome.continue_with = intent.remainder
        return outcome

    def _memory_update(self, intent: PersonalIntent, _text: str) -> PersonalOutcome:
        if not intent.subject or intent.subject in {SELF, LAST}:
            return PersonalOutcome()
        key, question = self._person_or_question(intent.subject)
        if question:
            return question
        name = (self.memory.person(key) or {}).get("name", intent.subject) if key else intent.subject
        result = self.memory.remember_about_person(name, intent.content, chat_id=self.chat_id, message_id=self.message_id,
                                                   replace=True)
        _diagnostic(memory_operation="update", memory_result=result["status"], memory_scope="person")
        if result["status"] in {"failed", "ambiguous"}:
            return PersonalOutcome(True, SAVE_FAILED)
        self.engine.remember_action(self.email, self.chat_id, "memory",
                                    {"type": "person", "key": result["key"], "fact_id": result["fact_id"], "name": result["name"]})
        return PersonalOutcome(True, f"Got it — I've updated what I remember about {result['name']}.")

    def _memory_read(self, intent: PersonalIntent, _text: str) -> PersonalOutcome:
        if intent.subject == SELF:
            notes = self.memory.self_notes()
            people = [entry for entry in self.memory.profile.list_people() if entry.get("name")]
            _diagnostic(memory_operation="read", memory_retrieval_count=len(notes), memory_scope="self")
            if not notes and not people:
                return PersonalOutcome(True, "You haven't asked me to remember anything specific yet. "
                                             "Just say \"remember that …\" whenever you want me to keep something.")
            lines = [f"• {_second_person_fact(note['text'])}" for note in notes[:10]]
            reply = ("Here's what you've asked me to remember:\n" + "\n".join(lines)) if lines else ""
            if people:
                names = [str(entry["name"]) for entry in people[:12]]
                reply += ("\n\n" if reply else "") + "I also know about the people you've told me about: " + ", ".join(names) + "."
            return PersonalOutcome(True, reply, metadata={"memory": {"scope": "self", "count": len(notes)}})
        if not intent.subject or intent.subject == LAST:
            return PersonalOutcome()
        key, question = self._person_or_question(intent.subject)
        if question:
            return question
        if key is None:
            return PersonalOutcome()        # not someone the user told me about: normal chat (may be a public figure)
        entry = self.memory.person(key) or {}
        facts = [fact for fact in entry.get("facts") or [] if isinstance(fact, dict) and fact.get("text")]
        details = [str(note) for note in entry.get("details") or [] if str(note).strip()]
        _diagnostic(memory_operation="read", memory_retrieval_count=len(facts) + len(details), memory_scope="person")
        name = entry.get("name", intent.subject)
        lines = [f"• {_second_person_fact(fact['text'], 900 if fact.get('kind') == 'context' else 240)}"
                 for fact in facts[-8:]] + [f"• {_brief(note)}" for note in details[:3]]
        label = entry.get("relationship_label")
        header = f"Here's what I know about {name}" + (f" — your {label}" if label else "") + ":"
        if not lines:
            return PersonalOutcome(True, f"I know {name} is someone you've mentioned, but you haven't told me anything specific yet.")
        return PersonalOutcome(True, header + "\n" + "\n".join(lines))

    def _memory_forget(self, intent: PersonalIntent, _text: str) -> PersonalOutcome:
        if intent.content and not intent.subject and intent.reference != LAST:
            result = self.memory.forget_matching(intent.content)
            _diagnostic(memory_operation="forget", memory_result="removed" if result["removed"] else
                        ("ambiguous" if result["candidates"] else "missing"), memory_scope="fact")
            if result["removed"]:
                return PersonalOutcome(True, f"Done — I've forgotten that: {result['text']}")
            if result["candidates"]:
                listed = "; ".join(f"“{text[:80]}”" for text in result["candidates"])
                return PersonalOutcome(True, f"I have more than one note like that: {listed}. Which one should I forget?")
            goal = self._forget_goal(intent.content)
            if goal is not None:
                return goal
            return PersonalOutcome(True, "I don't have that saved, so there's nothing to forget.")
        if intent.reference == LAST:
            action = self.engine.last_action(self.email, self.chat_id, "memory")
            if not action:
                return PersonalOutcome()      # nothing saved in this conversation: let normal chat handle "forget that"
            if action.get("type") == "person":
                ok = self.memory.forget_person_fact(action.get("key", ""), action.get("fact_id", ""))
            else:
                ok = self.memory.forget_memory(action.get("memory_id", ""))
            _diagnostic(memory_operation="forget", memory_result="removed" if ok else "missing", memory_scope=action.get("type"))
            self.engine.clear_action(self.email, self.chat_id, "memory")
            return PersonalOutcome(True, "Done — I've forgotten that." if ok else "That's already gone — there's nothing left to forget.")
        if intent.subject == SELF:
            removed = self.memory.forget_all_self_notes()
            _diagnostic(memory_operation="forget", memory_result=f"removed_{removed}", memory_scope="self")
            return PersonalOutcome(True, "Done — I've forgotten the notes you asked me to keep about you." if removed else
                                         "You hadn't asked me to keep any notes about you.")
        key, question = self._person_or_question(intent.subject)
        if question:
            return question
        if key is None:
            goal = self._forget_goal(intent.subject)
            if goal is not None:
                return goal
            return PersonalOutcome(True, f"I don't have anything saved about {intent.subject}.")
        result = self.memory.forget_person(key)
        _diagnostic(memory_operation="forget", memory_result="removed" if result.get("removed") else "failed",
                    memory_scope="person", memory_share_scope=f"unshared_{result.get('unshared', 0)}")
        if not result.get("removed"):
            return PersonalOutcome(True, "I couldn't forget that right now.")
        self.engine.clear_action(self.email, self.chat_id, "memory")
        extra = " I've also stopped sharing it with anyone." if result.get("unshared") else ""
        return PersonalOutcome(True, f"Done — I've forgotten what you told me about {result.get('name') or intent.subject}.{extra}")

    def _forget_goal(self, statement: str) -> PersonalOutcome | None:
        """A learned goal (e.g. "how to make a budget spreadsheet") is forgotten everywhere it lives, or None."""
        result = self.memory.forget_goal(statement)
        if result["removed"]:
            _diagnostic(memory_operation="forget", memory_result="removed", memory_scope="goal")
            return PersonalOutcome(True, f"Done — I've forgotten that goal: {result['text']}. I won't bring it up again.")
        if result["candidates"]:
            listed = "; ".join(f"“{text[:80]}”" for text in result["candidates"])
            return PersonalOutcome(True, f"I have more than one goal like that: {listed}. Which one should I forget?")
        return None

    def _share_source(self, intent: PersonalIntent) -> tuple[str, str, str | None] | None:
        """(subject label, text to share, subject account or None) for the thing the user wants to share."""
        if intent.subject in {LAST, ""}:
            action = self.engine.last_action(self.email, self.chat_id, "memory")
            if not action or action.get("type") != "person":
                return None
            entry = self.memory.person(action.get("key", "")) or {}
            fact = next((item for item in entry.get("facts") or [] if item.get("id") == action.get("fact_id")), None)
            return (entry.get("name", ""), fact.get("text", "") if fact else "", self._linked_account(entry)) if entry and fact else None
        key, _ = self.memory.find_person(intent.subject)
        entry = self.memory.person(key) if key else None
        if not entry:
            return None
        text = "\n\n".join(str(item.get("text")) for item in entry.get("facts") or [] if item.get("text"))
        return (entry.get("name", intent.subject), text, self._linked_account(entry)) if text else None

    def _linked_account(self, entry: dict) -> str | None:
        linked = str(entry.get("jupiter_username") or "").strip()
        account = self.find_user(linked) if linked else None
        return str(account["email"]) if account else None

    def _memory_share(self, intent: PersonalIntent, _text: str) -> PersonalOutcome:
        source = self._share_source(intent)
        if source is None:
            subject = intent.subject if intent.subject not in {LAST, ""} else "that"
            return PersonalOutcome(True, f"I don't have anything saved about {subject} to share yet.")
        subject_label, text, subject_account = source
        target_text = intent.target
        target = None
        find_users = getattr(self, "find_users", None)
        if find_users is not None and "@" not in target_text:
            # Exact username, same rule as messages: the sharer's own account never competes ("the project creator" is both
            # accounts' username); two other accounts with that exact name -> ask instead of guessing.
            try:
                others = [m for m in (find_users(target_text) or [])
                          if str(m.get("email") or "").casefold() != self.email.casefold()]
            except Exception:
                others = []
            if len(others) > 1:
                return PersonalOutcome(True, f"More than one Jupiter account uses the name “{target_text}”, so I haven't "
                                             "shared anything. Tell me their unique, exact Jupiter username.")
            target = others[0] if others else None
        if target is None:
            target = self.find_user(target_text)
        if target is None:
            key, _ = self.memory.find_person(target_text)
            entry = self.memory.person(key) if key else None
            linked = str((entry or {}).get("jupiter_username") or "").strip()
            target = self.find_user(linked) if linked else None
        if target is None:
            return PersonalOutcome(True, f"I couldn't find a Jupiter account for {target_text}. Tell me their Jupiter "
                                         "username and I'll share it with that account.")
        recipient = str(target["email"])
        if recipient.casefold() == self.email.casefold():
            return PersonalOutcome(True, "That's your own account — it already knows what you told me.")
        # The subject is the recipient when you share what you told me about someone *with that person*.
        if subject_account is None and clean_name(subject_label) and self.find_user(subject_label) == target:
            subject_account = recipient
        if subject_account is None and target_text.casefold().split()[0] == subject_label.casefold().split()[0]:
            subject_account = recipient
        attribution = bool(intent.attribution)
        from services.initiative_engine import InitiativeEngine  # noqa: F401  (same engine instance)
        try:
            self.engine.share_fact(
                self.email, subject_label, text, "SHARE_WITH_USERS", [recipient],
                allow_source_attribution=attribution, provenance={"source_type": "explicit_user_share"},
                subject_user_id=subject_account, replace_existing=True,
            )
        except ValueError:
            _diagnostic(memory_operation="share", memory_result="refused_sensitive")
            return PersonalOutcome(True, "I can't share that one — it looks sensitive, so it stays private to you.")
        visible = [fact for fact in self.engine.shared_facts_for(recipient)
                   if fact["subject"].casefold() == subject_label.casefold()]
        _diagnostic(memory_operation="share", memory_result="shared" if visible else "failed",
                    memory_share_scope="specific_user", attribution=str(attribution).lower())
        if not visible:
            return PersonalOutcome(True, "I couldn't share that right now.")
        name = str(target.get("username") or self.display_name(recipient))
        about = "that" if intent.subject in {LAST, ""} else f"what you told me about {subject_label}"
        tail = (" I can mention that it came from you." if attribution else
                " I won't say it came from you — tell me \"you can say it's from me\" if that's okay.")
        return PersonalOutcome(True, f"Done — I'll keep {about} in mind when I talk with {name}.{tail}")

    def _memory_unshare(self, intent: PersonalIntent, _text: str) -> PersonalOutcome:
        subject = None
        if intent.subject not in {LAST, ""}:
            subject = intent.subject
        else:
            action = self.engine.last_action(self.email, self.chat_id, "memory")
            if action and action.get("type") == "person":
                subject = action.get("name")
        recipient = None
        if intent.target:
            account = self.find_user(intent.target)
            if account is None:
                key, _ = self.memory.find_person(intent.target)
                entry = self.memory.person(key) if key else None
                account = self.find_user(str((entry or {}).get("jupiter_username") or "")) if entry else None
            recipient = str(account["email"]) if account else None
            if subject is None and account:
                subject = None      # stop sharing everything with that person
        if subject is None and recipient is None:
            return PersonalOutcome(True, "Tell me what to stop sharing — for example \"stop sharing what I told you about Mira with Mira\".")
        removed = self.engine.unshare(self.email, subject_label=subject, recipient_user_id=recipient)
        _diagnostic(memory_operation="unshare", memory_result=f"removed_{removed}")
        if not removed:
            return PersonalOutcome(True, "Nothing like that was being shared.")
        return PersonalOutcome(True, "Done — I've stopped sharing that.")

    # ── reminders ───────────────────────────────────────────────────────────────────────────────────────────────
    def _birthday_request(self, text: str) -> tuple[dict | None, str, str | None]:
        """(rule, title, question) for "... before X's birthday" / "on X's birthday" using the saved birthday."""
        match = re.search(r"(?i)\b(?:(?P<n>\d+|a|an|one|two|three|four|five|six|seven)\s+(?P<unit>days?|weeks?)\s+before|"
                          r"(?P<on>on))\s+(?P<name>[\w'’ .-]{2,40}?)['’]s\s+birthday\b", text)
        if not match:
            return None, "", None
        name = clean_name(match.group("name"))
        if not name:
            return None, "", None
        key, candidates = self.memory.find_person(name)
        if key is None and len(candidates) > 1:
            return None, "", f"I know more than one {name} — which one's birthday do you mean?"
        entry = self.memory.person(key) if key else None
        birthday = (entry or {}).get("birthday")
        if not birthday:
            return None, "", f"When is {name}'s birthday? Tell me (for example \"{name}'s birthday is 14 March\") and I'll set it up."
        words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
        amount = 0 if match.group("on") else int(words.get((match.group("n") or "").casefold(), match.group("n") or 0) or 0)
        days = amount * (7 if (match.group("unit") or "").casefold().startswith("week") else 1)
        birthday_day = datetime(2000, birthday["month"], birthday["day"])      # leap base: 29 February is valid
        target = birthday_day - timedelta(days=days)
        if (target.month, target.day) == (2, 29):
            target = target.replace(day=28)       # a yearly rule on 29 February would fire only every four years
        rule = {"freq": "yearly", "month": target.month, "day": target.day, "hour": 9, "minute": 0}
        name_display = (entry or {}).get("name", name)
        if days == 0:
            title = f"It's {name_display}'s birthday today"
        elif days % 7 == 0:
            weeks = days // 7
            title = f"{name_display}'s birthday is in {'a week' if weeks == 1 else f'{weeks} weeks'} ({birthday['day']} {birthday_day.strftime('%B')})"
        else:
            title = f"{name_display}'s birthday is in {days} day{'s' if days != 1 else ''} ({birthday['day']} {birthday_day.strftime('%B')})"
        return rule, title, None

    def _confirm_created(self, event: dict, title: str) -> str:
        due = datetime.fromisoformat(str(event["due_at"]).replace("Z", "+00:00")).astimezone(self.tz)
        now = self.now()
        remaining = (due - now).total_seconds()
        when = (f"in {max(1, int(remaining + 0.999))} seconds" if 0 < remaining < 60
                else describe_when(due, now, event.get("recurrence")))
        phrase = second_person(title)
        joiner = "about" if re.match(r"(?i)^(?:the|my|your|a|an|\w+['’]s|it's)\b", title) or " birthday" in title.casefold() else "to"
        if re.match(r"(?i)^(?:it's|\w+['’]s birthday is)\b", title) or _HINGLISH_TITLE.search(title):
            return f"Done — I'll remind you {when}: {phrase}."
        return f"Done — I'll remind you {when} {joiner} {phrase}."

    # "…and call me" on a reminder: a Jupiter in-app call when it is due (marker 172), never a phone call.
    _REMINDER_CALL = re.compile(r"(?i)\b(?:call|ring)\s+me\b")
    _REMINDER_CALL_HI = re.compile(r"(?i)\b(?:mujhe\s+)?(?:call|phone|ring)\s+(?:karna|karo|kar\s*do|kar\s*dena|kariye|kijiye|"
                                   r"kar\s*ke|karke|kerke|krke)\b")
    _REMINDER_CALL_NEGATED = re.compile(r"(?i)\b(?:don'?t|do\s+not|never|no|without)\s+(?:\w+\s+){0,2}(?:call|ring)")
    _REMINDER_CALL_WORDS = re.compile(r"(?i)[\s,;]*(?:\b(?:and|also|then)\s+)?(?:please\s+)?\b(?:call|ring)\s+me"
                                      r"(?:\s+(?:for|about)\s+it)?(?:\s+in\s+(?:the\s+)?jupiter(?:\s+app)?)?\b")

    def _reminder_create(self, intent: PersonalIntent, text: str) -> PersonalOutcome:
        self._reminder_call = bool(self._REMINDER_CALL.search(text or "") or self._REMINDER_CALL_HI.search(text or "")) \
            and not self._REMINDER_CALL_NEGATED.search(text or "")
        if self._reminder_call:
            intent = replace(intent, content=self._REMINDER_CALL_WORDS.sub("", intent.content).strip(" ,;"))
            # Marker 219: "call me after 10 sec" has no reminder text of its own: the call is the reminder
            # (never a leftover like "After Just" as its title)
            leftover = re.sub(r"(?i)\b(?:in|after|just|only|about|back|at|baad|bad|mein|me|mujhe|call|phone|karna|karo|"
                              r"kar\s*do|seconds?|secs?|minutes?|mins?|hours?|hrs?|am|pm|tomorrow|tonight|morning)\b|\d+|[:.,!?]",
                              " ", intent.content)
            self._call_only = not leftover.split()                     # the call itself is the reminder
        else:
            self._call_only = False
        now = self.now()
        rule, birthday_title, question = self._birthday_request(intent.content)
        if question:
            return PersonalOutcome(True, question)
        if rule:
            due = next_occurrence(rule, now, self.tz)
            return self._create(birthday_title, due, rule, intent)
        parsed = parse_when(intent.content, now)
        title = reminder_title(intent.content, parsed)
        if getattr(self, "_call_only", False):
            title = "your Jupiter call"          # marker 219: never leftover words ("After Just", "the call me asked")
        if parsed.passed:
            tomorrow = parsed.due + timedelta(days=1)
            self.engine.remember_action(self.email, self.chat_id, "reminder_draft",
                                        {"title": title, "due": tomorrow.isoformat(), "recurrence": None, "missing": "confirm"})
            clock = parsed.due.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
            return PersonalOutcome(True, f"{clock} today has already passed. Should I remind you tomorrow at {clock} instead?")
        if not title and not parsed.due:
            self.engine.remember_action(self.email, self.chat_id, "reminder_draft", {"title": "", "due": None, "recurrence": None, "missing": "both"})
            return PersonalOutcome(True, "Sure — what should I remind you about, and when?")
        if not title:
            self.engine.remember_action(self.email, self.chat_id, "reminder_draft",
                                        {"title": "", "due": parsed.due.isoformat(), "recurrence": parsed.recurrence, "missing": "title"})
            when = describe_when(parsed.due, now, parsed.recurrence)
            return PersonalOutcome(True, f"Sure — what should I remind you about {when}?")
        if not parsed.due:
            self.engine.remember_action(self.email, self.chat_id, "reminder_draft",
                                        {"title": title, "due": None, "recurrence": None, "missing": "time"})
            return PersonalOutcome(True, f"When should I remind you to {second_person(title)}?")
        return self._create(title, parsed.due, parsed.recurrence, intent)

    def _create(self, title: str, due: datetime, recurrence: dict | None, intent: PersonalIntent | None = None) -> PersonalOutcome:
        try:
            event = self.reminders.create(self.email, title, due.astimezone(timezone.utc), self.tz_name, recurrence=recurrence,
                                          source_chat_id=self.chat_id, source_message_id=self.message_id,
                                          call=bool(getattr(self, "_reminder_call", False)))
        except ValueError as error:
            _diagnostic(reminder_operation="create", reminder_result="rejected")
            return PersonalOutcome(True, "That time has already passed — tell me a time in the future." if "passed" in str(error)
                                   else REMINDER_FAILED)
        stored = self.reminders.get(self.email, event["event_id"]) if event else None
        _diagnostic(reminder_operation="create", reminder_result="scheduled" if stored else "failed",
                    recurring=str(bool(recurrence)).lower(), timezone=self.tz_name)
        if not stored:
            return PersonalOutcome(True, REMINDER_FAILED)
        self.engine.remember_action(self.email, self.chat_id, "reminder", {"event_id": stored["event_id"]})
        self.engine.clear_action(self.email, self.chat_id, "reminder_draft")
        reply = self._confirm_created(stored, title)
        if getattr(self, "_call_only", False):
            # Marker 219: "Call me after just 10 sec" -> "Done — I'll call you in 10 seconds ..." (seconds when it is soon)
            wait = (due.astimezone(timezone.utc) - self.now().astimezone(timezone.utc)).total_seconds()
            when = (f"in {max(1, int(round(wait)))} seconds" if wait < 90 else
                    describe_when(due.astimezone(self.tz), self.now().astimezone(self.tz), recurrence))
            return PersonalOutcome(True, f"Done — I'll call you {when} with a Jupiter call in the app (not a phone "
                                         "call). It follows your call settings and quiet hours.", REMINDER_CREATE,
                                   metadata={"reminder": {"id": stored["event_id"], "due_at": stored["due_at"],
                                                          "title": title, "recurring": bool(recurrence), "call": True}})
        if getattr(self, "_reminder_call", False):
            reply += (" I'll also ring you with a Jupiter call in the app then (not a phone call). It follows your "
                      "call settings and quiet hours, and a closed app rings only when background push is set up.")
        outcome = PersonalOutcome(True, reply, REMINDER_CREATE,
                                  metadata={"reminder": {"id": stored["event_id"], "due_at": stored["due_at"],
                                                         "title": title, "recurring": bool(recurrence)}})
        if intent is not None and intent.remainder:
            outcome.continue_with = intent.remainder
        return outcome

    RETIME_WINDOW = timedelta(seconds=90)      # marker 220

    def _retime_recent_reminder(self, text: str) -> PersonalOutcome:
        """Marker 220: "change it to 12:54" / "12:54 pe kar do" within 90 s of setting a reminder moves THAT reminder
        (never a duplicate, never a failure); later, the same words are ordinary chat."""
        from services.personal_intents import REMINDER_RETIME
        match = REMINDER_RETIME.match(" ".join(str(text or "").split()))
        if not match or not self.persist:
            return PersonalOutcome()
        when = next((match.group(key) for key in ("a", "b", "c", "d") if match.group(key)), "")
        if not parse_when(when, self.now()).due:
            return PersonalOutcome()
        recent = self.engine.last_action(self.email, self.chat_id, "reminder", max_age=self.RETIME_WINDOW)
        if not recent or not recent.get("event_id"):
            return PersonalOutcome()
        outcome = self._reminder_update(PersonalIntent(REMINDER_UPDATE, content=when, reference=LAST), text)
        outcome.kind = outcome.kind or REMINDER_UPDATE
        return outcome

    def _continue_reminder_draft(self, text: str) -> PersonalOutcome:
        draft = self.engine.last_action(self.email, self.chat_id, "reminder_draft", max_age=timedelta(minutes=15))
        if not draft or not self.persist:
            return PersonalOutcome()
        value = " ".join(str(text or "").split())
        if len(value.split()) > 20 or value.endswith("?"):
            return PersonalOutcome()
        now = self.now()
        missing = draft.get("missing")
        if missing == "confirm":
            if re.match(r"(?i)^(?:yes|yeah|yep|sure|ok(?:ay)?|please|do\s+it|go\s+ahead|tomorrow)\b", value):
                due = datetime.fromisoformat(draft["due"])
                return self._create(draft.get("title") or "your reminder", due, None)
            if re.match(r"(?i)^(?:no|nope|cancel|never\s*mind|don'?t)\b", value):
                self.engine.clear_action(self.email, self.chat_id, "reminder_draft")
                return PersonalOutcome(True, "Okay — I won't set it.", REMINDER_CREATE)
            return PersonalOutcome()
        parsed = parse_when(value, now)
        if missing == "title":
            # Only an answer that reads like a reminder subject completes the draft; a new question or request
            # ("what's 2+2", "tell me a joke") is normal chat and leaves the draft to expire.
            if not re.match(r"(?i)^(?:to|about|that|for)\b", value) and (
                    len(value.split()) > 8 or re.match(
                        r"(?i)^(?:what|who|why|how|when|where|which|can|could|would|will|is|are|do|does|did|tell|"
                        r"explain|write|show|give|search|find|open|play|translate|summari[sz]e|hi|hello|hey|thanks?)\b",
                        value)):
                return PersonalOutcome()
            title = reminder_title(value, parsed) or value.strip(" .!")
            due = datetime.fromisoformat(draft["due"]) if draft.get("due") else parsed.due
            return self._create(re.sub(r"(?i)^(?:to|about)\s+", "", title), due, draft.get("recurrence")) if due else PersonalOutcome()
        if missing == "time":
            if not parsed.due:
                return PersonalOutcome()
            return self._create(draft.get("title") or value, parsed.due, parsed.recurrence)
        if missing == "both":
            title = reminder_title(value, parsed)
            if parsed.due and title:
                return self._create(title, parsed.due, parsed.recurrence)
        return PersonalOutcome()

    def _describe(self, event: dict) -> str:
        due = datetime.fromisoformat(str(event.get("snoozed_until") or event["due_at"]).replace("Z", "+00:00")).astimezone(self.tz)
        return describe_when(due, self.now(), event.get("recurrence"))

    def _reminder_list(self, _intent: PersonalIntent, _text: str) -> PersonalOutcome:
        items = self.reminders.list(self.email)
        _diagnostic(reminder_operation="list", reminder_result=f"count_{len(items)}")
        if not items:
            return PersonalOutcome(True, "You don't have any reminders set.")
        lines = [f"• {second_person(str(item.get('title') or 'Reminder'))[:1].upper()}{second_person(str(item.get('title') or 'Reminder'))[1:]} — {self._describe(item)}"
                 for item in items[:12]]
        more = f"\n…and {len(items) - 12} more." if len(items) > 12 else ""
        return PersonalOutcome(True, "Here are your reminders:\n" + "\n".join(lines) + more,
                               metadata={"reminders": {"count": len(items)}})

    def _target_reminder(self, reference: str, words: str) -> tuple[dict | None, PersonalOutcome | None]:
        if words:
            matches = self.reminders.find(self.email, words)
            if len(matches) == 1:
                return matches[0], None
            if len(matches) > 1:
                titles = [second_person(str(item.get("title"))) for item in matches[:4]]
                return None, PersonalOutcome(True, "Which one — " + "; ".join(titles) + "?")
            return None, PersonalOutcome(True, f"I couldn't find a reminder about {second_person(words)}.")
        last = self.engine.last_action(self.email, self.chat_id, "reminder")
        if last:
            event = self.reminders.get(self.email, last.get("event_id", ""))
            if event and event.get("status") in {"pending", "notified"}:
                return event, None
        items = self.reminders.list(self.email, include_notified=True)
        if len(items) == 1:
            return items[0], None
        if not items:
            return None, PersonalOutcome(True, "You don't have any reminders set.")
        titles = [second_person(str(item.get("title"))) for item in items[:4]]
        return None, PersonalOutcome(True, "Which reminder — " + "; ".join(titles) + "?")

    def _reminder_cancel(self, intent: PersonalIntent, _text: str) -> PersonalOutcome:
        if intent.reference == "all":
            items = self.reminders.list(self.email, include_notified=True)
            # Count what the user sees as active; already-delivered leftovers are closed silently.
            cancelled = sum(1 for item in items
                            if self.reminders.cancel(self.email, item["event_id"]) and item.get("status") == "pending")
            _diagnostic(reminder_operation="cancel_all", reminder_result=f"cancelled_{cancelled}")
            return PersonalOutcome(True, f"Cancelled {cancelled} reminder{'s' if cancelled != 1 else ''}." if cancelled else
                                         "You don't have any reminders set.")
        event, question = self._target_reminder(intent.reference, intent.content)
        if question:
            return question
        ok = self.reminders.cancel(self.email, event["event_id"])
        _diagnostic(reminder_operation="cancel", reminder_result="cancelled" if ok else "failed")
        if not ok:
            return PersonalOutcome(True, "I couldn't cancel that reminder right now.")
        self.engine.clear_action(self.email, self.chat_id, "reminder")
        return PersonalOutcome(True, f"Cancelled — I won't remind you to {second_person(str(event.get('title')))}."
                               if not re.match(r"(?i)^\w+['’]s\s+birthday", str(event.get("title"))) else "Cancelled.")

    def _reminder_update(self, intent: PersonalIntent, _text: str) -> PersonalOutcome:
        event, question = self._target_reminder(intent.reference, intent.subject)
        if question:
            return question
        current = datetime.fromisoformat(str(event["due_at"]).replace("Z", "+00:00")).astimezone(self.tz)
        parsed = parse_when(intent.content, self.now())
        if not parsed.due:
            return PersonalOutcome(True, "What time should I move it to?")
        due = parsed.due
        if not parsed.has_date and parsed.has_time:
            due = current.replace(hour=parsed.due.hour, minute=parsed.due.minute)
            if due <= self.now():
                due += timedelta(days=1)
        recurrence = event.get("recurrence")
        if recurrence:
            recurrence = {**recurrence, "hour": due.hour, "minute": due.minute}
            due = next_occurrence(recurrence, self.now() - timedelta(seconds=1), self.tz)
        updated = self.reminders.reschedule(self.email, event["event_id"], due.astimezone(timezone.utc),
                                            recurrence=recurrence if recurrence else False)
        _diagnostic(reminder_operation="update", reminder_result="moved" if updated else "failed")
        if not updated:
            return PersonalOutcome(True, "I couldn't move that reminder right now.")
        self.engine.remember_action(self.email, self.chat_id, "reminder", {"event_id": updated["event_id"]})
        return PersonalOutcome(True, f"Done — I'll remind you {self._describe(updated)} instead.")

    def _reminder_snooze(self, intent: PersonalIntent, text: str) -> PersonalOutcome:
        amount = re.search(r"(?i)(\d+|an?|half\s+an|one|two|three|five|ten|fifteen|twenty|thirty)\s*(minutes?|mins?|hours?|hrs?)", text)
        minutes = 10
        if amount:
            words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20,
                     "thirty": 30, "half an": 0.5}
            value = float(amount.group(1)) if amount.group(1).isdigit() else words.get(amount.group(1).casefold(), 10)
            minutes = int(value * 60) if amount.group(2).casefold().startswith(("hour", "hr")) else int(value)
        event, question = self._target_reminder(LAST, "")
        if question:
            return question
        updated = self.reminders.snooze(self.email, event["event_id"], max(1, minutes))
        _diagnostic(reminder_operation="snooze", reminder_result="snoozed" if updated else "failed")
        if not updated:
            return PersonalOutcome(True, "I couldn't snooze that reminder right now.")
        until = datetime.fromisoformat(str(updated.get("snoozed_until")).replace("Z", "+00:00")).astimezone(self.tz)
        clock = until.strftime("%I:%M %p").lstrip("0")
        return PersonalOutcome(True, f"Snoozed — I'll remind you again at {clock}.")
