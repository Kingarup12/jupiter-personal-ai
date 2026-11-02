"""Lightweight per-turn topic awareness without another model request."""


class TopicTracker:
    SWITCH_MARKERS = ("anyway", "by the way", "changing subject", "new topic", "also,")
    STOP_WORDS = {
        "about", "after", "again", "been", "can", "could", "does", "from", "have",
        "help", "here", "into", "just", "like", "make", "need", "please", "should",
        "that", "the", "their", "there", "this", "what", "when", "which", "with",
        "would", "your", "you", "and", "for", "are", "was", "were", "how", "why",
    }

    def is_switch(self, message: str) -> bool:
        lowered = message.strip().lower()
        return any(lowered.startswith(marker) for marker in self.SWITCH_MARKERS)

    def extract(self, message: str) -> str | None:
        words = []
        for token in message.lower().replace("'", " ").split():
            token = token.strip(".,?!:;()[]{}\"-")
            if len(token) >= 3 and token not in self.STOP_WORDS and token.isalpha():
                words.append(token)
        return " ".join(words[:3]) or None

    def active_topic(self, user_message: str, conversation_messages: list[dict]) -> str | None:
        """Return the current topic or a useful recent fallback for short replies."""

        current = self.extract(user_message)
        if current:
            return current
        if self.is_switch(user_message):
            return None

        for message in reversed(conversation_messages[:-1]):
            if message.get("role") == "user":
                previous = self.extract(message.get("content", ""))
                if previous:
                    return previous
        return None
