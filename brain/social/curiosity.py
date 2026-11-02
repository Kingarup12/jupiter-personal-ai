"""Rules for natural follow-ups without an extra LLM request."""

from brain.social.conversation_goals import goals_for_stage
from brain.social.relationship_manager import RelationshipManager
from memory.profile import UserProfileStore


class CuriosityEngine:
    GREETINGS = {"hi", "hello", "hey", "heyy", "hii", "yo", "namaste"}
    SHARE_WORDS = {
        "ate", "pizza", "lunch", "dinner", "breakfast", "watched", "played",
        "visited", "bought", "made", "finished", "started", "feeling",
    }
    NAME_HINTS = {
        "mera naam", "my name", "naam hai mera", "i have a name", "naam hai",
    }
    QUESTION_COOLDOWN_TURNS = 2

    def __init__(self, user_id=None):
        self.relationships = RelationshipManager()
        self.profile = UserProfileStore(user_id=user_id)

    def instruction(
        self,
        user_message,
        profile_memories,
        conversation_messages,
        memory_count=0,
        emotional=False,
        direct_request=False,
        registered_display_name=None,
    ):
        """Return a short system instruction for the final chat model, if useful."""

        message = user_message.strip().lower()
        words = set(message.replace("!", " ").replace(",", " ").split())
        stage = self.relationships.stage(profile_memories, memory_count)
        registered_name = " ".join(str(registered_display_name or "").split())
        name = registered_name or self.relationships.get_user_name(profile_memories)

        # Check actual profile for robust onboarding detection
        profile = self.profile.load()
        profile_name = registered_name or profile.get("preferred_name") or profile.get("formal_name")
        needs_onboarding = not profile_name or profile_name in ("User", "default")

        # Curiosity is optional. Direct help, emotional moments, and a recent
        # assistant question always win over collecting another profile detail.
        if emotional or direct_request or message.endswith("?"):
            return ""

        assistant_messages = [
            item.get("content", "")
            for item in conversation_messages[-(self.QUESTION_COOLDOWN_TURNS * 2):]
            if item.get("role") == "assistant"
        ]
        if any("?" in content for content in assistant_messages):
            return ""

        if not needs_onboarding and stage in {"NEW", "KNOWN"} and memory_count < 5:
            relationship = profile.get("relationship") if isinstance(profile.get("relationship"), dict) else {}
            interaction_count = int(relationship.get("interaction_count") or 0)
            communication = profile.get("communication_style") if isinstance(profile.get("communication_style"), dict) else {}
            discovery_dimensions = []
            if not profile.get("preferences"):
                discovery_dimensions.append("what they commonly want to use Jupiter for")
            if not profile.get("goals"):
                discovery_dimensions.append("a current project, study focus, work goal, or other thing they are working toward")
            if not any(value is not None for value in communication.values()):
                discovery_dimensions.append("how they generally like explanations and answers presented")
            if discovery_dimensions and (words.intersection(self.GREETINGS) or len(conversation_messages) >= 3):
                dimension = discovery_dimensions[interaction_count % len(discovery_dimensions)]
                return (
                    "New User Discovery: use the authenticated display name as already-known account "
                    "information and never ask for their name. After fully handling the current casual "
                    f"turn, you may ask one lightweight, naturally worded question about {dimension}. "
                    "Do not copy a fixed onboarding sentence, do not ask multiple things, and omit the "
                    "question if the conversational boundary does not feel natural. Any answer still "
                    "passes through normal Memory/Personality classification."
                )

        if needs_onboarding and words.intersection(self.GREETINGS):
            return (
                "Social goal: this is a new user. Give a warm, brief introduction "
                "as Jupiter and ask one natural question: what should you call them? "
                "Do not say 'How can I assist you today?', do not use 'bhaiya' or "
                "'bro', and do not ask several profile questions at once. Match the "
                "user's casual English, Hindi, or Hinglish greeting; do not default "
                "to Namaste or formal Aap phrasing unless the user does."
            )

        if needs_onboarding and any(hint in message for hint in self.NAME_HINTS):
            return (
                "Social goal: the user indicated that they have a name but did not "
                "state it clearly. Briefly acknowledge that and ask one question: "
                "what should I call you? Do not guess their name or call them bhaiya."
            )

        if needs_onboarding and not message.endswith("?") and len(words) <= 3:
            return (
                "Social goal: the user may be introducing themselves. Reply warmly "
                "and, if their name is still unknown, ask what you should call them. "
                "Ask only one question."
            )

        if (
            name
            and not message.endswith("?")
            and words.intersection(self.SHARE_WORDS)
        ):
            return (
                "Social goal: the user shared a small personal moment. Respond "
                "warmly and naturally. You may ask one light, topic-specific follow-up "
                "question if it would genuinely continue the conversation; never force it."
            )

        if conversation_messages and stage in {"KNOWN", "REGULAR"}:
            goals = "; ".join(goals_for_stage(stage))
            return (
                f"Relationship stage: {stage.lower()}. Keep these quiet goals in mind: "
                f"{goals}. Do not ask a question unless it is relevant to the user's "
                "message and there has not been a recent follow-up."
            )

        return ""
