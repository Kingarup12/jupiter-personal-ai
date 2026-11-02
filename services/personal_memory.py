"""Explicit personal memory, owned by the application (never by a model's decision to save).

People the user tells Jupiter about live in the canonical *People I Know* store (profile.json -> social_graph), now
with structured, user-provided facts: `facts` [{id, text, kind, created_at, updated_at, chat_id, message_id}],
`relationship_label` (the user's own words, e.g. "girlfriend and best friend"), `relationship` (the existing enum,
only when stated) and `birthday` {month, day} (only when stated). Notes about the user themself are explicit
records in the user's own memory database. Everything here is scoped to one account's files; nothing reads across
accounts except the explicit sharing table (services.initiative_engine.shared_people_facts).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from difflib import SequenceMatcher

from memory.profile import UserProfileStore

CONTEXT_LIMIT = 6000        # one pasted context block about a person
FACTS_PER_PERSON = 40
# Standing preferences apply to every answer; every other explicit note is used only when relevant to the message.
_STANDING_PREFERENCE = re.compile(
    r"(?i)\b(?:ji+|honorific|address(?:es|ing)?|refer\s+to|use\s+'?\w+'?\s+(?:only\s+)?(?:with|in|for)|"   # marker 221: "use Jii only with Baishali"
    r"answers?|responses?|replies|explanations?|tone|call\s+me|address\s+me|my\s+pronouns|pronouns|"
    r"units?|metric|imperial|celsius|fahrenheit|bullet\s+points?|emojis?|concise|brief|short\s+(?:answers?|replies)|"
    r"detailed|formal|casual|simple\s+words|in\s+(?:hindi|bengali|english|hinglish))\b"
)

_RELATIONSHIPS = [
    (r"(?:project|lab|study|business|work|research|team|coding)\s+partner|co-?workers?|colleagues?|teammates?|"
     r"work\s+friend|office\s+friend", "colleague"),
    (r"classmates?|batchmates?|schoolmates?|roommates?|room\s*mates?", "classmate"),
    (r"girlfriend|boyfriend|wife|husband|fianc[eé]e?|spouse|life\s+partner|partner|lover|crush", "partner"),
    (r"best\s*friends?|bestie|close\s+friends?|closest\s+friends?|friends?|buddy|duo|bff", "friend"),
    (r"sisters?|brothers?|siblings?|twin", "sibling"),
    (r"mom|mother|mum|mummy|maa|dad|father|papa|parents?|cousins?|aunt|aunty|auntie|uncle|grand(?:ma|pa|mother|father)|"
     r"sons?|daughters?|nieces?|nephews?|family", "family"),
    (r"mentors?|teachers?|professors?|coach|guide|tutor|guru", "mentor"),
    (r"neighbou?rs?", "neighbor"),
]
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")
_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fold(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(text or "").casefold()).split())


def similar(a: str, b: str) -> float:
    left, right = _fold(a), _fold(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _month(token: str) -> int:
    return next(index for index, name in enumerate(_MONTHS) if name.startswith(token.casefold()[:3])) + 1


def parse_birthday(text: str, name: str = "") -> dict | None:
    """{month, day} from "<name>'s birthday is 21 May" / "her birthday is on May 21" (explicit statements only)."""
    subject = rf"(?:{re.escape(name)}['’]?s?|{re.escape(name.split()[0])}['’]s|her|his|their|my)" if name else r"(?:her|his|their|my|\w+['’]s)"
    match = re.search(rf"(?i)\b{subject}\s+(?:birthday|b-?day|date\s+of\s+birth)\s+(?:is|falls\s+on|was)\s+(?:on\s+)?"
                      rf"(?:the\s+)?(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_RE}|{_MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?)",
                      str(text or ""))
    if not match:
        return None
    day = int(match.group(1) or match.group(4))
    month = _month(match.group(2) or match.group(3))
    try:
        datetime(2000, month, day)           # a leap year, so 29 February is a real birthday; 31 February is not
    except ValueError:
        return None
    return {"month": month, "day": day}


def relationship_from(text: str, name: str) -> tuple[str, str]:
    """(enum, the user's own label) from an explicit "<name>/she/he is my X" statement, else ("", "")."""
    first = re.escape(name.split()[0]) if name else ""
    subject = rf"(?:{re.escape(name)}|{first}|she|he|they)" if name else r"(?:she|he|they)"
    match = re.search(rf"(?i)\b{subject}\s+(?:is|was|has\s+been|became|is\s+now|'s)\s+(?:also\s+)?my\s+"
                      rf"([^.;:!?\n]{{2,80}})", str(text or ""))
    if not match:
        lead = re.search(rf"(?i)\bmy\s+((?:best\s+|close\s+|closest\s+|older\s+|younger\s+|little\s+|big\s+)?[\w-]+(?:\s+[\w-]+)?)\s+{re.escape(name)}\b",
                         str(text or "")) if name else None
        if not lead:
            return "", ""
        label = lead.group(1)
    else:
        label = match.group(1)
    label = re.split(r"(?i)\s+(?:and\s+(?:we|i|she|he)|but|because|since|who|which|from\s+\w+\s+days)\b", label)[0]
    label = " ".join(label.split()).strip(" ,")[:60]
    for pattern, enum in _RELATIONSHIPS:
        if re.search(rf"(?i)\b(?:{pattern})\b", label):
            return enum, label
    return "", label


def _role_statement(text: str, name: str) -> bool:
    """A statement that defines who the person is to the user ("Mira is my project partner")."""
    first = re.escape(name.split()[0]) if name else r"\w+"
    return bool(re.match(rf"(?i)^(?:that\s+)?(?:{re.escape(name)}|{first})\s+(?:\w+\s+){{0,3}}?(?:is|was|'s)\s+(?:now\s+)?"
                         rf"(?:my|a|an|the)\s+", text.strip()))


class PersonalMemory:
    """Application-owned explicit memory operations for one signed-in account."""

    def __init__(self, email: str):
        self.email = str(email or "").strip()
        self.profile = UserProfileStore(user_id=self.email)

    # ── people ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _graph(self) -> tuple[dict, dict]:
        profile = self.profile.load()
        return profile, profile.setdefault("social_graph", {})

    def find_person(self, name: str) -> tuple[str | None, list[dict]]:
        """(key, candidates): a unique match by full name or first-name prefix; candidates when ambiguous."""
        _, graph = self._graph()
        wanted = " ".join(str(name or "").casefold().split())
        if not wanted:
            return None, []
        if wanted in graph:
            return wanted, [graph[wanted]]
        tokens = wanted.split()
        matches = []
        for key, entry in graph.items():
            existing = " ".join(str(entry.get("name") or key).casefold().split()).split()
            nickname = str(entry.get("nickname") or "").casefold()
            if existing[:len(tokens)] == tokens or (nickname and nickname == wanted):
                matches.append((key, entry))
        if len(matches) == 1:
            return matches[0][0], [matches[0][1]]
        return None, [entry for _, entry in matches]

    def person(self, key: str) -> dict | None:
        _, graph = self._graph()
        entry = graph.get(key)
        return {"key": key, **entry} if isinstance(entry, dict) else None

    def remember_about_person(self, name: str, text: str, *, chat_id: str | None = None,
                              message_id: str | None = None, replace: bool = False,
                              relationship_label: str | None = None, predicate: str | None = None,
                              value: str | None = None, single: bool = False) -> dict:
        """Persist an explicit statement about a person; newer explicit statements win over conflicting ones.

        Marker 221: a structured fact (``predicate`` / ``value``) from the per-turn memory step. A single-valued
        predicate (hostel, birthday, branch...) supersedes the current value: the old fact is KEPT, marked superseded
        (never shown as current), so "P4, not P3" is an update, not a second hostel."""
        clean_name = " ".join(str(name or "").split())[:80]
        body = " ".join(str(text or "").split()) if len(str(text or "")) < 400 else str(text or "").strip()
        body = body[:CONTEXT_LIMIT]
        if not clean_name or not body:
            raise ValueError("A person and something to remember are required.")
        profile, graph = self._graph()
        key, candidates = self.find_person(clean_name)
        if key is None and candidates:
            return {"status": "ambiguous", "candidates": [entry.get("name") for entry in candidates]}
        created_person = key is None
        if created_person:
            from memory.profile import plausible_person_name
            if not plausible_person_name(clean_name):
                raise ValueError("That doesn't look like a person's name.")
            key = clean_name.casefold()
            graph[key] = {"name": clean_name, "details": [], "facts": [], "source": "explicit", "created_at": _now()}
        entry = graph[key]
        entry.setdefault("person_id", uuid.uuid4().hex[:16])     # marker 221: a stable id, never the name
        facts: list[dict] = [fact for fact in entry.get("facts") or [] if isinstance(fact, dict)]
        superseded_value = None
        structured = bool(predicate and value)
        if structured:
            predicate = str(predicate)[:40]
            value = " ".join(str(value).split())[:120]
            current = [item for item in facts if item.get("predicate") == predicate and not item.get("superseded")]
            same = next((item for item in current if str(item.get("value") or "").casefold() == value.casefold()), None)
            if same is not None:
                same.update(updated_at=_now(), chat_id=chat_id or same.get("chat_id"),
                            message_id=message_id or same.get("message_id"))
                entry["facts"], entry["updated_at"] = facts[-FACTS_PER_PERSON:], _now()
                self.profile.save(profile)
                return {"status": "reinforced", "key": key, "name": entry.get("name", clean_name), "fact_id": same["id"],
                        "birthday": entry.get("birthday"), "relationship": entry.get("relationship_label") or "",
                        "created_person": created_person, "superseded_value": None}
            if single:
                for item in current:
                    item["superseded"], item["superseded_at"] = True, _now()
                    superseded_value = superseded_value or item.get("value")
        kind = "context" if len(body) > 300 else ("role" if _role_statement(body, entry.get("name", clean_name)) else "fact")
        birthday = parse_birthday(body, entry.get("name", clean_name))
        enum, label = relationship_from(body, entry.get("name", clean_name))
        action = "saved"
        if replace:
            facts = []
            action = "replaced"
        fact = {"id": uuid.uuid4().hex[:16], "text": body, "kind": kind, "created_at": _now(), "updated_at": _now(),
                "chat_id": chat_id, "message_id": message_id}
        if structured:
            fact.update(predicate=predicate, value=value)
        duplicate = None if structured else next(
            (item for item in facts if not item.get("superseded")
             and similar(item.get("text", ""), body) >= (0.9 if kind != "context" else 0.8)), None)
        if duplicate is not None:
            duplicate.update(text=body, updated_at=_now(), chat_id=chat_id or duplicate.get("chat_id"),
                             message_id=message_id or duplicate.get("message_id"))
            fact = duplicate
            action = "reinforced" if similar(duplicate.get("text", ""), body) >= 0.98 else "updated"
        else:
            if kind == "role":
                before = len(facts)
                facts = [item for item in facts if item.get("kind") != "role"]
                action = "updated" if len(facts) != before else action
            if birthday:
                before = len(facts)
                facts = [item for item in facts if item.get("kind") != "birthday" and not (
                    item.get("kind") == "fact" and parse_birthday(item.get("text", ""), entry.get("name", "")))]
                if len(facts) != before:
                    action = "updated"
                if kind == "fact":
                    fact["kind"] = "birthday"
            if superseded_value is not None:
                action = "updated"
            facts.append(fact)
        entry["facts"] = facts[-FACTS_PER_PERSON:]
        if birthday:
            entry["birthday"] = birthday
        if relationship_label:
            label = " ".join(str(relationship_label).split())[:80]
            enum = relationship_from(f"{entry.get('name', clean_name)} is my {label}", entry.get("name", clean_name))[0] or enum
        if label:
            entry["relationship_label"] = label
        if enum:
            entry["relationship"] = enum
        self._disambiguate(graph, key)
        entry["updated_at"] = _now()
        self.profile.save(profile)
        stored = self.person(key) or {}
        persisted = any(item.get("id") == fact["id"] and item.get("text") == body for item in stored.get("facts") or [])
        return {"status": action if persisted else "failed", "key": key, "name": stored.get("name", clean_name),
                "fact_id": fact["id"], "birthday": stored.get("birthday") if birthday else None,
                "relationship": stored.get("relationship_label") or "", "created_person": created_person,
                "superseded_value": superseded_value}

    @staticmethod
    def _disambiguate(graph: dict, key: str) -> None:
        """Marker 221 (real 26 Oct: friend a friend and sister a friend were mixed up): people who share a
        first name carry a short disambiguator ("college friend", "sister") so replies and lookups keep them apart."""
        entry = graph.get(key) or {}
        first = (str(entry.get("name") or key).split() or [""])[0].casefold()
        twins = [k for k, other in graph.items() if isinstance(other, dict)
                 and (str(other.get("name") or k).split() or [""])[0].casefold() == first]
        for twin in twins if len(twins) > 1 else []:
            other = graph[twin]
            label = str(other.get("relationship_label") or other.get("relationship") or "").strip()
            other["disambiguator"] = label or str(other.get("name") or twin)

    def forget_person_fact(self, key: str, fact_id: str) -> bool:
        profile, graph = self._graph()
        entry = graph.get(key)
        if not isinstance(entry, dict):
            return False
        facts = [fact for fact in entry.get("facts") or [] if isinstance(fact, dict)]
        kept = [fact for fact in facts if fact.get("id") != fact_id]
        if len(kept) == len(facts):
            return False
        removed = next(fact for fact in facts if fact.get("id") == fact_id)
        entry["facts"] = kept
        if removed.get("kind") == "birthday" or parse_birthday(removed.get("text", ""), entry.get("name", "")):
            entry.pop("birthday", None)
        if removed.get("kind") in {"role", "context"} and not any(item.get("kind") in {"role", "context"} for item in kept):
            entry.pop("relationship_label", None)
        entry["updated_at"] = _now()
        self.profile.save(profile)
        return not any(fact.get("id") == fact_id for fact in (self.person(key) or {}).get("facts") or [])

    def forget_person(self, key: str) -> dict:
        """Remove the person, their relationship memories and every fact this account shared about them."""
        entry = self.person(key)
        if entry is None:
            return {"removed": False}
        profile, graph = self._graph()
        graph.pop(key, None)
        self.profile.save(profile)
        removed_memories = 0
        manager = self._manager()
        try:
            normalized = " ".join(str(entry.get("name") or key).casefold().split())
            for memory in list(manager.get_all()):
                attributes = memory.attributes if isinstance(memory.attributes, dict) else {}
                person_name = attributes.get("person_name") or attributes.get("entity")
                if memory.domain == "RELATIONSHIP" and " ".join(str(person_name or "").casefold().split()) == normalized:
                    if manager.delete(memory.memory_id):
                        removed_memories += 1
        finally:
            manager.close()
        from services.initiative_engine import InitiativeEngine
        unshared = InitiativeEngine().delete_shared_facts_about(self.email, entry.get("name") or key)
        still_there = self.person(key) is not None
        return {"removed": not still_there, "name": entry.get("name"), "memories": removed_memories, "unshared": unshared}

    # ── the user themself ───────────────────────────────────────────────────────────────────────────────────────
    def _manager(self, read_only: bool = False):
        """The account's own memory store (SQLite + derived index) without chat-session side effects."""
        from memory.manager import MemoryManager
        return MemoryManager(user_id=self.email, read_only=read_only)

    def remember_about_self(self, text: str, *, chat_id: str | None = None, message_id: str | None = None,
                            defer_index: bool = False) -> dict:
        from memory.schema import Memory
        statement = " ".join(str(text or "").split())[:800]
        if not statement:
            raise ValueError("Something to remember is required.")
        manager = self._manager()
        try:
            existing = self._explicit_notes(manager)
            duplicate = next((memory for memory in existing
                              if similar((memory.attributes or {}).get("statement") or memory.summary, statement) >= 0.9), None)
            slot = self._slot(statement)
            conflicting = [memory for memory in existing if slot and self._slot((memory.attributes or {}).get("statement", "")) == slot]
            now = _now()
            if duplicate is not None:
                duplicate.attributes = {**(duplicate.attributes or {}), "statement": statement, "source_chat_id": chat_id,
                                        "source_message_id": message_id}
                duplicate.summary = f"The user asked Jupiter to remember: {statement}"
                duplicate.last_updated = now
                manager.save_bundle(duplicate, [], defer_index=defer_index)
                memory_id, status = duplicate.memory_id, "reinforced"
            else:
                for memory in conflicting:
                    manager.archive(memory.memory_id, reason="superseded_by_explicit_update")
                memory = Memory(
                    memory_id=f"explicit.{uuid.uuid4().hex[:20]}",
                    category="Explicit",
                    topic="Something the user asked Jupiter to remember",
                    summary=f"The user asked Jupiter to remember: {statement}",
                    memory_type="SEMANTIC",
                    domain="PREFERENCE" if re.search(r"(?i)\b(prefer|like|love|hate|favou?rite|enjoy|dislike)\b", statement) else "GENERAL",
                    attributes={"explicit": True, "user_confirmed": True, "memory_tier": "P1", "statement": statement,
                                "source_chat_id": chat_id, "source_message_id": message_id},
                    importance="High",
                    retention="Forever",
                    visibility="PERSONAL",
                    confidence=1.0,
                    source=f"explicit_user_request:{chat_id or 'unknown'}:{message_id or 'unknown'}",
                    created_at=now,
                    last_updated=now,
                )
                manager.save_bundle(memory, [], defer_index=defer_index)
                if chat_id and message_id:
                    try:
                        manager.add_memory_source(memory.memory_id, chat_id, message_id)
                    except Exception:
                        pass
                memory_id, status = memory.memory_id, ("updated" if conflicting else "saved")
            stored = manager.get_by_id(memory_id)
            ok = stored is not None and (stored.attributes or {}).get("statement") == statement
            return {"status": status if ok else "failed", "memory_id": memory_id}
        finally:
            manager.close()

    @staticmethod
    def _slot(statement: str) -> str:
        """'my favourite colour is blue' / 'I prefer dark mode' -> a comparable slot for conflict replacement."""
        match = re.match(r"(?i)^(?:that\s+)?my\s+((?:favou?rite\s+)?[\w-]+(?:\s+[\w-]+)?)\s+(?:is|are)\b", statement.strip())
        if match:
            return "my:" + _fold(match.group(1))
        match = re.match(r"(?i)^(?:that\s+)?i\s+prefer\s+(?:a\s+|the\s+)?(dark|light)\s+(mode|theme)\b", statement.strip())
        if match:
            return "prefer:" + match.group(2).casefold()
        return ""

    @staticmethod
    def _explicit_notes(manager) -> list:
        return [memory for memory in manager.get_all() if (memory.attributes or {}).get("explicit")]

    def self_notes(self) -> list[dict]:
        from storage.sqlite_store import SQLiteStore
        store = SQLiteStore(user_id=self.email, read_only=True)      # reads never open the semantic index
        try:
            return [{"id": memory.memory_id, "text": (memory.attributes or {}).get("statement") or memory.summary,
                     "updated_at": memory.last_updated} for memory in self._explicit_notes(store)]
        finally:
            store.close()

    def standing_preferences(self, limit: int = 6) -> list[str]:
        """Explicit notes about how the user wants to be answered or addressed (a bounded allowlist, always on)."""
        return [note["text"] for note in self.self_notes() if _STANDING_PREFERENCE.search(note["text"])][:limit]

    def forget_memory(self, memory_id: str) -> bool:
        manager = self._manager()
        try:
            memory = manager.get_by_id(memory_id)
            if memory is None or not (memory.attributes or {}).get("explicit"):
                return False
            return bool(manager.delete(memory_id)) and manager.get_by_id(memory_id) is None
        finally:
            manager.close()

    @staticmethod
    def _words(text: str) -> set[str]:
        stop = {"that", "this", "the", "a", "an", "my", "is", "are", "was", "to", "of", "and", "about", "user",
                "user's", "i", "me", "he", "she", "they", "it", "has", "have", "likes", "like"}
        return {word for word in re.findall(r"[\w']+", str(text or "").casefold()) if word not in stop and len(word) > 1}

    def forget_matching(self, statement: str) -> dict:
        """Forget the ONE saved fact (about a person, or an explicit note about the user) that best matches
        ``statement``. Nothing is removed when no fact clearly matches, or when several match equally well (the caller
        asks which one). Returns {"removed": bool, "text": str, "candidates": [texts]}."""
        wanted = self._words(statement)
        if not wanted:
            return {"removed": False, "text": "", "candidates": []}
        scored = []
        _, graph = self._graph()
        for key, entry in graph.items():
            if not isinstance(entry, dict):
                continue
            name_words = self._words(entry.get("name") or key)
            for fact in entry.get("facts") or []:
                if isinstance(fact, dict) and fact.get("id"):
                    words = self._words(fact.get("text")) | name_words
                    scored.append((len(wanted & words) / len(wanted), "person", key, fact["id"], str(fact.get("text"))))
        for note in self.self_notes():
            words = self._words(note["text"])
            scored.append((len(wanted & words) / len(wanted), "self", "", note["id"], str(note["text"])))
        scored = [item for item in scored if item[0] >= 0.75]          # every important word (bar one) must match
        if not scored:
            return {"removed": False, "text": "", "candidates": []}
        best = max(item[0] for item in scored)
        top = [item for item in scored if item[0] == best]
        if len(top) > 1:
            return {"removed": False, "text": "", "candidates": [item[4] for item in top][:3]}
        _, kind, key, fact_id, text = top[0]
        removed = self.forget_person_fact(key, fact_id) if kind == "person" else self.forget_memory(fact_id)
        return {"removed": bool(removed), "text": text, "candidates": []}

    _GOAL_FILLER = frozenset({"how", "make", "learn", "learning", "want", "for", "in", "on", "with", "do", "goal",
                              "working", "create"})

    def forget_goal(self, statement: str) -> dict:
        """Forget ONE learned goal that clearly matches ``statement`` everywhere it lives: the profile's goals, its
        GOAL memory (archived out of retrieval, vector removed) and the derived mentions/summaries that would bring it
        back ("topic_mentions", the interest note, the monthly summary). Nothing else is touched. Real WT 17 Oct: a
        one-off how-to question had become a goal; "remove it from memory" left it everywhere."""
        wanted = self._words(statement)
        profile = self.profile.load()
        goals = profile.get("goals") if isinstance(profile.get("goals"), dict) else {}
        scored = []
        for key, goal in goals.items():
            name = str(goal.get("name") or key) if isinstance(goal, dict) else str(key)
            words = self._words(name) - self._GOAL_FILLER
            overlap = len(wanted & words)
            if words and overlap >= min(2, len(words)) and overlap / len(words) >= 0.5:
                scored.append((overlap / len(words), key, name))
        if not scored:
            return {"removed": False, "text": "", "candidates": []}
        best = max(item[0] for item in scored)
        top = [item for item in scored if item[0] == best]
        if len(top) > 1:
            return {"removed": False, "text": "", "candidates": [item[2] for item in top][:3]}
        _, key, name = top[0]
        goals.pop(key, None)
        self.profile.save(profile)
        topics = {str(key).casefold(), name.casefold()}
        manager = self._manager()
        try:
            for memory in manager.get_all():
                attributes = memory.attributes if isinstance(memory.attributes, dict) else {}
                if memory.domain == "GOAL" and ({str(memory.topic or "").casefold(),
                                                 str(attributes.get("goal_name") or "").casefold()} & topics):
                    manager.delete(memory.memory_id)
            database = manager.database
            mentions = database.get_state("topic_mentions", {})
            if isinstance(mentions, dict) and any(topic in mentions for topic in topics):
                database.set_state("topic_mentions", {k: v for k, v in mentions.items() if k.casefold() not in topics})
            phrase = "|".join(re.escape(topic) for topic in topics if topic)
            clause = re.compile(rf"(?:,\s*)?(?:{phrase})(?:\s*\([\d.]+/10\))?", re.IGNORECASE)
            for table, column in (("internal_notes", "content"), ("monthly_summaries", "summary")):
                try:
                    rows = database.connection.execute(
                        f"SELECT rowid, {column} FROM {table} WHERE user_id=?", (database.user_id,)).fetchall()
                except Exception:
                    continue
                for rowid, text in rows:
                    cleaned = clause.sub("", str(text or "")) if text else text
                    if cleaned != text:
                        database.connection.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?",
                                                    (re.sub(r":\s*,\s*", ": ", cleaned), rowid))
            database.connection.commit()
        finally:
            manager.close()
        return {"removed": True, "text": name, "candidates": []}

    def forget_all_self_notes(self) -> int:
        manager = self._manager()
        try:
            return sum(1 for memory in self._explicit_notes(manager) if manager.delete(memory.memory_id))
        finally:
            manager.close()
