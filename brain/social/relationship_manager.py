"""Derive a gradual relationship stage from locally stored, user-owned data."""


NAME_TOPICS = {"name", "formal name", "user name", "identity", "nickname"}


class RelationshipManager:
    def get_user_name(self, profile_memories):
        for memory in profile_memories:
            attributes = memory.attributes or {}
            name = (
                attributes.get("preferred_name")
                or attributes.get("name")
                or attributes.get("formal_name")
            )
            if isinstance(name, str) and name.strip():
                return name.strip()

            if memory.topic.lower() in NAME_TOPICS:
                return memory.summary.strip()

        return None

    def get_score(self, profile_memories, memory_count=0, profile=None) -> int:
        """Calculate a 0-100 continuous relationship score."""
        interaction_count = 0
        if isinstance(profile, dict):
            relationship = profile.get("relationship", {})
            interaction_count = int(relationship.get("interaction_count", 0))
            
        # Every interaction gives some points, but memories carry weight too.
        # It takes ~50 interactions or a mix of interactions/memories to reach 100.
        score = min(100, int((interaction_count * 1.5) + (memory_count * 2.0)))
        return score

    def stage(self, profile_memories, memory_count=0, profile=None):
        """Return a tone hint based on the 0-100 score."""

        if not self.get_user_name(profile_memories):
            return "NEW"

        score = self.get_score(profile_memories, memory_count, profile)

        if score < 20:
            return "KNOWN"
        if score < 45:
            return "REGULAR"
        if score < 75:
            return "FRIEND"
        return "CLOSE_FRIEND"

    @staticmethod
    def tone_for_stage(stage: str) -> str:
        """Return a brief tone description for the relationship stage."""
        return {
            "NEW": "Relationship Score 0-10: Warm and welcoming but not overfamiliar; no callbacks to shared history.",
            "KNOWN": "Relationship Score 11-20 (Formal/Polite): Relaxed and friendly; light references to recent topics.",
            "REGULAR": "Relationship Score 21-45 (Friendly/Curious): Natural rapport; remembers preferences and ongoing topics.",
            "FRIEND": "Relationship Score 46-75 (Playful): Playful, occasional teasing, running jokes, emotional callbacks.",
            "CLOSE_FRIEND": "Relationship Score 76-100 (Deep Trust): Casual and warm; uses shared history; deep emotional awareness. Tu/Tum naturally.",
        }.get(stage, "Be warm and helpful.")
