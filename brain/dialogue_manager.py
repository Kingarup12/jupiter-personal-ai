"""Manages conversational state such as pending questions and user satisfaction."""

import re

class DialogueManager:
    """Tracks the state of the conversation to avoid looping and ignoring questions."""
    
    def __init__(self):
        self.pending_user_question = False
        self.pending_assistant_question = False
        self.last_user_question = None
        self.last_assistant_question = None
        self.user_confusion_count = 0
        self.current_goal = "chat"
        self.user_answered_question = False
        
    def observe_user_message(self, message: str):
        """Update state based on what the user just said."""
        
        lowered = message.lower()
        
        # Check if user is confused
        confusion_phrases = {
            "samajh nahi aa raha", "samajh nhi aa raha", "samajh nahi aaya",
            "what do you mean", "kya bol", "confusing", "not making sense",
            "what are you trying to say"
        }
        if any(phrase in lowered for phrase in confusion_phrases):
            self.user_confusion_count += 1
        else:
            self.user_confusion_count = max(0, self.user_confusion_count - 1)
            
        # Check if user asked a question
        if "?" in message or any(lowered.startswith(q) for q in ("kya", "kyu", "kaise", "kab", "kaha", "what", "how", "why", "when", "where")):
            self.pending_user_question = True
            self.last_user_question = message
        else:
            self.pending_user_question = False
            self.last_user_question = None
            
        # If assistant had asked a question, user likely just answered it
        self.user_answered_question = False
        if self.pending_assistant_question:
            self.user_answered_question = True
            self.pending_assistant_question = False

    def observe_assistant_message(self, message: str):
        """Update state based on what Jupiter replied."""
        
        # Did Jupiter ask a question?
        if "?" in message:
            self.pending_assistant_question = True
            
            # Extract just the question part
            sentences = [s.strip() for s in re.split(r'[?!.]', message) if s.strip()]
            for s in reversed(sentences):
                if s and ("?" in message or s.lower().startswith(("kya", "how", "what", "why"))):
                    self.last_assistant_question = s + "?"
                    break
        else:
            self.pending_assistant_question = False
            self.last_assistant_question = None
            
        # Jupiter answered the user's question
        if self.pending_user_question:
            self.pending_user_question = False

    def get_instruction(self) -> str:
        """Return system prompt instructions based on dialogue state."""
        instructions = []
        
        if self.user_confusion_count > 0:
            instructions.append(
                "Social goal: The user seems confused. Stop joking, simplify your language, "
                "and explain clearly what you meant in the previous turn. Do not ask new questions."
            )
            
        if self.user_answered_question:
            instructions.append(
                "Conversation State: The user just answered your previous question. Acknowledge it naturally."
            )
            
        return "\n".join(instructions)
