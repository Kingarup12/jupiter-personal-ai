"""Deterministic memory intent handling for common companion interactions.

Local models can be slow and may misunderstand mixed Hinglish.  The patterns
here intentionally cover only high-confidence cases; everything ambiguous is
still sent to the memory analyzer.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from memory.schema import Memory
from memory.personality import PersonalityStore
from memory.types import expiry_for, retention_label


# Marker 221 (real 26 Oct owner store: fake names "With", "Doing Right Now", "Watching"; "What is the user's current
# latest mail" used 40 times; a person named "vo CSE AIML mai"): regex clause-slicing no longer writes durable facts.
# Durable facts come from the per-turn extractor (memory/turn_memory.py). Only these proven, instant rules still write;
# short-lived conversation notes (episodic./short_term.) keep their old non-durable role.
REGEX_DURABLE_IDS = frozenset({"preference.language", "preference.address_style", "preference.communication_style"})


def regex_memory_allowed(memory) -> bool:
    memory_id = str(getattr(memory, "memory_id", "") or "")
    return (memory_id in REGEX_DURABLE_IDS or str(getattr(memory, "memory_type", "")).upper() == "PERSONALITY"
            or memory_id.startswith(("episodic.", "short_term.")))


class MemoryIntentClassifier:
    """Create reliable profile and lifetime records before asking an LLM."""

    _NON_PERSISTENCE_REQUEST = re.compile(
        r"\b(?:(?:don['’]?t|do not|never)\s+(?:remember|save)|"
        r"keep\s+(?:this|that|it)\s+only\s+in\s+(?:this|the)\s+(?:chat|conversation))\b",
        flags=re.IGNORECASE,
    )

    GREETING_WORDS = {"hi", "hii", "hello", "hey", "heyy", "yo", "namaste"}
    FILLER_WORDS = {
        "a", "an", "and", "are", "bhai", "bhaiya", "bro", "dear", "hai",
        "i", "is", "ka", "ki", "ko", "main", "mera", "my", "naam", "name",
        "please", "sir", "the", "to", "tum", "yaar",
    }
    NAME_STOP_WORDS = {
        "a", "actually", "an", "and", "are", "called", "give", "hai", "i",
        "is", "mera", "my", "naam", "name", "not", "the", "to", "what", "you",
    }
    NON_NAME_STARTS = {
        "learning", "working", "trying", "preparing", "feeling", "looking",
        "going", "building", "making", "studying", "thinking",
    }
    GENERAL_KNOWLEDGE_PREFIXES = (
        "what is", "who is", "who was", "when was", "where is", "where was",
        "calculate", "solve", "tell me", "explain", "define", "how much is",
    )
    PERSONAL_ADVICE_WORDS = {
        "advice", "anxious", "breakup", "career", "family", "feel", "feeling",
        "friend", "life", "lonely", "personal", "relationship", "sad", "stress",
        "stressed", "should i", "what should i do",
    }
    DURABLE_LIFE_CUES = {
        "bought", "purchased", "upgraded", "joined", "started", "moved", "relocated",
        "graduated", "graduation", "married", "engaged", "adopted", "born", "died",
        "passed away", "got a job", "new job", "college", "university", "internship",
        "important phase", "phase of my life", "life chapter", "milestone", "promoted",
        "achievement", "first time", "birthday", "accident", "recovered",
        # Hinglish life cues
        "kharida", "kharid liya", "le liya", "naya liya",
        "shuru kiya", "join kiya", "join kar liya",
        "shift ho gaya", "ghar badla", "naukri mili", "naukri lagi",
        "shaadi", "sagai", "paida hua", "guzar gaye", "chal base",
        "pehli baar", "pahli baar",
    }
    DEVICE_WORDS = {"car", "laptop", "phone", "computer", "gpu", "pc", "bike", "motorcycle"}
    EVERYDAY_CUES = {
        "ate", "breakfast", "dinner", "joke", "lol", "lunch", "meme", "pizza",
        "played", "saw", "today", "watched", "weekend", "yesterday",
    }
    EMOTIONAL_MILESTONE_CUES = {
        "happiest day", "best day of my life", "proudest", "life changed",
        "heartbroken", "worst day", "deeply grateful", "so grateful",
        # Hinglish emotional milestones
        "sabse khush din", "sabse acha din", "zindagi ka sabse",
        "bahut bura din", "sabse bura din", "dil toot gaya", "dil tut gaya",
        "bahut proud", "maa ke liye", "papa ke liye",
        "life ka best moment", "aaj tak ka sabse",
        "bohot khush", "bahut khush", "itna khush",
        "sabse mushkil", "zindagi ka sabse mushkil",
    }
    RELATIONSHIP_SIGNALS = {
        "best ai", "best friend", "you're annoying", "you are annoying",
        "i'm sorry", "im sorry", "love talking to you", "trust you",
        # Hinglish relationship signals
        "tu best hai", "tum best ho", "sabse achi ai",
        "tujhse baat karke acha lagta", "tujhse baat karke accha lagta",
        "tum annoying ho", "bore kar rahi", "bore ho rahi",
        "sorry yaar", "maaf kar", "maaf karo",
        "tujhpe trust hai", "tujh par bharosa",
    }
    GOAL_CUES = {"goal", "learning", "learn", "preparing", "prepare", "working on", "trying to", "finished", "completed", "progress"}
    CHAPTER_CUES = {"school", "college", "university", "jee", "wbjee", "internship", "job", "career", "married", "marriage"}
    _LONG_TERM_DURATION = re.compile(
        r"\b(?:for\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"several|many)\s+years?|years|a\s+long\s+time)|since\s+(?:childhood|i\s+was\b[^,.!?]*)|"
        r"all\s+my\s+life|lifelong|over\s+the\s+years)\b",
        flags=re.IGNORECASE,
    )

    def classify(self, user_message: str, recent_conversation=None) -> Memory | None:
        message = " ".join(user_message.strip().split())
        if not message:
            return None
        lowered = message.lower()
        words = set(lowered.replace("!", " ").replace(",", " ").replace("?", " ").split())

        # An explicit request not to remember/save is a hard privacy boundary,
        # including when the text otherwise resembles a durable fact.
        if self._NON_PERSISTENCE_REQUEST.search(lowered):
            return None
        
        # ============================================
        # EARLY EXIT: Pure conversational messages that should NEVER be stored
        # ============================================
        
        # Greetings, reactions, and fillers are dialogue, not memory
        pure_dialogue_words = {
            "hi", "hello", "hey", "heyy", "hii", "yo", "namaste",
            "bye", "byee", "goodbye", "goodnight", "gn",
            "haha", "lol", "lmao", "hehe", "xd", "nice", "wow", "damn",
            "ok", "okay", "cool", "accha", "acha", "achha", "hmm", "hmmm",
            "thik", "theek", "sahi", "mast", "badiya",
            "haan", "han", "nahi", "na", "nhi",
            "are", "arre", "arey", "oho", "oh", "ohh",
        }
        if words and words.issubset(pure_dialogue_words | self.FILLER_WORDS):
            return None
            
        # Short messages that are just answering a conversational question
        # e.g., "mai thik hu", "raha hoon?", "bas thik hu"
        conversational_answers = {
            "mai thik", "main thik", "thik hu", "theek hu", "thik hoon",
            "bas thik", "sab thik", "mast hu", "accha hu", "achha hu",
            "i'm fine", "im fine", "i am fine", "i'm good", "im good",
            "nothing much", "kuch nahi", "bas wahi", "wahi sab",
            "tum batao", "aap batao", "tum sunao",
        }
        if any(phrase in lowered for phrase in conversational_answers) and len(words) <= 10:
            return None

        uncertain_relationship = self._extract_uncertain_relationship(message)
        if uncertain_relationship:
            return uncertain_relationship

        name = self._extract_name(message, recent_conversation)
        if name:
            return self._formal_name(name)

        contextual_person = self._extract_contextual_person_name(message, recent_conversation)
        if contextual_person:
            return contextual_person

        if self._is_hinglish_preference(lowered):
            return self._preference(
                memory_id="preference.language",
                topic="Preferred Language",
                summary="User prefers Jupiter to speak in natural Hinglish by default.",
                predicate="prefers_language",
                object_="Hinglish",
                attributes={"preferred_language": "Hinglish"},
            )

        if self._is_english_preference(lowered):
            return self._preference(
                memory_id="preference.language",
                topic="Preferred Language",
                summary="User prefers Jupiter to speak mainly in English.",
                predicate="prefers_language",
                object_="English",
                attributes={"preferred_language": "English"},
            )

        if self._rejects_bhaiya(lowered):
            return self._preference(
                memory_id="preference.address_style",
                topic="Address Style",
                summary="User does not want Jupiter to call them 'bhaiya'; use their name or a neutral friendly address.",
                predicate="dislikes_address",
                object_="bhaiya",
                attributes={"avoid_addresses": ["bhaiya", "bhai", "bro"], "address_style": "name_or_neutral"},
            )

        style = self._explicit_style_preference(lowered)
        if style:
            return style

        preference = self._extract_user_preference(message)
        if preference:
            return preference

        goal = self._extract_goal(message)
        if goal:
            return goal

        user_relation = self._extract_user_entity_relation(message)
        if user_relation:
            return user_relation

        social = self._extract_social_detail(message)
        if social:
            return social

        if self._is_emotional_milestone(lowered):
            return self._emotional_milestone(message)

        if self._is_relationship_signal(lowered):
            return self._relationship_signal(message)

        if self._mentions_important_relationship(lowered):
            return self._permanent_life_memory(
                message,
                category="Relationships",
                topic="Important Relationship Detail",
                domain="RELATIONSHIP",
                predicate="shared_relationship_detail",
            )

        if self._mentions_durable_life_event(lowered):
            domain = "DEVICE" if any(word in lowered for word in self.DEVICE_WORDS) else "GENERAL"
            category = "Devices" if domain == "DEVICE" else "Life Chapter"
            topic = "Important Purchase" if any(word in lowered for word in self.DEVICE_WORDS) else "Important Life Event"
            return self._permanent_life_memory(
                message,
                category=category,
                topic=topic,
                domain=domain if domain == "DEVICE" else "LIFE",
                predicate="shared_important_life_event",
            )

        long_term_interest = self._extract_long_term_interest(message, lowered)
        if long_term_interest:
            return long_term_interest

        if self._is_personal_advice(lowered):
            return self._temporary_memory(message, "EPISODIC", "Personal Conversation", "GENERAL")

        if self._is_general_question(lowered):
            return self._temporary_memory(message, "SHORT_TERM", "Question or Doubt", "SHORT_TERM")

        if self._is_ordinary_conversation(lowered):
            # If the user is just answering Jupiter's conversational question (e.g., "mai thik hu"),
            # do not store it as a long-term episodic memory. It's just dialogue state.
            if self._answered_question(lowered, recent_conversation):
                return None
            return self._temporary_memory(message, "EPISODIC", "Everyday Conversation", "EPISODIC")

        return None

    def _extract_uncertain_relationship(self, message):
        """Represent uncertain person claims as reviewable, not trusted facts."""

        relation_pattern = (
            r"best friend|friend|brother|sister|mother|father|mom|dad|sibling|"
            r"classmate|colleague|cousin|family member|partner|mentor|neighbou?r"
        )
        patterns = (
            rf"\b(?:maybe|perhaps|possibly|i think)\s+(?P<entity>[A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){{0,3}})\s+"
            rf"(?:is|(?:may|might|could|possibly)\s+be)\s+(?:my|mera|meri|mere)\s+"
            rf"(?P<relationship>{relation_pattern})\b",
            rf"\b(?:i['’]?m|i am)\s+(?:not sure|unsure|uncertain)\s+(?:whether|if)\s+"
            rf"(?P<entity>[A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){{0,3}})\s+"
            rf"is\s+(?:my|mera|meri|mere)\s+(?P<relationship>{relation_pattern})\b",
        )
        match = next(
            (found for pattern in patterns if (found := re.search(pattern, message, flags=re.IGNORECASE))),
            None,
        )
        if not match:
            return None
        name = self._clean_name(match.group("entity"))
        if not name:
            return None
        memory = self._person_relationship_memory(
            name=name,
            relationship=match.group("relationship"),
            message=message,
        )
        memory.summary = f"{name} may be the user's {memory.attributes['relationship']}."
        memory.confidence = 0.64
        memory.certainty = 0.64
        memory.attributes["uncertain"] = True
        memory.attributes["uncertainty_reason"] = "user_statement_uncertain"
        return memory

    def _extract_name(self, message, recent_conversation):
        patterns = (
            # New Hinglish patterns
            r"\bbula\s+mujhe\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bmujhe\s+([A-Za-z][A-Za-z .'-]{1,60})\s+(?:bolo|bulao|kaho)",
            # marker 219 (real WT 26 Oct): "Call me after just 10 sec" is a call request, not the name "After Just"
            r"\bcall\s+me\s+(?!(?:after|in|at|back|when|whenever|once|if|tomorrow|tonight|later|now|before|for|about|on|every|again|please|right|immediately|asap|soon|today|by|with|via|from|just|only|up|this|that|the|a|an|and|then|so|to|as|jupiter|daily|everyday|each|someday|sometime|no|not|maybe)\b)([A-Za-z][A-Za-z .'-]{1,60})",
            r"\b([A-Za-z][A-Za-z .'-]{1,60})\s+bolo\s+mujhe",
            # Existing patterns
            r"\bmy\s+name\s+is\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bmera\s+naam\s+(?:hai\s+)?([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bnaam\s+hai\s+mera\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\b(?:i\s+am|i'm)\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bmain\s+([A-Za-z][A-Za-z .'-]{1,60}?)(?:\s+(?:hu|hoon|hun))?(?:[?!,;:]|$)",
        )
        for pattern in patterns:
            match = re.search(pattern, message, flags=re.IGNORECASE)
            if match:
                candidate = self._clean_name(match.group(1))
                if candidate:
                    return candidate

        if self._name_was_requested(recent_conversation):
            candidate = self._clean_name(message)
            if candidate and len(candidate.split()) <= 4:
                return candidate
        return None

    def _clean_name(self, value):
        value = re.split(r"[?!,;:()]|\b(?:and|but|please|yaar)\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
        words = [word.strip(" .'\t") for word in value.split()]
        words = [word for word in words if word and word.lower() not in self.NAME_STOP_WORDS]
        while words and words[-1].lower() in {"hai", "hi", "hu", "hoon", "hun"}:
            words.pop()
        if not 1 <= len(words) <= 4:
            return None
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z'-]*", word) for word in words):
            return None
        candidate = " ".join(words)
        if candidate.split()[0].lower() in self.NON_NAME_STARTS:
            return None
        if candidate.lower() in self.NAME_STOP_WORDS or len(candidate) < 2:
            return None
        return " ".join(word.capitalize() if word.islower() else word for word in words)

    @staticmethod
    def _name_was_requested(recent_conversation):
        recent_assistant_text = " ".join(
            item.get("content", "").lower()
            for item in (recent_conversation or [])[-4:]
            if item.get("role") == "assistant"
        )
        return any(phrase in recent_assistant_text for phrase in ("what should i call", "your name", "naam"))

    @staticmethod
    def _answered_question(lowered, recent_conversation):
        """Check if the user is directly answering a conversational question."""
        if not recent_conversation:
            return False
        
        last_assistant = next(
            (msg.get("content", "") for msg in reversed(recent_conversation) if msg.get("role") == "assistant"), 
            ""
        )
        if "?" in last_assistant:
            # If the user is saying something short and conversational in response
            words = lowered.split()
            if len(words) <= 10:
                return True
        return False

    @staticmethod
    def _is_hinglish_preference(text):
        # Direct mentions of 'hinglish'
        if "hinglish" in text:
            return any(word in text for word in (
                "talk", "speak", "bolo", "bol", "reply", "baat", "samajh",
                "understand", "karo", "kar", "mein", "me", "in", "use",
                "can u", "can you", "please", "back to me",
            ))
        # Hindi+English mix requests without the word 'hinglish'
        hindi_english_mix = any(phrase in text for phrase in (
            "hindi english mix", "hindi-english", "roman hindi",
            "hindi mein baat", "hindi mein bol", "hindi me baat",
            "hindi me bol", "mixed language",
        ))
        return hindi_english_mix

    @staticmethod
    def _is_english_preference(text):
        if "english" not in text:
            return False
        return any(word in text for word in (
            "talk", "speak", "bolo", "bol", "reply", "baat",
            "mein", "me", "in", "only", "use", "switch",
            "karo", "kar", "please", "can you", "can u",
        ))

    @staticmethod
    def _rejects_bhaiya(text):
        has_address = any(word in text for word in ("bhaiya", "bhai", "bro", "sir"))
        has_rejection = any(phrase in text for phrase in (
            "mat bolo", "mat kaho", "mat bulao", "mat bol", "mat kah",
            "naa bolo", "na bolo", "nahi bolo", "nahi bol",
            "band karo", "band kar",
            "don't call", "do not call", "not call", "dont call",
            "don't say", "do not say", "dont say",
            "avoid", "stop calling", "stop saying",
            "nahi bulana", "mat bulana", "se mat bulao",
            "bolke nahi", "bolke mat", "bol ke nahi", "bol ke mat",
            "kehke nahi", "kehke mat", "keh ke nahi",
            "nahi chahiye", "pasand nahi",
        ))
        # Handle: "bhaiya nahi" / "bhai nahi" / "bro nahi" as standalone rejection
        standalone_rejection = bool(re.search(
            r'\b(?:bhaiya|bhai|bro|sir)\s+(?:nahi|nhi|no|nahin|mat)\b', text
        ))
        return (has_address and has_rejection) or standalone_rejection

    @staticmethod
    def _mentions_important_relationship(text):
        return bool(
            re.search(
                r"\b(?:my|mera|meri)\s+(?:best\s+)?(?:friend|brother|sister|mother|father|family)\b"
                r"\s+(?:is|was|named|ka\s+naam|hai)\b",
                text,
            )
        )

    def _mentions_durable_life_event(self, text):
        return any(cue in text for cue in self.DURABLE_LIFE_CUES) or (
            any(word in text for word in self.DEVICE_WORDS)
            and any(word in text for word in ("bought", "purchased", "new", "upgraded", "got"))
        )

    def _extract_long_term_interest(self, message, lowered):
        """Capture an explicit, long-lived hobby/interest as durable LIFE data."""

        if not self._LONG_TERM_DURATION.search(lowered):
            return None
        match = re.search(
            r"\b(?:i(?:['’]ve|\s+have)\s+(?:been\s+)?|i\s+am\s+|my\s+(?:long[- ]term\s+)?"
            r"(?:hobby|interest)\s+is\s+)"
            r"(?P<topic>[A-Za-z][A-Za-z0-9 &'#+/-]{2,100}?)\s+"
            r"(?:for\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"several|many)\s+years?|years|a\s+long\s+time)|"
            r"since\s+(?:childhood|i\s+was\s+[^,.!?]+)|all\s+my\s+life|lifelong|"
            r"over\s+the\s+years)\b",
            message,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        topic = " ".join(match.group("topic").split())
        topic = re.sub(
            r"(?i)^(?:been\s+|practicing\s+|practiced\s+|studying\s+|learning\s+|"
            r"playing\s+|played\s+|following\s+|followed\s+|doing\s+|"
            r"working\s+on\s+)+",
            "",
            topic,
        ).strip(" .,:;?!")
        if len(topic) < 3:
            return None
        digest = self._digest(f"long-term-interest:{topic}")
        return self._memory(
            memory_id=f"interest.long_term.{digest}",
            category="Interests",
            topic=topic,
            summary=f"User has a long-term interest in {topic}.",
            memory_type="SEMANTIC",
            domain="LIFE",
            importance="High",
            facts=[{"subject": "User", "predicate": "long_term_interest", "object": topic}],
            attributes={
                "interest": topic,
                "long_term": True,
                "original_statement": message,
            },
        )

    def _is_personal_advice(self, text):
        return "?" in text and any(cue in text for cue in self.PERSONAL_ADVICE_WORDS)

    def _is_general_question(self, text):
        compact = text.strip()
        return "?" in compact or compact.startswith(self.GENERAL_KNOWLEDGE_PREFIXES) or bool(re.fullmatch(r"[\d\s+\-*/().=]+", compact))

    def _is_ordinary_conversation(self, text):
        words = {word.strip(".,?!:;()[]{}\"'") for word in text.split()}
        if not words or words.issubset(self.GREETING_WORDS | self.FILLER_WORDS):
            return False
        return len(words) >= 4 or bool(words.intersection(self.EVERYDAY_CUES))

    def _explicit_style_preference(self, text):
        preferences = PersonalityStore.parse_explicit(text)
        if not preferences:
            return None
        return self._preference(
            memory_id="preference.communication_style",
            topic="Communication Style",
            summary="User stated communication preferences: " + ", ".join(
                f"{key.replace('_', ' ')}={value}" for key, value in preferences.items()
            ) + ".",
            predicate="prefers_communication_style",
            object_="; ".join(f"{key}={value}" for key, value in preferences.items()),
            attributes={"personality_preferences": preferences, "explicit": True},
        )

    def _extract_user_preference(self, message):
        match = re.search(
            r"\b(?:i\s+(?P<sentiment>like|love|dislike|hate)\s+|"
            r"my\s+(?:favourite|favorite)(?:\s+is)?\s+)"
            r"(?P<thing>[A-Za-z][A-Za-z0-9 .+#'-]{1,80})",
            message,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        sentiment = match.group("sentiment")
        thing = match.group("thing")
        thing = re.split(r"[?!,;:]|\b(?:because|but|and)\b", thing, maxsplit=1, flags=re.IGNORECASE)[0].strip(" .")
        if not thing or thing.lower() in {"it", "this", "that", "you"}:
            return None
        preference = "likes" if sentiment is None or sentiment.lower() in {"like", "love"} else "dislikes"
        slug = self._digest(f"{preference}:{thing}")
        return self._memory(
            memory_id=f"preference.{preference}.{slug}",
            category="Preferences",
            topic=thing,
            summary=f"User {preference} {thing}.",
            memory_type="SEMANTIC",
            domain="PREFERENCE",
            importance="Medium",
            facts=[{"subject": "User", "predicate": preference, "object": thing}],
            attributes={"preference": preference, "thing": thing, "explicit": True},
        )

    def _extract_goal(self, message):
        text = message.lower()
        if not any(cue in text for cue in self.GOAL_CUES):
            return None
        match = re.search(
            r"(?:learning|learn|preparing for|prepare for|working on|goal is|goal:|trying to|finished|completed)\s+([A-Za-z0-9 .+#-]{2,80})",
            message,
            flags=re.IGNORECASE,
        )
        name = self._clean_goal_name(match.group(1)) if match else "Personal Goal"
        progress_match = re.search(r"\b(\d{1,3})\s*%", message)
        progress = int(progress_match.group(1)) if progress_match else None
        progress_total = None
        progress_unit = None
        count_match = re.search(
            r"\b(\d{1,6})\s+(?:out\s+)?of\s+(\d{1,6})\s+"
            r"([A-Za-z][A-Za-z0-9 .+#-]{1,60}?)(?=[?!,;.]|$)",
            message,
            flags=re.IGNORECASE,
        )
        if count_match:
            progress = int(count_match.group(1))
            progress_total = int(count_match.group(2))
            progress_unit = self._clean_goal_name(count_match.group(3))
            name = progress_unit
        # Keep numeric progress separate from the durable goal subject.
        if progress is not None and (not name or re.fullmatch(r"\d+(?:\.\d+)?", name.strip())):
            subject = re.search(
                r"\b(?:of|in|on)\s+(?:my|the|a|an)?\s*"
                r"([A-Za-z][A-Za-z0-9 .+#-]{1,80}?)(?=[?!,;.]|$)",
                message,
                flags=re.IGNORECASE,
            )
            if subject:
                name = self._clean_goal_name(subject.group(1))
        status = "completed" if any(word in text for word in ("finished", "completed", "done with")) else "active"
        if progress_total is not None:
            status = "completed" if progress >= progress_total else "active"
        return self._memory(
            memory_id=f"goal.{self._digest(name)}",
            category="Goals",
            topic=name,
            summary=f"User {status if status == 'completed' else 'is working on'} the goal: {name}. {message}",
            memory_type="SEMANTIC",
            domain="GOAL",
            importance="High",
            facts=[{"subject": "User", "predicate": "goal_status", "object": f"{name}: {status}"}],
            attributes={
                "goal_name": name,
                "status": status,
                "progress": progress,
                **({"progress_total": progress_total, "progress_unit": progress_unit} if progress_total is not None else {}),
                "original_statement": message,
            },
        )

    @staticmethod
    def _clean_goal_name(value):
        cleaned = re.split(r"[?!,;:]|\b(?:and|but|because|today)\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
        cleaned = re.sub(r"\s+\d+\s*$", "", cleaned)
        return cleaned.strip(" .")[:80] or "Personal Goal"

    def _extract_social_detail(self, message):
        relations = r"best friend|friend|brother|sister|mother|father|mom|dad"
        match = re.search(
            rf"\b(?:my|mera|meri)\s+({relations})(?:'s)?\s+"
            rf"(?:name\s+is\s+|is\s+|ka\s+naam\s+|named\s+)"
            rf"([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){{0,3}})(?=[?!,;.]|$)",
            message,
            flags=re.IGNORECASE,
        )
        if not match:
            # Connector-less form, for example "My friend a friend loves anime".
            # Requiring title-cased name tokens prevents ordinary verbs such as
            # "wanna talk" from being mistaken for a person's name.
            match = re.search(
                rf"\b[Mm]y\s+({relations})\s+"
                rf"([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){{0,3}})\b",
                message,
            )
        if not match:
            return None
        relationship, name = match.groups()
        relationship = relationship.lower()
        name = self._clean_name(name)
        if not name:
            return None
        return self._person_relationship_memory(
            name=name,
            relationship=relationship,
            message=message,
        )

    def _extract_contextual_person_name(self, message, recent_conversation):
        """Resolve an explicit third-party name against a recent user relation.

        This intentionally requires both an explicit ``her/his/their name``
        statement and a recent user-authored ``my friend/...`` reference.  It
        therefore cannot turn Jupiter's own guesses into People I Know facts.
        """

        named = re.search(
            r"\b(?:her|his|their)\s+(?:full\s+)?name\s+is\s+"
            r"([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,3})(?=[?!,;.]|$)",
            message,
            flags=re.IGNORECASE,
        )
        if not named:
            return None
        base_name = self._clean_name(named.group(1))
        if not base_name:
            return None

        relationship = None
        relation_pattern = re.compile(
            r"\b(?:my|mera|meri)\s+(best friend|friend|brother|sister|mother|father|mom|dad)\b",
            flags=re.IGNORECASE,
        )
        for item in reversed((recent_conversation or [])[-8:]):
            if item.get("role") != "user":
                continue
            found = relation_pattern.search(str(item.get("content") or ""))
            if found:
                relationship = found.group(1).casefold()
                break
        if not relationship:
            return None

        # A correction/apposition after the first sentence may contain a fuller
        # version of the same name. Prefer the longest compatible proper-name
        # phrase while staying inside this user message.
        candidates = [base_name]
        base_first = base_name.split()[0].casefold()
        for candidate in re.findall(
            r"(?<![A-Za-z'-])([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){0,3})(?=[?!,;.]|$)",
            message,
        ):
            cleaned = self._clean_name(candidate)
            if cleaned and cleaned.split()[0].casefold() == base_first:
                candidates.append(cleaned)
        name = max(candidates, key=lambda value: (len(value.split()), len(value)))
        return self._person_relationship_memory(
            name=name,
            relationship=relationship,
            message=message,
        )

    def _person_relationship_memory(self, *, name, relationship, message):
        relationship = " ".join(str(relationship).casefold().split())
        name = " ".join(str(name).split())
        return self._memory(
            memory_id=(
                f"relationship.{relationship.replace(' ', '_')}."
                f"{self._digest(name.casefold())}"
            ),
            category="Relationships",
            topic=name,
            summary=f"{name} is the user's {relationship}.",
            memory_type="SEMANTIC",
            domain="RELATIONSHIP",
            importance="High" if "best" in relationship or relationship in {"mother", "father", "mom", "dad"} else "Medium",
            facts=[{"subject": name, "predicate": "relationship_to_user", "object": relationship}],
            attributes={
                "person_name": name,
                "entity": name,
                "relation_to_user": relationship,
                "relationship": relationship,
                "relationship_status": "current",
                "original_statement": message,
                "explicit": True,
            },
        )

    def _extract_user_entity_relation(self, message):
        """Preserve explicit user-to-entity relationships without entity hardcoding."""

        patterns = (
            r"(?P<entity>[^?!]{2,140})[?!]\s*(?:because\s+)?(?:that|it|yeh|ye)\s+"
            r"(?P<link>is|was|hai|tha|thi)\s+(?:my|mera|meri|mere)\s+"
            r"(?P<relation>[A-Za-z][A-Za-z -]{1,40}?)(?=[?!,;.]|$|\s*[^\x00-\x7f])",
            r"(?P<entity>[A-Za-z0-9][A-Za-z0-9 &'().+-]{1,120}?)\s+"
            r"(?P<link>is|was|hai|tha|thi)\s+(?:currently\s+)?(?:my|mera|meri|mere)\s+"
            r"(?P<relation>[A-Za-z][A-Za-z -]{1,40}?)(?=[?!,;.]|$)",
            r"(?:my|mera|meri|mere)\s+(?P<relation>[A-Za-z][A-Za-z -]{1,40}?)\s+"
            r"(?P<link>is|was|hai|tha|thi)\s+(?P<entity>[A-Za-z0-9][A-Za-z0-9 &'().+-]{1,120}?)(?=[?!,;.]|$)",
        )
        match = next((found for pattern in patterns if (found := re.search(pattern, message, re.IGNORECASE))), None)
        if not match:
            return None
        entity = " ".join(match.group("entity").split()).strip(" .,:;?!")
        relation = " ".join(match.group("relation").split()).strip(" .,:;?!").casefold()
        entity = re.sub(r"(?i)^(?:a|an|the)\s+", "", entity).strip()
        entity = re.sub(
            r"(?i)^(?:(?:show|find|check|search|read|open|latest|newest|last|any|the|my)\s+|"
            r"(?:email|mail|message|gmail|inbox|from|about|regarding|matching|mentioning)\s+)+",
            "",
            entity,
        ).strip(" .,:;?!")
        if not entity or not relation or entity.casefold() in {"it", "that", "this", "something", "anything"}:
            return None
        past = match.group("link").casefold() in {"was", "tha", "thi"} or bool(
            re.search(r"(?i)\b(?:former|previous|old|used\s+to)\b", message)
        )
        status = "past" if past else "current"
        education_relations = {"college", "school", "university", "institute", "academy"}
        work_relations = {"company", "employer", "office", "workplace"}
        device_relations = {"phone", "laptop", "computer", "car", "bike", "device"}
        if relation in education_relations:
            domain, category = "EDUCATION", "Education"
        elif relation in work_relations:
            domain, category = "WORK", "Work"
        elif relation in device_relations:
            domain, category = "DEVICE", "Devices"
        else:
            domain, category = "RELATIONSHIP", "Relationships"
        memory_id = (
            f"relationship.{relation.replace(' ', '_')}.{self._digest(entity.casefold())}"
            if domain == "RELATIONSHIP"
            else f"relationship.user.{self._digest(f'{relation}:{entity}')}"
        )
        memory = self._memory(
            memory_id=memory_id,
            category=category,
            topic=entity if domain == "RELATIONSHIP" else relation.title(),
            summary=f"{entity} is the user's {status} {relation}.",
            memory_type="SEMANTIC",
            domain=domain,
            importance="High",
            facts=[{"subject": entity, "predicate": "relationship_to_user", "object": relation}],
            attributes={
                "entity": entity,
                **({"person_name": entity} if domain == "RELATIONSHIP" else {}),
                "relation_to_user": relation,
                "relationship": relation,
                "relationship_status": status,
                "original_statement": message,
                "explicit": True,
            },
        )
        memory.entities = [entity]
        return memory

    def _is_emotional_milestone(self, text):
        return any(cue in text for cue in self.EMOTIONAL_MILESTONE_CUES)

    def _emotional_milestone(self, message):
        return self._memory(
            memory_id=f"emotion.milestone.{self._digest(message)}",
            category="Emotional Milestone",
            topic="Important Emotional Moment",
            summary=f"User shared an important emotional moment: {message}",
            memory_type="SEMANTIC",
            domain="EMOTION",
            importance="High",
            facts=[{"subject": "User", "predicate": "important_emotional_moment", "object": message}],
            attributes={"original_statement": message, "emotional_milestone": True},
        )

    def _is_relationship_signal(self, text):
        return any(cue in text for cue in self.RELATIONSHIP_SIGNALS)

    def _is_correction(self, text):
        """Detect when user is correcting Jupiter in Hinglish."""
        return any(phrase in text for phrase in (
            "aisa mat bolo", "aisa mat bol", "aisa nahi",
            "yeh galat hai", "ye galat hai", "galat bol",
            "wrong bol rahi", "wrong bol raha",
            "nahi nahi, actually", "nahi nahi actually",
            "arre nahi", "arey nahi",
            "sahi nahi hai", "theek nahi hai",
        ))

    def _relationship_signal(self, message):
        return self._memory(
            memory_id=f"relationship.user_jupiter.{self._digest(message)}",
            category="Relationship History",
            topic="User and Jupiter",
            summary=f"Relationship signal from the user: {message}",
            memory_type="EPISODIC",
            domain="RELATIONSHIP",
            importance="Medium",
            facts=[{"subject": "User", "predicate": "relationship_signal", "object": message}],
            attributes={"relationship_signal": message},
        )

    def _formal_name(self, name):
        preferred_name = name.split()[0]
        return self._memory(
            memory_id="identity.name",
            category="Identity",
            topic="Formal Name",
            summary=f"User's formal name is {name}; Jupiter should call them {preferred_name} unless they state another preference.",
            memory_type="SEMANTIC",
            domain="PROFILE",
            importance="Critical",
            facts=[
                {"subject": "User", "predicate": "formal_name", "object": name},
                {"subject": "User", "predicate": "preferred_name", "object": preferred_name},
            ],
            attributes={"name": name, "formal_name": name, "preferred_name": preferred_name},
        )

    def _preference(self, memory_id, topic, summary, predicate, object_, attributes):
        return self._memory(
            memory_id=memory_id,
            category="Preferences",
            topic=topic,
            summary=summary,
            memory_type="PERSONALITY",
            domain="PREFERENCE",
            importance="High",
            facts=[{"subject": "User", "predicate": predicate, "object": object_}],
            attributes=attributes,
        )

    def _permanent_life_memory(self, message, category, topic, domain, predicate):
        digest = self._digest(message)
        return self._memory(
            memory_id=f"{category.lower().replace(' ', '_')}.{digest}",
            category=category,
            topic=topic,
            summary=f"User shared an important long-term detail: {message}",
            memory_type="SEMANTIC",
            domain=domain,
            importance="High",
            facts=[{"subject": "User", "predicate": predicate, "object": message}],
            attributes={"original_statement": message, "durable": True},
        )

    def _temporary_memory(self, message, memory_type, category, domain):
        digest = self._digest(message)
        label = "ordinary conversation" if memory_type == "EPISODIC" else "question or doubt"
        return self._memory(
            memory_id=f"{memory_type.lower()}.{digest}",
            category=category,
            topic=message[:80],
            summary=f"User had a {label}: {message}",
            memory_type=memory_type,
            domain=domain,
            importance="Low",
            facts=[{"subject": "User", "predicate": "discussed", "object": message[:300]}],
            attributes={"original_statement": message},
        )

    def _memory(self, memory_id, category, topic, summary, memory_type, domain, importance, facts, attributes):
        created_at = datetime.now()
        attributes = {
            **dict(attributes or {}),
            "provenance_type": "user_statement",
        }
        return Memory(
            memory_id=memory_id,
            category=category,
            topic=topic,
            summary=summary,
            memory_type=memory_type,
            domain=domain,
            facts=facts,
            attributes=attributes,
            importance=importance,
            retention=retention_label(memory_type),
            visibility="PERSONAL",
            certainty=1.0,
            confidence=1.0,
            source="conversation",
            expires_at=expiry_for(memory_type, created_at),
            created_at=created_at.isoformat(),
            last_updated=created_at.isoformat(),
            last_accessed=created_at.isoformat(),
        )

    @staticmethod
    def _digest(message):
        return hashlib.sha1(message.lower().encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _normalize_hinglish(text: str) -> str:
        """Strip common Hinglish particles to expose core intent."""
        # Remove common particles that don't carry intent
        particles = {
            'na', 're', 'yaar', 'bhai', 'bhaiya', 'bro', 'hai', 'kya',
            'toh', 'to', 'bhi', 'hi', 'ji', 'arre', 'arey', 'acha',
            'haan', 'han', 'naa', 'please', 'plz', 'pls',
        }
        words = text.lower().split()
        cleaned = [w for w in words if w.strip('.,?!') not in particles]
        return ' '.join(cleaned)

    @staticmethod
    def split_clauses(text: str) -> list[str]:
        """Split a single message into multiple intents (e.g., 'mai thik hu tum batao')."""
        import re
        # Split on punctuation or explicit Hinglish conjunctions
        pattern = r'(?<=[?!.,;])\s+|\s+(?:aur|par|lekin|to|waise)\s+'
        clauses = [c.strip() for c in re.split(pattern, text) if c.strip()]
        # Fallback for implicit splits where no punctuation is used, e.g., "mai to thik hu tum batao"
        # We look for pronoun + question word combinations after an assertion
        if len(clauses) == 1:
            if " tum " in text.lower() or " aap " in text.lower() or " kya " in text.lower():
                # Attempt to split if it looks like two separate thoughts
                parts = re.split(r'\s+(?=tum batao|aap batao|aur tum|aur aap|kya)', text, flags=re.IGNORECASE)
                clauses = [p.strip() for p in parts if p.strip()]
        return clauses
