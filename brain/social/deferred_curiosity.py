"""A tiny queue for delayed, optional companion follow-ups.

It gives Jupiter a chance to remember an interesting shared detail without
interrupting the user's current message or behaving like a data-collection bot.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from memory.profile import UserProfileStore


class DeferredCuriosity:
    STATE_KEY = "deferred_curiosity"
    DELAY = timedelta(minutes=25)
    MAX_PENDING = 3

    def __init__(self, user_id=None):
        self.profile = UserProfileStore(user_id=user_id)

    def schedule(self, manager, user_message: str):
        match = re.search(
            r"\b(?:bought|got|started|made|finished)\s+(?:a\s+|an\s+|my\s+)?"
            r"([A-Za-z][A-Za-z0-9 .+#'-]{1,60})",
            user_message,
            flags=re.IGNORECASE,
        )
        if not match:
            return False
        topic = re.split(r"[?!,;:]|\b(?:and|but|because|today)\b", match.group(1), maxsplit=1, flags=re.IGNORECASE)[0].strip(" .")
        if not topic:
            return False
        pending = manager.get_state(self.STATE_KEY, [])
        if not isinstance(pending, list):
            pending = []
        if any(item.get("topic", "").lower() == topic.lower() for item in pending if isinstance(item, dict)):
            return False
        now = datetime.now()
        pending.append(
            {
                "topic": topic,
                "created_at": now.isoformat(),
                "due_at": (now + self.DELAY).isoformat(),
            }
        )
        manager.set_state(self.STATE_KEY, pending[-self.MAX_PENDING:])
        return True

    def schedule_cross_session(self, user_message: str):
        """Queue interesting topics for warm follow-up in the next session."""

        match = re.search(
            r"\b(?:bought|got|started|made|finished|joined|learning)\s+(?:a\s+|an\s+|my\s+)?"
            r"([A-Za-z][A-Za-z0-9 .+#'-]{1,60})",
            user_message,
            flags=re.IGNORECASE,
        )
        if not match:
            return False
        topic = re.split(
            r"[?!,;:]|\b(?:and|but|because|today)\b",
            match.group(1), maxsplit=1, flags=re.IGNORECASE,
        )[0].strip(" .")
        if not topic or len(topic) < 3:
            return False
        self.profile.add_curiosity_item(topic)
        return True

    def cross_session_instruction(self):
        """Return a warm follow-up cue from a previous session, if available."""

        topic = self.profile.pop_curiosity_item()
        if not topic:
            return ""
        return (
            f"Cross-session continuity: the user mentioned '{topic}' in a previous "
            "session. If the greeting feels natural for it, ask one warm follow-up "
            "about it. If it would feel abrupt, skip it entirely."
        )

    def due_instruction(self, manager, user_message: str, conversation_messages: list[dict]):
        """Return one safe optional cue, removing it only when it is eligible."""

        if self._should_not_interrupt(user_message, conversation_messages):
            return ""
        pending = manager.get_state(self.STATE_KEY, [])
        if not isinstance(pending, list):
            return ""
        now = datetime.now()
        eligible, remaining = [], []
        for item in pending:
            try:
                due_at = datetime.fromisoformat(item["due_at"])
            except (KeyError, TypeError, ValueError):
                continue
            (eligible if due_at <= now else remaining).append(item)
        if not eligible:
            return ""
        item = eligible[0]
        manager.set_state(self.STATE_KEY, remaining)
        return (
            f"Optional delayed curiosity: the user earlier mentioned {item['topic']}. "
            "Only after responding to their current message, you may ask one light, "
            "specific follow-up if it fits naturally. Skip it if the topic would feel abrupt."
        )

    @staticmethod
    def _should_not_interrupt(message, conversation_messages):
        text = message.lower()
        if "?" in message or any(word in text for word in ("sad", "stressed", "angry", "upset", "help", "urgent")):
            return True
        recent_assistant = [
            item.get("content", "")
            for item in conversation_messages[-4:]
            if item.get("role") == "assistant"
        ]
        return any("?" in reply for reply in recent_assistant)
