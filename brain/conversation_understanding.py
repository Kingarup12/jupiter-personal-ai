"""Conversation Understanding for Jupiter.

Parses user messages to understand their conversational INTENT,
not just their semantic content. This is the difference between
'processing a sentence' and 'understanding a dialogue'.
"""

import re
from brain.conversation import prior_turns


class DialogueAct:
    """Represents what the user is actually doing in conversation."""

    GREETING = "greeting"
    FAREWELL = "farewell"
    ANSWER = "answer"              # User answered Jupiter's question
    QUESTION = "question"          # User asked a question
    CORRECTION = "correction"      # User corrected Jupiter
    SHARING = "sharing"            # User sharing about their day/life
    REACTION = "reaction"          # User reacting to something Jupiter said
    COMMAND = "command"            # User wants Jupiter to do something
    CONTINUATION = "continuation"  # User continuing a topic
    PLAYFUL = "playful"            # User being playful/teasing


class ConversationUnderstanding:
    """Parse what the user actually means in conversational context."""

    GREETING_WORDS = {"hi", "hello", "hey", "heyy", "hii", "yo", "namaste", "kaise ho"}
    FAREWELL_WORDS = {"bye", "byee", "goodbye", "goodnight", "gn", "exit", "quit", "chal bye"}
    
    CORRECTION_PHRASES = {
        # English
        "no no", "that's wrong", "thats wrong", "not like that",
        "i didn't say", "i didnt say", "you misunderstood",
        "i meant", "what i meant", "actually i",
        # Hinglish
        "are nahi", "arre nahi", "arey nahi", "nahi nahi",
        "galat", "ye galat", "yeh galat", "sahi nahi",
        "aisa nahi", "aisa mat", "wrong bol",
        "sahi pakda",  # User pointing out Jupiter's mistake
    }
    
    REACTION_PHRASES = {
        "haha", "lol", "lmao", "rofl", "hehe", "xd",
        "nice", "wow", "damn", "omg", "oh",
        "are baap re", "arre baap", "oho", "wah",
        "sahi hai", "mast", "badiya", "zabardast",
        "kya baat", "kya baat hai",
    }
    
    PLAYFUL_PHRASES = {
        "pagal", "pagli", "kya kar rahi", "hatt",
        "chal chal", "jhooth", "jhuth", "mazak",
        "tu bhi na", "tum bhi na",
    }
    
    SHARING_CUES = {
        "maine", "aaj", "kal", "abhi", "today", "yesterday",
        "i was", "i did", "i went", "i played", "i ate",
        "khela", "khaya", "gaya", "gayi", "dekha",
        "kar raha", "kar rahi", "ker raha",
    }

    def understand(self, user_message: str, conversation_history: list[dict]) -> dict:
        """Parse the user's message in conversational context.
        
        Returns a dict with:
            act: The primary dialogue act
            sub_acts: List of secondary acts (e.g., answer + question)
            is_answering: Whether user is answering Jupiter's question
            is_asking_back: Whether user is also asking Jupiter something
            emotion: Detected conversational emotion
            should_store_memory: Whether this message should be stored
        """
        lowered = user_message.lower().strip()
        words = set(lowered.replace("!", " ").replace(",", " ").replace("?", " ").split())
        
        acts = []
        is_answering = False
        is_asking_back = False
        is_teaching = False
        is_translation = False
        emotion = "neutral"
        should_store = True
        
        # Get last assistant message
        last_assistant = self._get_last_assistant(conversation_history)
        assistant_asked_question = last_assistant is not None and "?" in last_assistant
        
        # 1. Check if this is a greeting
        if words.intersection(self.GREETING_WORDS) and len(words) <= 5:
            acts.append(DialogueAct.CONTINUATION if prior_turns(user_message, conversation_history)
                        or any(item.get('_p4_compaction') for item in conversation_history)
                        else DialogueAct.GREETING)
            should_store = False
            
        # 2. Check if this is a farewell
        elif words.intersection(self.FAREWELL_WORDS) and len(words) <= 4:
            acts.append(DialogueAct.FAREWELL)
            should_store = False
            
        # 3. Check if user is correcting Jupiter
        elif any(phrase in lowered for phrase in self.CORRECTION_PHRASES):
            acts.append(DialogueAct.CORRECTION)
            emotion = "corrective"
            should_store = False  # Corrections are dialogue, not memory
            
        # 4. Check if user is reacting
        elif any(phrase in lowered for phrase in self.REACTION_PHRASES) and len(words) <= 6:
            acts.append(DialogueAct.REACTION)
            emotion = self._detect_reaction_emotion(lowered)
            should_store = False  # Reactions are dialogue, not memory
            
        # 5. Check if user is being playful
        elif any(phrase in lowered for phrase in self.PLAYFUL_PHRASES):
            acts.append(DialogueAct.PLAYFUL)
            emotion = "playful"
            should_store = False
            
        # 6. Check if user is sharing
        elif any(cue in lowered for cue in self.SHARING_CUES):
            acts.append(DialogueAct.SHARING)
            emotion = self._detect_sharing_emotion(lowered)
            
        # 7. Continuous interpreter intent is distinct from a teaching request.
        elif re.search(
            r"(?i)\b(?:translate|interpret)\s+(?:everything|anything|all)\s+(?:i|we)\s+(?:say|speak)\b",
            lowered,
        ):
            acts.append(DialogueAct.COMMAND)
            is_translation = True

        # 8. Check if user is asking for language teaching
        elif any(phrase in lowered for phrase in ["teach me", "how to say", "how do you say", "translate", "what is the meaning", "pronounce", "pronunciation", "correct my"]):
            acts.append(DialogueAct.QUESTION)
            is_teaching = True
            
        # 8. Check if user asked a question
        elif "?" in user_message or self._starts_with_question_word(lowered):
            acts.append(DialogueAct.QUESTION)
            
        else:
            acts.append(DialogueAct.CONTINUATION)
        
        # Cross-cutting: Is user answering Jupiter's question?
        if assistant_asked_question:
            is_answering = True
            if len(words) <= 10 and not any(phrase in lowered for phrase in self.CORRECTION_PHRASES):
                should_store = False  # Short answers to questions are dialogue, not memory
        
        # Cross-cutting: Is user also asking Jupiter something?
        # "mai thik hu tum batao" = answered + asked back
        ask_back_patterns = [
            r"\btum\s+batao\b", r"\baap\s+batao\b", r"\btum\s+sunao\b",
            r"\baur\s+tum\b", r"\baur\s+aap\b", r"\band\s+you\b",
            r"\bwhat\s+about\s+you\b", r"\btum\s+kaise\b", r"\baap\s+kaise\b",
            r"\btumhara\s+kya\b", r"\baapka\s+kya\b",
        ]
        if any(re.search(p, lowered) for p in ask_back_patterns):
            is_asking_back = True
            if DialogueAct.ANSWER not in acts:
                acts.append(DialogueAct.ANSWER)
        
        primary_act = acts[0] if acts else DialogueAct.CONTINUATION
        
        return {
            "importance": self.conversation_importance(user_message),
            "act": primary_act,
            "sub_acts": acts,
            "is_answering": is_answering,
            "is_asking_back": is_asking_back,
            "is_teaching": is_teaching,
            "is_translation": is_translation,
            "emotion": emotion,
            "should_store_memory": should_store,
        }
    
    @staticmethod
    def conversation_importance(text: str) -> str:
        """Explicit non-sensitive interaction cues, not diagnoses or emotion guesses."""
        text = str(text).casefold()
        if re.search(r"\b(?:do not interrupt|don't interrupt|urgent emergency|critical issue)\b", text):
            return "CRITICAL"
        if re.search(r"\b(?:important|deadline|exam|interview|focus|debug|decision)\b", text):
            return "IMPORTANT"
        if re.search(r"\b(?:just chatting|casual|joke|hello|hi)\b", text):
            return "CASUAL"
        return "NORMAL"

    @staticmethod
    def _get_last_assistant(conversation_history: list[dict]) -> str | None:
        for msg in reversed(conversation_history):
            if msg.get("role") == "assistant":
                return msg.get("content", "")
        return None
    
    @staticmethod
    def _starts_with_question_word(text: str) -> bool:
        question_starters = (
            "kya ", "kyu ", "kyun ", "kaise ", "kab ", "kaha ", "kidhar ",
            "what ", "how ", "why ", "when ", "where ", "who ", "which ",
            "kis ", "kaun ", "kitna ", "kitni ", "kitne ",
        )
        return any(text.startswith(q) for q in question_starters)
    
    @staticmethod
    def _detect_reaction_emotion(text: str) -> str:
        if any(w in text for w in ("haha", "lol", "lmao", "hehe", "rofl", "xd")):
            return "amused"
        if any(w in text for w in ("wow", "damn", "omg", "baap re", "oho")):
            return "surprised"
        if any(w in text for w in ("nice", "sahi", "mast", "badiya", "zabardast")):
            return "appreciative"
        return "neutral"
    
    @staticmethod
    def _detect_sharing_emotion(text: str) -> str:
        if any(w in text for w in ("sad", "dukhi", "bura", "worst")):
            return "sad"
        if any(w in text for w in ("happy", "khush", "mast", "amazing")):
            return "happy"
        if any(w in text for w in ("stressed", "tension", "pareshan")):
            return "stressed"
        return "neutral"
    
    def get_dialogue_instruction(self, understanding: dict) -> str:
        """Generate a system prompt instruction from the understanding."""
        parts = []
        
        act = understanding["act"]
        
        if understanding["is_answering"] and understanding["is_asking_back"]:
            parts.append(
                "DIALOGUE STATE: The user ANSWERED your question AND asked you something back. "
                "You MUST: 1) Briefly acknowledge their answer, 2) Answer their question naturally. "
                "Do NOT ask another question immediately. Do NOT ignore their question."
            )
        elif understanding["is_answering"]:
            parts.append(
                "DIALOGUE STATE: The user is answering your previous question. "
                "Acknowledge their answer naturally before moving on. Do NOT re-ask the same question."
            )
            
        if act == DialogueAct.CORRECTION:
            parts.append(
                "DIALOGUE STATE: The user is CORRECTING you. You made a mistake. "
                "Acknowledge the mistake naturally: 'Oops 😅 sahi pakda!' or 'Haha sahi bola!' "
                "Then correct yourself. Do NOT get defensive or ignore the correction."
            )
            
        if act == DialogueAct.REACTION:
            emotion = understanding["emotion"]
            if emotion == "amused":
                parts.append(
                    "DIALOGUE STATE: The user found something funny. Match their energy with "
                    "light humor. Keep it brief. Maybe add a playful follow-up."
                )
            elif emotion == "surprised":
                parts.append(
                    "DIALOGUE STATE: The user is surprised or impressed. Acknowledge their "
                    "reaction naturally."
                )
                
        if act == DialogueAct.PLAYFUL:
            parts.append(
                "DIALOGUE STATE: The user is being playful/teasing. Play along! "
                "Tease back lightly. Keep the energy fun. Don't get serious."
            )
            
        if act == DialogueAct.SHARING:
            parts.append(
                "DIALOGUE STATE: The user is sharing about their day/life. "
                "React naturally like a friend would. Ask ONE specific follow-up question "
                "about what they shared, not a generic 'kaisa laga?'"
            )
        
        return "\n".join(parts)
