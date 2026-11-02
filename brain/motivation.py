"""Slow, user-centred companion motivations and multi-session follow-ups."""

from datetime import datetime, timedelta


class MotivationEngine:
    """Keep a tiny queue of useful social goals without interrogating the user."""

    REQUIRED_GOALS = (
        ("preferred name", "PROFILE", "name"),
        ("current project", "PROJECT", ""),
        ("meaningful goal or dream", "GOAL", ""),
        ("communication preferences", "PREFERENCE", ""),
    )
    FOLLOW_UP_INTERVAL = timedelta(days=7)

    def pending_goals(self, memories: list) -> list[str]:
        corpus = " ".join(
            f"{memory.domain} {memory.topic} {memory.summary}".lower()
            for memory in memories
        )
        pending = []
        for label, domain, keyword in self.REQUIRED_GOALS:
            if domain.lower() not in corpus and (not keyword or keyword not in corpus):
                pending.append(label)
        return pending

    def _follow_up_candidate(self, memories: list):
        candidates = [
            memory
            for memory in memories
            if memory.domain in {"GOAL", "PROJECT", "EDUCATION"}
            and memory.visibility != "SECRET"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda memory: memory.last_updated or memory.created_at)
        return candidates[0]

    def instruction(self, executive_action: str, memories: list, state_store) -> str:
        """Offer one optional check-in only after a deliberate cooldown.

        Jupiter cannot initiate terminal conversations by itself.  This makes a
        multi-session follow-up available on a later natural greeting instead.
        """

        pending = self.pending_goals(memories)
        state_store.set_state("motivation.pending_goals", pending)
        if executive_action != "GREET":
            return ""

        candidate = self._follow_up_candidate(memories)
        if candidate is None:
            return ""
        last_checkin = state_store.get_state("motivation.last_checkin")
        if last_checkin:
            try:
                if datetime.now() - datetime.fromisoformat(last_checkin) < self.FOLLOW_UP_INTERVAL:
                    return ""
            except ValueError:
                pass

        state_store.set_state("motivation.last_checkin", datetime.now().isoformat())
        return (
            "Optional continuity: the user has an ongoing "
            f"{candidate.domain.lower()} topic ({candidate.topic}). If it fits the greeting, "
            "you may ask one warm, non-pressuring check-in about it; otherwise do not mention it."
        )
