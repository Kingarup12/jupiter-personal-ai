"""Dialogue State Tracker for Jupiter.

Tracks what has been discussed, what questions have been asked,
and what topics have been covered in the current session.
Prevents Jupiter from asking the same question twice.
"""

from dataclasses import dataclass, field
from datetime import datetime
import time


@dataclass
class DialogueState:
    """Complete state of the current conversation session."""
    
    # Questions Jupiter has already asked (to avoid repeating)
    asked_questions: list[str] = field(default_factory=list)
    importance: str = "NORMAL"
    last_activity: float = 0.0
    mid_answer: bool = False
    topic_boundary: bool = False
    
    # Topics discussed this session
    discussed_topics: set[str] = field(default_factory=set)
    
    # Whether Jupiter has introduced herself
    has_introduced: bool = False
    
    # Whether user's name is known
    user_name_known: bool = False
    
    # Current conversation energy level
    energy: str = "normal"  # low, normal, high, playful
    
    # Turn count since last question asked by Jupiter
    turns_since_question: int = 0
    
    # The last thing user was talking about
    last_user_topic: str | None = None
    
    # Session start time
    session_start: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # Consecutive turns on same topic
    topic_depth: int = 0
    
    # Whether user seems bored (short responses, changing topics)
    boredom_signals: int = 0


class DialogueStateTracker:
    """Track and update dialogue state across the conversation."""
    
    def __init__(self):
        self.state = DialogueState()
    
    def update_on_user_message(self, user_message: str, understanding: dict):
        """Update state based on user message and its understanding."""
        lowered = user_message.lower().strip()
        words = lowered.split()
        self.state.importance = understanding.get("importance", "NORMAL")
        self.state.last_activity = time.monotonic()
        self.state.mid_answer = True
        self.state.topic_boundary = understanding.get("act") in {"farewell", "greeting"}
        
        # Track energy from user message length and style
        if len(words) <= 3:
            self.state.boredom_signals += 1
        else:
            self.state.boredom_signals = max(0, self.state.boredom_signals - 1)
        
        # Track topic depth
        if understanding.get("act") == "continuation":
            self.state.topic_depth += 1
        else:
            self.state.topic_depth = 0
            
        # Detect if user seems bored (20+ minutes of tech discussion)
        if self.state.topic_depth > 8:
            self.state.boredom_signals += 1
    
    def update_on_assistant_message(self, assistant_message: str):
        """Track what Jupiter has said."""
        self.state.mid_answer = False
        self.state.last_activity = time.monotonic()
        if "?" in assistant_message:
            self.state.asked_questions.append(assistant_message)
            self.state.turns_since_question = 0
        else:
            self.state.turns_since_question += 1
        
        # Mark introduction
        if any(phrase in assistant_message.lower() for phrase in ("main jupiter", "i am jupiter", "jupiter hoon")):
            self.state.has_introduced = True
    
    def should_ask_question(self) -> bool:
        """Whether Jupiter should ask a follow-up question."""
        # Don't ask too many questions in a row
        if self.state.turns_since_question < 2:
            return False
        return True
    
    def has_already_asked(self, question_topic: str) -> bool:
        """Check if Jupiter has already asked about this topic."""
        topic_lower = question_topic.lower()
        for q in self.state.asked_questions:
            if topic_lower in q.lower():
                return True
        return False
    
    def get_instruction(self) -> str:
        """Generate system instructions from current state."""
        instructions = []
        
        if self.state.boredom_signals >= 3:
            instructions.append(
                "BOREDOM DETECTED: The user's responses are getting shorter. "
                "Consider changing the topic naturally or making a playful observation."
            )
        
        if self.state.topic_depth > 6:
            instructions.append(
                "TOPIC FATIGUE: You've been on the same topic for many turns. "
                "If the user seems disengaged, shift naturally."
            )
        
        if not self.should_ask_question():
            instructions.append(
                "QUESTION COOLDOWN: You asked a question recently. "
                "Do NOT ask another question this turn. Just respond naturally."
            )
        
        return "\n".join(instructions)
    
