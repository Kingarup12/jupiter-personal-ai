"""Durable, user-owned profile data for Jupiter.

The profile is deliberately separate from the database folder name.  A folder is
an implementation detail and can stay stable while a user changes a display or
preferred name.  This file is a small, inspectable source of truth that is
also rebuilt from long-term profile memories when Jupiter starts.
"""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from config.settings import PROFILE_PATH, USER_ID, USERS_DIR, _safe_user_id

# Marker 201: automatic extraction saved sentence fragments as "people" (real data 17 Oct: "at 52 right now" with the
# relationship "volume", "Okay . I can see o kay wha t", "What", "Tomorrow"). Only plausible names become people.
_NAME_STOPWORDS = frozenset("""the a an and or but i me my mine you your he she it its we they them us is are was were be
been to of in on at for with from by what who whom how when where why which that this these those do does did not no
yes okay ok now right today tomorrow yesterday much all about so still just can could see also her him his their our
here there then than very really please thanks thank hello hi hey uske voh woh mujhe hai hain nahi kya aur toh bhi ek
mera meri mere tum aap main hum""".split())


def plausible_person_name(name: str) -> bool:
    """A short real-looking name ("a friend", "Meera Sharma", "J. R. Rao"), not a sentence fragment or ASR debris."""
    value = " ".join(str(name or "").split())
    if not value or len(value) > 40 or any(ch.isdigit() for ch in value):
        return False
    words = value.replace("-", " ").split()
    if not 1 <= len(words) <= 4:
        return False
    for word in words:
        core = word.strip(".'")
        if not core or not all(unicodedata.category(ch)[0] in "LM" or ch in ".'" for ch in word):   # letters + matras
            return False
        if core.casefold() in _NAME_STOPWORDS:
            return False
    short = sum(1 for word in words if len(word.strip(".'")) <= 2 and not word.endswith("."))
    return short * 2 <= len(words) - (1 if len(words) > 1 else 0)


class UserProfileStore:
    """Persist identity, communication preferences, and companion continuity."""

    DEFAULT = {
        "schema_version": 2,
        "user_id": "",
        "formal_name": None,
        "preferred_name": None,
        "preferred_language": None,
        "address_style": "name_or_neutral",
        "avoid_addresses": ["bhaiya", "bhai", "bro", "sir"],
        "communication_style": {
            "reply_length": None,
            "emoji_level": None,
            "formality": None,
            "sarcasm": None,
            "storytelling": None,
        },
        "preferences": {},
        "goals": {},
        "social_graph": {},
        "life_chapters": [],
        "relationship": {"stage": "NEW", "signals": [], "interaction_count": 0},
        "emotional_state": None,
        # --- v2 fields ---
        "emotional_milestones": [],
        "conversation_style_signals": {
            "avg_message_length": None,
            "emoji_frequency": None,
            "question_frequency": None,
            "hinglish_ratio": None,
            "samples": 0,
        },
        "personality_modulation": {
            "energy_default": "playful",
            "current_modulation": None,
            "reason": None,
        },
        "internal_reflections": [],
        "curiosity_backlog": [],
        "personality_provenance_baseline": {},
        "personality_provenance": [],
        "updated_at": None,
    }

    VALID_RELATIONSHIP_TYPES = {
        "friend", "sibling", "classmate", "colleague", "family",
        "partner", "mentor", "neighbor", "other",
    }

    VISIBILITY_SETTINGS_DEFAULT = {
        "display_name_visible": True,
        "bio_visible": True,
        "interests_visible": False,
    }

    def __init__(self, path: Path | None = None, user_id: str | None = None):
        # One profile per Jupiter identity. Without this, every signed-in user
        # would read and write the same profile.json, so one user's visibility
        # choices and People I Know entries would leak into another account.
        self.user_id = _safe_user_id(user_id) if user_id else USER_ID
        if path is not None:
            self.path = Path(path)
        elif user_id:
            self.path = USERS_DIR / self.user_id / "profile.json"
        else:
            self.path = Path(PROFILE_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        data = deepcopy(self.DEFAULT)
        data["user_id"] = self.user_id
        if not self.path.exists():
            return data
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return data
        if not isinstance(loaded, dict):
            return data
        merged = self._merge(data, loaded)
        if merged.get("schema_version", 1) < 2:
            merged = self._migrate_v1_to_v2(merged)
        return merged

    def save(self, profile: dict) -> dict:
        profile = self._merge(deepcopy(self.DEFAULT), profile)
        profile["user_id"] = self.user_id
        profile["updated_at"] = datetime.now().isoformat()
        from storage.atomic_json import write_json
        write_json(self.path, profile)
        return profile
    def hydrate_authenticated_name(self, name: str | None) -> dict:
        """Synchronize profile identity from the authenticated account record.

        The registered display name is authoritative.  Learned name memories
        may assist conversation, but cannot keep an older name active after an
        account/profile rename.
        """
        if not name or not isinstance(name, str) or not name.strip():
            return self.load()
        canonical_name = name.strip()
        profile = self.load()
        changed = False
        if profile.get("formal_name") != canonical_name:
            profile["formal_name"] = canonical_name
            changed = True
        if profile.get("preferred_name") != canonical_name:
            profile["preferred_name"] = canonical_name
            changed = True
        if changed:
            self.save(profile)
        return profile

    def update_from_memory(self, memory) -> dict:
        """Apply explicit profile facts; never turn an inference into identity."""

        profile = self.load()
        attributes = memory.attributes or {}
        if memory.memory_id.startswith("identity.name") or memory.domain == "PROFILE":
            formal_name = attributes.get("formal_name") or attributes.get("name")
            preferred_name = attributes.get("preferred_name")
            if isinstance(formal_name, str) and formal_name.strip():
                profile["formal_name"] = formal_name.strip()
            if isinstance(preferred_name, str) and preferred_name.strip():
                profile["preferred_name"] = preferred_name.strip()

        if memory.domain == "PREFERENCE" or memory.category.lower().startswith("preference"):
            language = attributes.get("preferred_language")
            if isinstance(language, str) and language.strip():
                profile["preferred_language"] = language.strip()
            style = attributes.get("communication_style")
            if isinstance(style, dict):
                profile["communication_style"].update(
                    {key: value for key, value in style.items() if value is not None}
                )
            for key in ("reply_length", "emoji_level", "formality", "sarcasm", "storytelling"):
                if attributes.get(key) is not None:
                    profile["communication_style"][key] = attributes[key]
            if attributes.get("address_style"):
                profile["address_style"] = attributes["address_style"]
            avoided = attributes.get("avoid_addresses")
            if isinstance(avoided, list):
                profile["avoid_addresses"] = sorted(
                    set(profile["avoid_addresses"]).union(
                        str(item).lower() for item in avoided if str(item).strip()
                    )
                )
            source = str(getattr(memory, "source", "") or "")
            if source.startswith("conversation:"):
                entries = profile.setdefault("personality_provenance", [])
                entries.append({
                    "source": source,
                    "values": {key: profile["communication_style"].get(key)
                               for key in ("reply_length", "emoji_level", "formality", "sarcasm", "storytelling")
                               if profile["communication_style"].get(key) is not None},
                })
                profile["personality_provenance"] = entries[-100:]

        if memory.domain == "GOAL":
            self._update_goal(profile, memory)
        if memory.domain == "RELATIONSHIP":
            self._update_social_graph(profile, memory)
            self._update_relationship_history(profile, memory)
        if memory.category == "Life Chapter":
            self._update_chapter(profile, memory)
        if memory.domain == "EMOTION" and (memory.attributes or {}).get("emotional_milestone"):
            self.record_emotional_milestone(
                event=memory.topic,
                emotion=memory.emotion,
                context=memory.summary,
            )
        return self.save(profile)

    def remove_chat_provenance(self, chat_id: str) -> bool:
        """Remove conversation-sourced personality evidence after chat deletion."""

        profile = self.load()
        prefix = f"conversation:{chat_id}:"
        entries = profile.get("personality_provenance") or []
        kept = [entry for entry in entries if not str(entry.get("source") or "").startswith(prefix)]
        if len(kept) == len(entries):
            return False
        profile["personality_provenance"] = kept
        style = profile.setdefault("communication_style", {})
        for key in ("reply_length", "emoji_level", "formality", "sarcasm", "storytelling"):
            latest = next((entry.get("values", {}).get(key) for entry in reversed(kept)
                           if entry.get("values", {}).get(key) is not None), None)
            style[key] = latest
        self.save(profile)
        return True

    def observe_message(self, message: str, emotion=None) -> dict:
        """Keep only user-approved style signals and short-lived emotion context."""

        profile = self.load()
        profile["relationship"]["interaction_count"] += 1
        self._infer_explicit_style(profile, message)
        self._observe_style_signals(profile, message)
        self._update_relationship_stage(profile)
        if emotion and getattr(emotion, "label", "neutral") != "neutral":
            profile["emotional_state"] = {
                "label": emotion.label,
                "intensity": emotion.intensity,
                "source": message[:280],
                "recorded_at": datetime.now().isoformat(),
            }
        return self.save(profile)

    def profile_instruction(self) -> str:
        """Return bounded response guidance, with no hidden reasoning or guesses."""

        profile = self.load()
        parts = []
        name = profile.get("preferred_name") or profile.get("formal_name")
        if name:
            parts.append(f"Saved user name: {name}. Use it only when natural.")
        language = profile.get("preferred_language")
        if language:
            parts.append(f"Saved language preference: {language}.")
        avoided = profile.get("avoid_addresses") or []
        if avoided:
            parts.append("Never address the user as " + ", ".join(avoided) + ".")
        style = profile.get("communication_style") or {}
        for key, label in (
            ("reply_length", "Reply length"),
            ("emoji_level", "Emoji use"),
            ("formality", "Formality"),
            ("sarcasm", "Sarcasm"),
            ("storytelling", "Storytelling"),
        ):
            if style.get(key):
                parts.append(f"{label} preference: {style[key]}.")

        # Implicit style suggestions from observed signals
        signals = profile.get("conversation_style_signals") or {}
        samples = signals.get("samples", 0)
        if samples >= 10:
            avg_len = signals.get("avg_message_length") or 0
            if avg_len > 0 and avg_len < 6 and not style.get("reply_length"):
                parts.append("Implicit style signal: user tends to write short messages; keep replies concise.")
            emoji_freq = signals.get("emoji_frequency") or 0
            if emoji_freq > 0.5 and not style.get("emoji_level"):
                parts.append("Implicit style signal: user uses emojis frequently; match their energy.")
            hinglish = signals.get("hinglish_ratio") or 0
            if hinglish > 0.3 and not language:
                parts.append("Implicit style signal: user frequently uses Hinglish; reply in natural Hinglish.")

        emotional = self.current_emotional_state(profile)
        if emotional:
            parts.append(
                "Emotional continuity: the user recently said they felt "
                f"{emotional['label']}; be gentle and do not assume it has passed."
            )
        relationship = profile.get("relationship") or {}
        if relationship.get("stage"):
            parts.append(f"Relationship stage: {relationship['stage']}.")

        # Active life chapter context
        chapter = self.get_active_chapter(profile)
        if chapter:
            parts.append(f"Current life chapter: {chapter['name']} — {chapter['summary'][:120]}.")

        return "\n".join(parts)

    def current_emotional_state(self, profile=None, max_hours=18):
        profile = profile or self.load()
        state = profile.get("emotional_state")
        if not isinstance(state, dict) or not state.get("recorded_at"):
            return None
        try:
            age = datetime.now() - datetime.fromisoformat(state["recorded_at"])
        except (TypeError, ValueError):
            return None
        return state if age.total_seconds() <= max_hours * 3600 else None

    def _update_goal(self, profile, memory):
        attributes = memory.attributes or {}
        name = str(attributes.get("goal_name") or memory.topic).strip()
        if not name:
            return
        profile["goals"][name.lower()] = {
            "name": name,
            "status": attributes.get("status", "active"),
            "progress": attributes.get("progress"),
            "last_update": memory.summary,
            "updated_at": datetime.now().isoformat(),
        }

    def _update_social_graph(self, profile, memory):
        attributes = memory.attributes or {}
        name = attributes.get("person_name") or attributes.get("entity")
        if not isinstance(name, str) or not name.strip():
            return
        name = " ".join(name.strip().split())
        if not plausible_person_name(name):
            return                                            # never turn a sentence fragment into a person
        relationship = str(attributes.get("relationship") or attributes.get("relation_to_user") or "").strip().casefold()
        if len(relationship.split()) > 4 or any(ch.isdigit() for ch in relationship):
            relationship = ""
        graph = profile["social_graph"]
        key = self._matching_person_key(graph, name, relationship)
        canonical_key = name.casefold()
        entry = graph.get(key) if key else None
        if entry is None:
            key = canonical_key
            entry = {"name": name, "details": []}
            graph[key] = entry
        elif len(name.split()) > len(str(entry.get("name") or "").split()) and key != canonical_key:
            del graph[key]
            key = canonical_key
            graph[key] = entry
            entry["name"] = name
        if relationship and attributes.get("relationship"):
            entry["relationship"] = attributes["relationship"]
        if memory.summary not in entry["details"]:
            entry["details"] = (entry["details"] + [memory.summary])[-8:]
        entry["updated_at"] = datetime.now().isoformat()

    @staticmethod
    def _matching_person_key(graph: dict, name: str, relationship: str = "") -> str | None:
        """Resolve a safe exact/unique short-name match without merging namesakes."""

        normalized = " ".join(name.casefold().split())
        if normalized in graph:
            return normalized
        incoming_tokens = normalized.split()
        candidates = []
        for key, entry in graph.items():
            existing_name = " ".join(str(entry.get("name") or key).casefold().split())
            existing_tokens = existing_name.split()
            if not incoming_tokens or not existing_tokens or incoming_tokens[0] != existing_tokens[0]:
                continue
            shorter, longer = sorted((incoming_tokens, existing_tokens), key=len)
            if shorter != longer[:len(shorter)]:
                continue
            existing_relation = str(entry.get("relationship") or "").casefold()
            if relationship and existing_relation and relationship != existing_relation:
                continue
            candidates.append(key)
        return candidates[0] if len(candidates) == 1 else None

    # ------------------------------------------------------------------
    # Profile visibility & People I Know
    # ------------------------------------------------------------------

    def visibility_settings(self) -> dict:
        """Return the user's current profile visibility preferences."""
        profile = self.load()
        defaults = deepcopy(self.VISIBILITY_SETTINGS_DEFAULT)
        stored = profile.get("visibility_settings")
        if isinstance(stored, dict):
            for key in defaults:
                if key in stored and isinstance(stored[key], bool):
                    defaults[key] = stored[key]
        return defaults

    def set_visibility_settings(self, settings: dict) -> dict:
        """Persist field-level profile visibility preferences."""
        profile = self.load()
        current = profile.get("visibility_settings")
        if not isinstance(current, dict):
            current = deepcopy(self.VISIBILITY_SETTINGS_DEFAULT)
        for key in self.VISIBILITY_SETTINGS_DEFAULT:
            if key in settings and isinstance(settings[key], bool):
                current[key] = settings[key]
        profile["visibility_settings"] = current
        self.save(profile)
        return current

    def public_profile(self, username: str | None = None) -> dict:
        """Return only the fields this user has allowed to be publicly visible.

        The caller supplies the target's Jupiter username. The internal
        identifier is derived from an email address, so it is never returned.
        Nothing outside ``VISIBILITY_SETTINGS_DEFAULT`` is ever considered here:
        chats, Gmail, tokens, memories, files and any other stored data stay
        private regardless of these settings.
        """
        profile = self.load()
        visibility = self.visibility_settings()
        public: dict = {"username": str(username or "").strip()}
        if visibility.get("display_name_visible"):
            name = profile.get("formal_name") or profile.get("preferred_name")
            if name:
                public["display_name"] = name
        if visibility.get("bio_visible"):
            bio = str(profile.get("custom_instructions") or "").strip()
            if bio:
                public["bio"] = bio
        if visibility.get("interests_visible"):
            interests = profile.get("preferences") or {}
            if interests:
                public["interests"] = interests
        if len(public) == 1:
            public["visibility"] = "private"
            public["message"] = "This user has not shared their profile."
        else:
            public["visibility"] = "partial"
        return public

    def list_people(self) -> list[dict]:
        """Return the user's social graph as a list of people entries."""
        profile = self.load()
        graph = profile.get("social_graph") or {}
        return [
            {
                "key": key,
                "name": entry.get("name", key),
                "relationship": entry.get("relationship", ""),
                "jupiter_username": entry.get("jupiter_username", ""),
                "details": entry.get("details", []),
                "updated_at": entry.get("updated_at", ""),
                # Explicit, user-provided facts (services.personal_memory); never inferred.
                "facts": [
                    {key_: fact.get(key_) for key_ in ("id", "text", "kind", "created_at", "updated_at", "predicate", "value")}
                    for fact in entry.get("facts") or [] if isinstance(fact, dict) and fact.get("text")
                    and not fact.get("superseded")          # marker 221: an old hostel / birthday is history
                ],
                "person_id": entry.get("person_id", ""),
                "disambiguator": entry.get("disambiguator", ""),
                "relationship_label": entry.get("relationship_label", ""),
                "birthday": entry.get("birthday"),
                "nickname": entry.get("nickname", ""),
            }
            for key, entry in sorted(
                graph.items(),
                key=lambda item: item[1].get("name", item[0]).lower(),
            )
        ]

    def upsert_person(
        self, name: str, relationship: str = "", jupiter_username: str = "", notes: str = "",
    ) -> dict:
        """Add or update a person in the social graph."""
        if relationship and relationship not in self.VALID_RELATIONSHIP_TYPES:
            raise ValueError(
                f"Invalid relationship type. Allowed: {', '.join(sorted(self.VALID_RELATIONSHIP_TYPES))}"
            )
        profile = self.load()
        clean_name = " ".join(name.strip().split())
        key = clean_name.casefold()
        if not key:
            raise ValueError("Person name is required.")
        graph = profile["social_graph"]
        matched_key = self._matching_person_key(graph, clean_name, relationship.casefold())
        entry = graph.get(matched_key) if matched_key else None
        if entry is None:
            entry = {"name": clean_name, "details": []}
            graph[key] = entry
        elif len(clean_name.split()) > len(str(entry.get("name") or "").split()) and matched_key != key:
            del graph[matched_key]
            graph[key] = entry
            entry["name"] = clean_name
        else:
            key = matched_key
        if relationship:
            entry["relationship"] = relationship
        if jupiter_username:
            entry["jupiter_username"] = jupiter_username.strip()
        if notes:
            if notes not in entry["details"]:
                entry["details"] = (entry["details"] + [notes])[-8:]
        entry["updated_at"] = datetime.now().isoformat()
        self.save(profile)
        return {"key": key, **entry}

    def remove_person(self, name: str) -> bool:
        """Remove a person from the social graph by name key."""
        profile = self.load()
        key = name.strip().lower()
        if key not in profile.get("social_graph", {}):
            return False
        del profile["social_graph"][key]
        self.save(profile)
        return True

    def _update_chapter(self, profile, memory):
        event = {
            "name": memory.topic,
            "summary": memory.summary,
            "updated_at": datetime.now().isoformat(),
        }
        existing = next(
            (item for item in profile["life_chapters"] if item.get("name") == event["name"]),
            None,
        )
        if existing:
            existing["summary"] = event["summary"]
            existing["updated_at"] = event["updated_at"]
        else:
            profile["life_chapters"].append(event)

    @staticmethod
    def _infer_explicit_style(profile, message):
        text = message.lower()
        style = profile["communication_style"]
        if any(phrase in text for phrase in ("short replies", "keep it short", "brief reply", "concise reply")):
            style["reply_length"] = "concise"
        elif any(phrase in text for phrase in ("detailed reply", "explain in detail", "long reply")):
            style["reply_length"] = "detailed"
        if any(phrase in text for phrase in ("less emoji", "no emoji")):
            style["emoji_level"] = "minimal"
        elif any(phrase in text for phrase in ("more emoji", "use emojis")):
            style["emoji_level"] = "playful"
        if "be formal" in text:
            style["formality"] = "formal"
        elif any(phrase in text for phrase in ("be casual", "gen z", "gennz")):
            style["formality"] = "casual"
        if "don't be sarcastic" in text or "dont be sarcastic" in text:
            style["sarcasm"] = "avoid"
        elif "be sarcastic" in text:
            style["sarcasm"] = "light when appropriate"
        if any(phrase in text for phrase in ("tell stories", "storytelling", "story style")):
            style["storytelling"] = "use when helpful"
        elif any(phrase in text for phrase in ("no stories", "skip stories")):
            style["storytelling"] = "avoid unless requested"

    @staticmethod
    def _update_relationship_stage(profile):
        count = profile["relationship"]["interaction_count"]
        profile["relationship"]["stage"] = (
            "CLOSE_FRIEND" if count >= 80 else "FRIEND" if count >= 30 else
            "REGULAR" if count >= 15 else "KNOWN" if count >= 2 else "NEW"
        )

    @staticmethod
    def _update_relationship_history(profile, memory):
        signal = (memory.attributes or {}).get("relationship_signal")
        if not isinstance(signal, str) or not signal.strip():
            return
        relationship = profile["relationship"]
        entries = relationship.setdefault("signals", [])
        entry = {"message": signal[:280], "recorded_at": datetime.now().isoformat()}
        if not any(item.get("message") == entry["message"] for item in entries):
            relationship["signals"] = (entries + [entry])[-40:]

    @classmethod
    def _merge(cls, base, extra):
        for key, value in extra.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                base[key] = cls._merge(base[key], value)
            else:
                base[key] = value
        return base

    # ------------------------------------------------------------------
    # Emotional milestones
    # ------------------------------------------------------------------

    def record_emotional_milestone(self, event: str, emotion: str, context: str) -> dict:
        """Store a significant emotional moment for future warm callbacks."""

        profile = self.load()
        milestones = profile.get("emotional_milestones", [])
        # Avoid exact duplicates
        if any(item.get("event") == event[:280] for item in milestones):
            return profile
        milestones.append({
            "event": event[:280],
            "emotion": emotion,
            "context": context[:500],
            "recorded_at": datetime.now().isoformat(),
        })
        profile["emotional_milestones"] = milestones[-20:]  # Keep last 20
        return self.save(profile)

    def get_emotional_milestones(self, profile=None) -> list[dict]:
        """Return stored emotional milestones."""

        profile = profile or self.load()
        return profile.get("emotional_milestones", [])

    # ------------------------------------------------------------------
    # Life chapters
    # ------------------------------------------------------------------

    def get_active_chapter(self, profile=None) -> dict | None:
        """Return the most recently updated life chapter, if any."""

        profile = profile or self.load()
        chapters = profile.get("life_chapters", [])
        if not chapters:
            return None
        return max(chapters, key=lambda c: c.get("updated_at", ""))

    def infer_chapters_from_memories(self, memories) -> list[str]:
        """Create life chapters from temporal memory clusters.

        If 5+ memories in a 3-month window reference a common keyword, create a
        chapter for that phase.  Returns a list of newly created chapter names.
        """

        from collections import Counter

        chapter_keywords = {
            "college": "College Phase",
            "university": "University Phase",
            "school": "School Days",
            "internship": "Internship Period",
            "job": "Work Life",
            "career": "Career Phase",
            "jee": "JEE Preparation",
            "wbjee": "WBJEE Preparation",
            "exam": "Exam Season",
        }
        profile = self.load()
        existing_names = {c.get("name", "").lower() for c in profile.get("life_chapters", [])}
        corpus = Counter()
        for memory in memories:
            text = f"{memory.topic} {memory.summary}".lower()
            for keyword, chapter_name in chapter_keywords.items():
                if keyword in text and chapter_name.lower() not in existing_names:
                    corpus[chapter_name] += 1

        created = []
        for chapter_name, count in corpus.items():
            if count >= 5:
                profile["life_chapters"].append({
                    "name": chapter_name,
                    "summary": f"Inferred from {count} related memories.",
                    "updated_at": datetime.now().isoformat(),
                })
                created.append(chapter_name)
        if created:
            self.save(profile)
        return created

    # ------------------------------------------------------------------
    # Implicit style observation
    # ------------------------------------------------------------------

    @staticmethod
    def _observe_style_signals(profile, message):
        """Track rolling style averages from actual user messages."""

        signals = profile.get("conversation_style_signals", {})
        if not isinstance(signals, dict):
            signals = {"avg_message_length": None, "emoji_frequency": None,
                       "question_frequency": None, "hinglish_ratio": None, "samples": 0}

        words = message.split()
        word_count = len(words)
        has_emoji = bool(re.search(
            r'[\U0001F300-\U0001F9FF\U00002702-\U000027B0\U0000FE00-\U0000FE0F]',
            message,
        ))
        has_question = "?" in message

        # Detect Hinglish words (common Hindi words in Roman script)
        hinglish_markers = {
            "hai", "hain", "kya", "nahi", "nhi", "haan", "yaar", "bhai",
            "mein", "aur", "toh", "karo", "kar", "bolo", "bol", "dekh",
            "accha", "acha", "theek", "thik", "kaise", "kaisa", "abhi",
            "woh", "yeh", "mujhe", "tumhe", "tum", "main", "hum", "raha",
            "rahi", "bahut", "bohot", "lekin", "matlab", "samajh", "pata",
            "chahiye", "sochna", "batao", "sunna", "chalo", "chal",
        }
        hindi_count = sum(1 for w in words if w.lower().strip(".,?!") in hinglish_markers)
        hinglish_ratio = hindi_count / max(word_count, 1)

        n = signals.get("samples", 0)
        # Exponential moving average (alpha=0.15 for smoothing)
        alpha = 0.15 if n >= 5 else 1.0 / max(n + 1, 1)

        prev_len = signals.get("avg_message_length") or word_count
        signals["avg_message_length"] = round(prev_len * (1 - alpha) + word_count * alpha, 1)

        prev_emoji = signals.get("emoji_frequency") or (1.0 if has_emoji else 0.0)
        signals["emoji_frequency"] = round(
            prev_emoji * (1 - alpha) + (1.0 if has_emoji else 0.0) * alpha, 3
        )

        prev_question = signals.get("question_frequency") or (1.0 if has_question else 0.0)
        signals["question_frequency"] = round(
            prev_question * (1 - alpha) + (1.0 if has_question else 0.0) * alpha, 3
        )

        prev_hinglish = signals.get("hinglish_ratio") or hinglish_ratio
        signals["hinglish_ratio"] = round(
            prev_hinglish * (1 - alpha) + hinglish_ratio * alpha, 3
        )

        signals["samples"] = n + 1
        profile["conversation_style_signals"] = signals

    # ------------------------------------------------------------------
    # Internal reflections (per-session, ephemeral)
    # ------------------------------------------------------------------

    def save_reflection(self, note: str) -> dict:
        """Store a per-turn self-reflection note. Keeps the last 10."""

        profile = self.load()
        reflections = profile.get("internal_reflections", [])
        reflections.append({
            "note": note[:500],
            "recorded_at": datetime.now().isoformat(),
        })
        profile["internal_reflections"] = reflections[-10:]
        return self.save(profile)

    def get_recent_reflections(self, profile=None, limit=3) -> list[str]:
        """Return the most recent reflection notes."""

        profile = profile or self.load()
        reflections = profile.get("internal_reflections", [])
        return [r["note"] for r in reflections[-limit:]]

    # ------------------------------------------------------------------
    # Cross-session curiosity backlog
    # ------------------------------------------------------------------

    def add_curiosity_item(self, topic: str) -> dict:
        """Queue a topic for warm follow-up in a future session."""

        profile = self.load()
        backlog = profile.get("curiosity_backlog", [])
        if any(item.get("topic", "").lower() == topic.lower() for item in backlog):
            return profile
        backlog.append({
            "topic": topic[:200],
            "created_at": datetime.now().isoformat(),
        })
        profile["curiosity_backlog"] = backlog[-5:]  # Oldest-first, max 5
        return self.save(profile)

    def pop_curiosity_item(self) -> str | None:
        """Retrieve and remove the oldest cross-session curiosity item."""

        profile = self.load()
        backlog = profile.get("curiosity_backlog", [])
        if not backlog:
            return None
        item = backlog.pop(0)
        self.save(profile)
        return item.get("topic")

    # ------------------------------------------------------------------
    # Schema migration
    # ------------------------------------------------------------------

    @staticmethod
    def _migrate_v1_to_v2(profile: dict) -> dict:
        """Non-destructive migration from schema v1 to v2."""

        profile.setdefault("emotional_milestones", [])
        profile.setdefault("conversation_style_signals", {
            "avg_message_length": None,
            "emoji_frequency": None,
            "question_frequency": None,
            "hinglish_ratio": None,
            "samples": 0,
        })
        profile.setdefault("personality_modulation", {
            "energy_default": "playful",
            "current_modulation": None,
            "reason": None,
        })
        profile.setdefault("internal_reflections", [])
        profile.setdefault("curiosity_backlog", [])
        profile["schema_version"] = 2
        return profile
