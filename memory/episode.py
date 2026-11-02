"""Compact episodic replay: store one session record, not every utterance."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class EpisodicState:
    active_topic: str | None = None
    user_emotion: str = "neutral"


class EpisodicReplay:
    def summarize(self, messages, state):
        user_messages = [
            message.get("content", "").strip()
            for message in messages
            if message.get("role") == "user" and message.get("content", "").strip()
        ]
        if not user_messages:
            return None
        highlights = " | ".join(user_messages[-3:])
        topic = state.active_topic or "general conversation"
        return {
            "summary": f"Session about {topic}: {highlights[:700]}",
            "topic": topic,
            "emotion": state.user_emotion,
            "started_at": messages[0].get("timestamp", datetime.now().isoformat()),
            "ended_at": datetime.now().isoformat(),
        }
