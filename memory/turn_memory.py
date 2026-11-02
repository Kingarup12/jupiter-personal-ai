"""One memory step per user turn: extract -> validate -> save -> an honest receipt (marker 221).

Real 26 Oct (owner): "a friend she my collage friend in a local college" / "She is also my Batch
mate CSE AI-ML" / "Her Hostal is P3 and mine is P5" / "Her Birthday 8 Jan 2007" / "Btw mine is 12 Feb 2008" /
"a friend ka P4 hostel hai my bad, P3 nhi" / "my mother name is a family member and my Father name is a family member" /
"only use Jii in Baishali name". Jupiter answered "saved / noted / locked in memory" every time while the regex
classifier saved nothing for a friend, made the birthday a life event of the USER, glued both parents into one junk row
and later created a person named "vo CSE AIML mai". A new chat then said "you never told me" about her batch.

This module replaces that regex fact-slicing for ordinary statements:
- a cheap gate decides whether a turn can carry a durable fact at all (questions, commands and small talk never do);
- ONE model call reads the message with the last few user turns, the People I Know roster and the existing facts, and
  returns atomic JSON operations (pronouns "she / her / uska / vo / mine" resolved, "mother X and father Y" split,
  "P4, not P3" as an update);
- a deterministic validator keeps only operations grounded in the user's own words (the evidence must be a substring
  of this message), with a real person's name, a sane value and the right cardinality (hostel / birthday / branch
  replace the old value, which is kept as superseded; friends / hobbies add);
- writes go to the existing stores only (People I Know in profile.json, the user's own explicit notes in
  memories.db), and the result is a receipt (SAVED / UPDATED / ASKED / NONE) the reply must follow: Jupiter never
  says "saved" unless the receipt says so.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field

from core.logger import Logger

SAVED, UPDATED, ASKED, NONE = "SAVED", "UPDATED", "ASKED", "NONE"

# hostel / birthday / branch...: a person has one current value; a newer one supersedes the old (kept, marked superseded)
SINGLE_VALUED = frozenset({
    "birthday", "hostel", "room", "branch", "department", "course", "college", "school", "university", "batch", "year",
    "class", "section", "city", "hometown", "lives_in", "address", "job", "employer", "company", "profession", "age",
    "full_name", "surname", "nickname", "relationship", "favourite_colour", "favorite_color", "favourite_food",
    "favourite_song", "phone_model", "blood_group", "height", "native_language", "mother_name", "father_name",
})
# what the user calls each predicate in a stored sentence ("a friend's hostel: P4")
_LABELS = {"lives_in": "city", "favourite_colour": "favourite colour", "favorite_color": "favourite colour",
           "favourite_food": "favourite food", "favourite_song": "favourite song", "mother_name": "mother's name",
           "father_name": "father's name", "full_name": "full name", "blood_group": "blood group",
           "native_language": "native language", "phone_model": "phone", "jupiter_account": "Jupiter account"}
# never a person's name (real debris in the owner's store: "With", "Doing Right Now", "After Just", "vo CSE AIML mai")
_NOT_NAMES = frozenset("""
what who whom whose which when where why how with without doing done talking watching music after before just only
kaise kya kaun kab kahan kyun mera meri mere mujhe main hum uska uski uske unka unki unke vo woh wo ye yeh is us
branch batch hostel college school class perte padhti padhta hai hain tha thi nhi nahi nahin haan right now today
tomorrow call me okay ok yes no hello hi bro bhai boss jupiter user volume heart latest mail question sister brother
mother father mom dad friend girlfriend boyfriend wife husband she he her his him they them their my mine i
""".split())
_TRIVIAL = re.compile(r"(?i)^(?:ok(?:ay)?|okk+|haan|ha|han|hmm+|achha|acha|accha|thik\s+hai|theek\s+hai|thanks?(?:\s+you)?|"
                      r"thank\s+you|ty|yes|no|nope|yeah|yep|cool|nice|great|good|wow|lol|haha+|done|sure|bye)[\s.!?]*$")
# wh-questions and "tell me..." only: "Are mere branch mai..." is Hinglish "arre", not an English question
_QUESTION_START = re.compile(
    r"(?i)^(?:what|who|whom|whose|which|when|where|why|how|can\s+you|could\s+you|would\s+you|do\s+you|did\s+you|"
    r"kya|kaun|kaunsa|kaunsi|kab|kahan|kaha|kyun|kyu|kaise|kitna|kitni|kitne|tell\s+me|bata(?:o|na)?|batao)\b")
_DURABLE_CUE = re.compile(
    r"(?i)\b(?:my|mine|myself|i\s+am|i'?m|i\s+have|i\s+live|i\s+study|i\s+work|i\s+was\s+born|her|hers|his|she|he|their|"
    r"mera|meri|mere|mujhe|main|apna|apni|uska|uski|uske|unka|unki|unke|iska|iski|vo|woh|wo|"
    r"name|naam|birthday|bday|born|janamdin|hostel|hostal|room|branch|batch|college|collage|school|class|department|"
    r"friend|dost|sister|didi|behen|bahan|brother|bhai|bhaiya|mother|mummy|maa|mom|father|papa|dad|wife|husband|"
    r"girlfriend|boyfriend|cousin|uncle|aunt|chacha|mama|nana|nani|dada|dadi|teacher|lives?|rehti|rehta|works?|job|"
    r"favou?rite|pasand|allergic|prefer|use\s+\w+\s+(?:with|in|for)|ji+)\b|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|"
    r"aug|sep|oct|nov|dec)[a-z]*\b")
_DICTATION = re.compile(r"(?i)\b(?:update\s+(?:your\s+)?memory|yaad\s+rakh|remember\s+(?:this|these|that)|note\s+(?:this|kar)|"
                        r"i\s+will\s+(?:say|tell)|main\s+bata(?:ta|ti|unga|ungi))")
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)

PROMPT = """You extract durable personal facts for Jupiter, a personal assistant, from the USER's latest message.
Languages: English, Hinglish (Hindi in Latin letters), Hindi, Bengali, often mixed and with typos.

Input JSON has: "message" (the latest user message), "recent_user_turns" (the user's previous messages, oldest first),
"people" (people already saved: name, relationship, current facts), "self" (facts already saved about the user).

Return ONLY a JSON object: {"operations": [...]} with zero or more of:
- {"op":"person","name":"<full name>","relationship":"<user's words, e.g. college friend and batch mate>","evidence":"<exact words from message>","confidence":0-1}
- {"op":"person_fact","name":"<full name of an existing or just-introduced person>","predicate":"<snake_case>","value":"<value>","old_value":"<value it replaces or null>","evidence":"<exact words from message>","confidence":0-1}
- {"op":"self_fact","predicate":"<snake_case>","value":"<value>","old_value":null,"evidence":"<exact words>","confidence":0-1}
- {"op":"preference","text":"<the standing rule in plain English, e.g. Use 'Jii' only with Baishali's name>","evidence":"<exact words>","confidence":0-1}
- {"op":"ambiguous","name":"<the name as said>","candidates":["<full name>", "..."],"evidence":"<exact words>"}

Rules:
1. Only facts the user STATES as true about themself or someone in their life. Questions, requests, commands, plans
   for today, moods, hypotheticals, jokes and things happening "right now" are NOT facts: return {"operations": []}.
2. Resolve pronouns and omitted subjects from recent_user_turns: she/her/he/his/uska/uski/vo/woh = the person the user
   was just talking about; mine/mera/meri = the user ("Her hostel is P3 and mine is P5" -> person_fact hostel P3 AND
   self_fact hostel P5). "Btw mine is 12 Feb 2008" right after a birthday = the user's birthday.
3. Split combined statements into separate operations ("my mother name is X and my father name is Y" -> two "person"
   ops, relationship mother and father).
4. A correction ("P4 hai, P3 nahi", "my bad, it's P4", "not P3") is a person_fact/self_fact with old_value set.
5. Use the person's FULL name when it is known from "people" or the recent turns. If a first name matches more than one
   saved person and nothing in the message or recent turns tells which one, return "ambiguous" instead of guessing.
   Two people with different surnames or relationships are different people, even with the same first name.
6. Names are names of people only. Never a verb, pronoun, place, question word or phrase ("With", "Doing Right Now",
   "After Just", "vo CSE AIML mai" are not names).
7. Use predicates like: birthday (value as said, e.g. "8 Jan 2007"), hostel, branch, college, school, batch, year,
   city, job, employer, favourite_colour, nickname, surname, mother_name, father_name. Keep values short.
8. "evidence" must be copied exactly from "message" (a substring), never from older turns.
9. A rule about how Jupiter should talk or address someone is a "preference".
"""


@dataclass
class Receipt:
    kind: str = NONE
    lines: list[str] = field(default_factory=list)          # what was saved / updated, in plain words
    question: str = ""
    considered: bool = False                               # the gate let this turn through
    pending: bool = False                                  # still saving when the reply had to be written

    @property
    def saved(self) -> bool:
        return self.kind in {SAVED, UPDATED}

    def note(self) -> str:
        """System note for the reply model (reference data; never user speech)."""
        if self.saved:
            return ("MEMORY RECEIPT (Jupiter's memory system already did this; reference data): " + "; ".join(self.lines)
                    + ". Confirm this briefly and naturally in the user's language. Do not claim anything else was "
                      "saved, and do not mention this note.")
        if self.kind == ASKED:
            return ("MEMORY RECEIPT: nothing was saved yet. Ask the user exactly this, briefly, in their language: "
                    + self.question + " Do not say anything was saved.")
        if self.pending:
            return ("MEMORY RECEIPT: Jupiter's memory system is still processing this message. Do not say it is saved "
                    "or noted; just reply to what the user said.")
        return ("MEMORY RECEIPT: nothing was saved to memory in this turn. Never say you saved, noted, stored, locked in "
                "or will remember anything (no \"yaad rakh liya\", \"note kar liya\", \"save ho gaya\"). If the user "
                "wanted something remembered, say plainly that you haven't saved it and offer to.")


# ── gate ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def looks_like_memory_turn(text: str, recent_user_turns: list[str] | None = None,
                           last_assistant: str = "", known_names: list[str] | None = None) -> bool:
    """Cheap, deterministic: can this turn carry a durable fact? (Most turns cannot, so most turns cost nothing.)"""
    value = " ".join(str(text or "").split())
    if not value or len(value) > 1500 or _TRIVIAL.match(value):
        return False
    words = value.split()
    if len(words) < 3:
        return False
    if value.rstrip().endswith("?") or _QUESTION_START.match(value):
        return False
    try:
        from services.desktop_intents import looks_like_device_request
        if looks_like_device_request(value):
            return False
    except Exception:
        pass
    if _DICTATION.search(" ".join([last_assistant or "", *(recent_user_turns or [])[-2:]])):
        return True                                        # the user said "update your memory, I'll tell you..."
    if _DURABLE_CUE.search(value):
        return True
    if known_names and any(re.search(rf"(?i)\b{re.escape(name.split()[0])}\b", value) for name in known_names if name):
        return True                                        # "a friend loves badminton": a saved person
    # a capitalised name that is not the first word ("This is a friend", "Riya lives in Pune")
    return any(word[:1].isupper() and word.strip(".,!?").isalpha() and len(word) > 2 for word in words[1:])


# ── validation ───────────────────────────────────────────────────────────────────────────────────────────────────
def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(re.sub(r"[^\w\s:/.-]", " ", value).split())


def valid_name(name: str, message: str, recent_user_turns: list[str], known: set[str]) -> bool:
    value = " ".join(str(name or "").split())
    if not value or len(value) > 45 or re.search(r"[.?!,;:]|\d", value):
        return False
    words = value.split()
    if not 1 <= len(words) <= 4 or any(word.casefold() in _NOT_NAMES for word in words):
        return False
    from memory.profile import plausible_person_name
    if not plausible_person_name(value):
        return False
    if value.casefold() in known:
        return True
    # the (first) name must really have been said: in this message or the user's last few turns, never invented
    haystack = _fold(" ".join([message, *recent_user_turns[-4:]]))
    return all(re.search(rf"\b{re.escape(_fold(word))}\b", haystack) for word in words[:1]) and \
        sum(1 for word in words if re.search(rf"\b{re.escape(_fold(word))}\b", haystack)) >= max(1, len(words) - 1)


def _clean_predicate(value: str) -> str:
    predicate = re.sub(r"[^a-z0-9_]", "", re.sub(r"[\s-]+", "_", str(value or "").strip().casefold()))[:40]
    return {"hostal": "hostel", "bday": "birthday", "date_of_birth": "birthday", "dob": "birthday",
            "favorite_colour": "favourite_colour", "favourite_color": "favourite_colour", "favorite_color": "favourite_colour",
            "lives": "city", "home_town": "hometown"}.get(predicate, predicate)


def validate(operations: list, message: str, recent_user_turns: list[str], known_people: list[str]) -> list[dict]:
    """Keep only operations grounded in the user's own words with sane names, predicates and values."""
    folded_message = _fold(message)
    known = {" ".join(str(name).split()).casefold() for name in known_people if name}
    introduced = {" ".join(str(raw.get("name") or "").split()).casefold() for raw in operations or []
                  if isinstance(raw, dict) and raw.get("op") == "person" and str(raw.get("relationship") or "").strip()}
    kept: list[dict] = []
    for raw in operations if isinstance(operations, list) else []:
        if not isinstance(raw, dict):
            continue
        op = str(raw.get("op") or "").strip()
        evidence = _fold(raw.get("evidence"))
        if not evidence or evidence not in folded_message:
            continue                                        # not in this message: never saved
        try:
            confidence = float(raw.get("confidence", 0.0 if op != "ambiguous" else 1.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if op != "ambiguous" and confidence < 0.7:
            continue
        name = " ".join(str(raw.get("name") or "").split())
        if op in {"person", "person_fact", "ambiguous"} and op != "ambiguous" and not valid_name(
                name, message, recent_user_turns, known):
            continue
        if op == "person":
            relationship = " ".join(str(raw.get("relationship") or "").split())[:80]
            if not relationship or re.search(r"(?i)\bis\s+the\s+user'?s\b", relationship):
                continue
            kept.append({"op": op, "name": name, "relationship": relationship})
        elif op in {"person_fact", "self_fact"}:
            predicate = _clean_predicate(raw.get("predicate"))
            value = " ".join(str(raw.get("value") or "").split()).strip(" .,!")[:120]
            old_value = " ".join(str(raw.get("old_value") or "").split()).strip(" .,!")[:120] or None
            if not predicate or not value or len(value.split()) > 12 or value.casefold() in _NOT_NAMES:
                continue
            if op == "person_fact":
                name_words = {word.casefold() for word in name.split()}
                if name.casefold() not in known and name.casefold() not in introduced:
                    continue                            # facts only about someone saved or introduced in this turn
                if value.casefold() in name_words or set(value.casefold().split()) <= name_words:
                    continue                            # "Oskar Kunz's hostel: Kunz": the value is the name itself
            item = {"op": op, "predicate": predicate, "value": value, "old_value": old_value,
                    "single": predicate in SINGLE_VALUED}
            if op == "person_fact":
                item["name"] = name
            kept.append(item)
        elif op == "preference":
            text = " ".join(str(raw.get("text") or "").split())[:300]
            if len(text.split()) >= 3:
                kept.append({"op": op, "text": text})
        elif op == "ambiguous":
            candidates = [" ".join(str(item).split()) for item in raw.get("candidates") or [] if str(item).strip()]
            candidates = [item for item in candidates if item.casefold() in known][:4]
            if name and len(candidates) >= 2:
                kept.append({"op": op, "name": name, "candidates": candidates})
    return kept


def guard_shared_first_names(operations: list[dict], message: str, recent_user_turns: list[str], email: str) -> list[dict]:
    """Marker 221 (auto-test WT 28 Oct: "a friend loves badminton" was given to a friend while a sister a friend
    was saved): a fact about a bare first name that two saved people share is saved only when the message says the
    surname, the user's previous message named that person in full, or a correction's old value ("P3 nahi") belongs to
    exactly one of them. Otherwise Jupiter asks which one."""
    try:
        from memory.profile import UserProfileStore
        saved = UserProfileStore(user_id=email).list_people()
    except Exception:
        return operations
    folded_message = _fold(message)
    previous = _fold(recent_user_turns[-1]) if recent_user_turns else ""
    result, asked = [], set()
    for item in operations:
        if item.get("op") != "person_fact":
            result.append(item)
            continue
        name = item["name"]
        first = name.split()[0].casefold()
        twins = [person for person in saved if str(person.get("name") or "").split()[:1]
                 and str(person.get("name")).split()[0].casefold() == first]
        if len(twins) < 2 or name.casefold() not in {str(person.get("name")).casefold() for person in twins}:
            result.append(item)
            continue
        surname_said = any(re.search(rf"\b{re.escape(_fold(word))}\b", folded_message) for word in name.split()[1:])
        named_before = bool(previous) and _fold(name) in previous
        old = str(item.get("old_value") or "").casefold()
        owners = [person for person in twins if old and any(
            fact.get("predicate") == item["predicate"] and str(fact.get("value") or "").casefold() == old
            for fact in person.get("facts") or [])]
        if surname_said or named_before or (len(owners) == 1 and str(owners[0].get("name")).casefold() == name.casefold()):
            result.append(item)
        elif first not in asked:
            asked.add(first)
            result.append({"op": "ambiguous", "name": name.split()[0],
                           "candidates": [str(person.get("name")) for person in twins][:4]})
    return result


# ── extraction ───────────────────────────────────────────────────────────────────────────────────────────────────
def _roster(email: str) -> tuple[list[dict], list[str]]:
    from memory.profile import UserProfileStore, plausible_person_name
    people = []
    for person in UserProfileStore(user_id=email).list_people():
        name = str(person.get("name") or "")
        if not plausible_person_name(name):
            continue
        facts = [str(fact.get("text") or "")[:120] for fact in person.get("facts") or []][-6:]
        people.append({"name": name, "relationship": person.get("relationship_label") or person.get("relationship") or "",
                       "facts": facts})
    return people[:40], [item["name"] for item in people]


def _self_facts(email: str) -> list[str]:
    try:
        from services.personal_memory import PersonalMemory
        return [note["text"][:160] for note in PersonalMemory(email).self_notes()][-12:]
    except Exception:
        return []


def extract(message: str, recent_user_turns: list[str], people: list[dict], self_facts: list[str],
            generate=None) -> list:
    payload = {"message": message, "recent_user_turns": recent_user_turns[-4:], "people": people, "self": self_facts}
    if generate is None:
        from core.llm import LLM
        from core.model_manager import ModelManager, Task

        def generate(system_prompt, user_prompt):
            return LLM.generate(model=ModelManager().get_best_models(Task.MEMORY_ANALYZER), system_prompt=system_prompt,
                                user_prompt=user_prompt)
    raw = generate(PROMPT, json.dumps(payload, ensure_ascii=False))
    text = str(raw or "")
    match = _JSON_BLOCK.search(text)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return []
    operations = parsed.get("operations") if isinstance(parsed, dict) else None
    return operations if isinstance(operations, list) else []


def identifier_operations(message: str, people: list[dict]) -> list[dict]:
    """Marker 223: "my owner acc maintainer@example.invalid" / "my girlfriend's username is friend_handle" is saved without a
    model call (real 28 Oct: it was said while Jupiter waited for a call target and nothing was saved). A label that is
    exactly one saved person's relationship ("girlfriend" -> a friend) is that person's Jupiter account; anything else
    ("owner account") is the user's own fact."""
    from services.personal_intents import identifier_statement
    parsed = identifier_statement(message)
    if not parsed:
        return []
    label, identifier = parsed
    owners = [person for person in people
              if re.search(rf"\b{re.escape(label)}\b", str(person.get("relationship") or "").casefold())
              or str(person.get("name") or "").casefold() == label]
    if len(owners) == 1:
        return [{"op": "person_fact", "name": owners[0]["name"], "predicate": "jupiter_account", "value": identifier,
                 "old_value": None, "single": True}]
    predicate = re.sub(r"[^a-z0-9]+", "_", label).strip("_")[:40]
    return [{"op": "self_fact", "predicate": predicate, "value": identifier, "old_value": None, "single": True}] \
        if predicate else []


# ── saving ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _label(predicate: str) -> str:
    return _LABELS.get(predicate, predicate.replace("_", " "))


def apply(email: str, operations: list[dict], *, chat_id: str | None = None, message_id: str | None = None) -> Receipt:
    from services.personal_memory import PersonalMemory
    memory = PersonalMemory(email)
    receipt = Receipt(considered=True)
    updated = False
    for item in operations:
        try:
            if item["op"] == "ambiguous":
                receipt.kind = ASKED
                receipt.question = (f"Which {item['name']} do you mean — " + " or ".join(item["candidates"]) + "?")
                continue
            if item["op"] == "person":
                result = memory.remember_about_person(item["name"], f"{item['name']} is my {item['relationship']}",
                                                      chat_id=chat_id, message_id=message_id,
                                                      relationship_label=item["relationship"])
                if result.get("status") == "ambiguous":
                    receipt.kind, receipt.question = ASKED, ("Which " + item["name"] + " do you mean — "
                                                            + " or ".join(result.get("candidates") or []) + "?")
                    continue
                if result.get("status") in {"saved", "updated", "replaced"}:
                    receipt.lines.append(f"{result.get('name') or item['name']} ({item['relationship']})")
                    updated |= result.get("status") != "saved" and not result.get("created_person")
            elif item["op"] == "person_fact":
                predicate, value = item["predicate"], item["value"]
                sentence = (f"{item['name']}'s birthday is {value}" if predicate == "birthday"
                            else f"{item['name']}'s {_label(predicate)}: {value}")
                result = memory.remember_about_person(item["name"], sentence, chat_id=chat_id, message_id=message_id,
                                                      predicate=predicate, value=value, single=item["single"])
                if result.get("status") == "ambiguous":
                    receipt.kind, receipt.question = ASKED, ("Which " + item["name"] + " do you mean — "
                                                            + " or ".join(result.get("candidates") or []) + "?")
                    continue
                if result.get("status") in {"saved", "updated", "replaced", "reinforced"}:
                    previous = result.get("superseded_value") or item.get("old_value")
                    if previous and previous.casefold() != value.casefold():
                        receipt.lines.append(f"{result.get('name') or item['name']}'s {_label(predicate)} changed from "
                                             f"{previous} to {value}")
                        updated = True
                    else:
                        receipt.lines.append(f"{result.get('name') or item['name']}'s {_label(predicate)}: {value}")
            elif item["op"] == "self_fact":
                predicate, value = item["predicate"], item["value"]
                result = memory.remember_about_self(f"My {_label(predicate)} is {value}", chat_id=chat_id,
                                                    message_id=message_id, defer_index=True)
                if result.get("status") in {"saved", "updated", "reinforced"}:
                    receipt.lines.append(f"your {_label(predicate)}: {value}")
                    updated |= result.get("status") == "updated"
            elif item["op"] == "preference":
                result = memory.remember_about_self(item["text"], chat_id=chat_id, message_id=message_id,
                                                    defer_index=True)
                if result.get("status") in {"saved", "updated", "reinforced"}:
                    receipt.lines.append(f"rule: {item['text']}")
        except ValueError:
            continue
        except Exception as error:
            Logger.warning(f"turn memory apply failed error_class={type(error).__name__}")
    if receipt.lines:
        receipt.kind = UPDATED if updated else SAVED
    return receipt


def process_turn(email: str, message: str, recent_user_turns: list[str] | None = None, *, last_assistant: str = "",
                 chat_id: str | None = None, message_id: str | None = None, generate=None) -> Receipt:
    """Gate -> extract -> validate -> save. Never raises; a failure is an honest NONE receipt."""
    recent = [str(item or "") for item in (recent_user_turns or []) if str(item or "").strip()]
    if not email:
        return Receipt()
    try:
        people, names = _roster(email)
    except Exception:
        people, names = [], []
    if not looks_like_memory_turn(message, recent, last_assistant, names):
        return Receipt()
    try:
        identifiers = identifier_operations(message, people)
        if identifiers:                                    # the whole message is that statement: no model needed
            return apply(email, identifiers, chat_id=chat_id, message_id=message_id)
        operations = validate(extract(message, recent, people, _self_facts(email), generate=generate),
                              message, recent, names)
        operations = guard_shared_first_names(operations, message, recent, email)
        if not operations:
            return Receipt(considered=True)
        return apply(email, operations, chat_id=chat_id, message_id=message_id)
    except Exception as error:
        Logger.warning(f"turn memory skipped error_class={type(error).__name__}")
        return Receipt(considered=True)


# ── honest replies ───────────────────────────────────────────────────────────────────────────────────────────────
_MEMORY_CLAIM = re.compile(
    r"(?i)\b(?:i'?ve|i\s+have|i'?ll|i\s+will|i'?m\s+going\s+to)\s+(?:also\s+)?(?:saved|save|noted|note|stored|store|"
    r"remembered|remember|added|updated|locked|tucked|kept|keep)\b(?!\s+(?:it\s+)?in\s+mind\s+(?:if|when))"
    r"|\b(?:saved|noted|stored|locked|tucked\s+away|added)\s+(?:it\s+|that\s+|this\s+|them\s+)?(?:to|in|into)\s+"
    r"(?:my\s+)?(?:memory|notes|archive)\b|\bmemory\s+(?:updated|mein\s+(?:save|lock|add|daal)\w*)\b"
    r"|\b(?:got\s+it|noted)[,!.]?\s+(?:i'?ve|i\s+have)\s+(?:saved|noted|stored)\b"
    r"|\byaad\s+rakh\s*(?:liya|li|lungi|lunga|loongi|loonga|ungi|unga|lenge)\b|\byaad\s+rakhungi\b|\byaad\s+rakhunga\b"
    r"|\bnote\s+kar\s*(?:liya|li|lungi|lunga|diya|di)\b|\bsave\s+(?:ho\s+gay[ae]|kar\s*(?:liya|li|diya|di|lungi|lunga))\b"
    r"|\b(?:notes?|memory)\s+update\s+(?:ho\s+gay[ae]|kar\s*(?:liya|li|diya|di))\b|\block\s+kar\s*(?:di|diya|li|liya)\b"
    r"|याद\s+रख\s*(?:लिया|ली|लूंगी|लूँगी|लूंगा|लूँगा)|सेव\s+कर\s*(?:लिया|ली|दिया)"
)
_MEMORY_HEDGE = re.compile(r"(?i)\b(?:can'?t|cannot|couldn'?t|haven'?t|didn'?t|won'?t|not\s+saved|nahi|nahin|nhi|"
                           r"should\s+i|shall\s+i|do\s+you\s+want|want\s+me\s+to|kya\s+main|if\s+you|agar|once\s+you|"
                           r"when\s+you\s+tell)\b|नहीं")     # "If you tell me, I'll remember it" is an offer


def false_memory_claim(reply: str) -> bool:
    for sentence in re.split(r"(?<=[.!?।])\s+", " ".join(str(reply or "").split())):
        if _MEMORY_CLAIM.search(sentence) and not _MEMORY_HEDGE.search(sentence) and not sentence.rstrip().endswith("?"):
            return True
    return False


def honest_memory_reply(reply: str, user_input: str, receipt: Receipt | None) -> str:
    """Drop the sentences that claim a save the memory system did not make; say what is true instead."""
    if receipt is not None and receipt.saved:
        return reply
    sentences = re.split(r"(?<=[.!?।])\s+", " ".join(str(reply or "").split()))
    kept = [sentence for sentence in sentences
            if not (_MEMORY_CLAIM.search(sentence) and not _MEMORY_HEDGE.search(sentence))]
    from services.delivery_claims import _language
    language = _language(user_input)
    if receipt is not None and receipt.kind == ASKED and receipt.question:
        truth = receipt.question
    elif receipt is not None and receipt.pending:
        truth = {"hi-Latn": "Main ise abhi save kar rahi hoon.", "hi": "मैं इसे अभी सेव कर रही हूँ।"}.get(
            language, "I'm still saving that.")
    else:
        truth = {"hi-Latn": "Maine ise abhi memory mein save nahi kiya hai — chaho toh main yaad rakh sakti hoon.",
                 "hi": "मैंने इसे अभी मेमोरी में सेव नहीं किया है — आप चाहें तो मैं याद रख सकती हूँ।"}.get(
            language, "I haven't saved that to memory yet — I can remember it if you'd like.")
    body = " ".join(kept).strip()
    return f"{body} {truth}".strip() if body else truth
