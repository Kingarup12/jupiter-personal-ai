"""Choose a helpful response mode before the chat model writes a reply.

This is deliberately a deterministic decision layer: it makes Jupiter's
conversation behaviour stable without adding latency or trusting an LLM to
invent its own social rules each turn.
"""

from dataclasses import dataclass

from brain.emotion import EmotionDetector, EmotionState
from brain.social.curiosity import CuriosityEngine
from brain.topic_tracker import TopicTracker
from memory.profile import UserProfileStore
from brain.conversation import prior_turns, temporary_speaker_possible


@dataclass(frozen=True)
class ConversationPlan:
    action: str
    topic: str | None
    emotion: EmotionState
    instruction: str


class ConversationManager:
    GREETINGS = {"hi", "hello", "hey", "heyy", "hii", "yo", "namaste"}
    FAREWELLS = {"bye", "byee", "goodbye", "goodnight", "gn", "exit", "quit"}
    REQUEST_WORDS = {
        "build", "create", "debug", "explain", "fix", "help", "make", "plan",
        "review", "show", "tell", "write",
    }

    def __init__(self, user_id=None):
        self.emotions = EmotionDetector()
        self.topics = TopicTracker()
        self.curiosity = CuriosityEngine(user_id=user_id)
        self.profile = UserProfileStore(user_id=user_id)

    @staticmethod
    def _preference_instruction(profile_memories: list) -> str:
        """Apply explicit communication preferences without guessing from a turn."""

        preferences = [
            memory
            for memory in profile_memories
            if memory.domain == "PREFERENCE"
            or memory.category.lower() in {"preference", "preferences"}
        ]
        preference_text = " ".join(
            " ".join(
                [
                    memory.summary,
                    str(memory.attributes.get("response_length", "")),
                    str(memory.attributes.get("reply_length", "")),
                    str(memory.attributes.get("preferred_language", "")),
                    str(memory.attributes.get("address_style", "")),
                    " ".join(memory.attributes.get("avoid_addresses", [])),
                ]
            ).lower()
            for memory in preferences
        )
        instructions = []
        if "hinglish" in preference_text:
            instructions.append(
                "Known communication preference: reply in natural, simple Roman-script Hinglish by default."
            )
        elif "english" in preference_text:
            instructions.append("Known communication preference: reply mainly in English.")
        if any(word in preference_text for word in ("bhaiya", "bhai", "bro", "name_or_neutral")):
            instructions.append(
                "Known address preference: never call the user bhaiya, bhai, or bro; use their saved name or no address."
            )
        if any(word in preference_text for word in ("concise", "brief", "short replies")):
            instructions.append("Known communication preference: keep replies concise unless the user asks for detail.")
        elif any(word in preference_text for word in ("detailed", "in-depth", "long replies")):
            instructions.append("Known communication preference: include useful detail and reasoning when answering.")
        return "\n".join(instructions)

    def _onboarding_instruction(
        self,
        profile_memories: list,
        *,
        authenticated_display_name: str = "",
        action: str = "CONTINUE",
    ) -> str:
        """Generate a name-capture instruction when formal_name is missing."""

        profile = self.profile.load()
        name = (
            " ".join(str(authenticated_display_name or "").split())
            or profile.get("preferred_name")
            or profile.get("formal_name")
        )
        if name and name not in ("User", "default"):
            return ""
        if action not in {"GREET", "CONTINUE"}:
            return ""
        # Check if name is already in profile memories
        for memory in profile_memories:
            attributes = memory.attributes or {}
            if attributes.get("formal_name") or attributes.get("preferred_name"):
                return ""
        return (
            "The user's name is not yet known. If this is a greeting or early "
            "conversation turn, naturally ask what Jupiter should call them. "
            "Do not use 'bhaiya', 'bhai', 'bro', or 'sir'. Ask only once per session."
        )

    def plan(
        self,
        user_message: str,
        conversation_messages: list[dict],
        profile_memories: list,
        memory_count: int,
        authenticated_profile: dict | None = None,
    ) -> ConversationPlan:
        authenticated_profile = authenticated_profile if isinstance(authenticated_profile, dict) else {}
        authenticated_display_name = str(authenticated_profile.get("display_name") or "").strip()
        lowered = user_message.strip().lower()
        words = {word.strip(".,?!:;()[]{}\"") for word in lowered.split()}
        emotion = self.emotions.detect(user_message)
        topic = self.topics.active_topic(user_message, conversation_messages)
        is_question = "?" in user_message
        is_request = bool(words.intersection(self.REQUEST_WORDS))
        is_greeting = bool(words.intersection(self.GREETINGS)) and len(words) <= 4
        is_farewell = bool(words.intersection(self.FAREWELLS)) and len(words) <= 4
        continuing = bool(prior_turns(user_message, conversation_messages)) or any(
            item.get('_p4_compaction') for item in conversation_messages
        )
        guest_possible = temporary_speaker_possible(conversation_messages)

        if is_farewell:
            action = "END"
            action_instruction = "Give a warm, short sign-off. Do not ask a follow-up question."
        elif emotion.needs_care:
            action = "LISTEN"
            action_instruction = (
                "Respond with calm empathy first. Do not joke, rush to solve, or "
                "turn this into a profile question. Offer practical help only if it "
                "fits what the user said."
            )
        elif is_greeting and not continuing:
            action = "GREET"
            action_instruction = (
                "Give a warm, concise, modern greeting that matches the user's wording and energy. "
                "Do not default to Namaste or formal honorific language unless the user did or the context calls for it."
            )
        elif is_question or is_request:
            action = "ANSWER"
            action_instruction = "Answer the user's request directly before any optional follow-up."
        else:
            action = "CONTINUE"
            action_instruction = "Continue the user's topic naturally; do not change the subject."
        if continuing:
            action_instruction += (
                " This is an ongoing conversation, not first contact. Short greetings and "
                "acknowledgements continue its subject, participants and recent language; do not "
                "restart onboarding or introduce yourself again unless asked."
            )

        social_instruction = ""
        if not is_farewell and not guest_possible and not (continuing and len(words) <= 4):
            social_instruction = self.curiosity.instruction(
                user_message,
                profile_memories,
                conversation_messages,
                memory_count=memory_count,
                emotional=emotion.needs_care,
                direct_request=is_question or is_request,
                registered_display_name=authenticated_display_name,
            )
        topic_instruction = (
            f"Tentative topic keywords: {topic}. Use only if supported by the recent dialogue; "
            "these keywords do not resolve missing referents." if topic else "Follow the user's current lead."
        )
        emotion_instruction = (
            f"The message may carry a {emotion.label} tone; match it with care."
            if emotion.label != "neutral"
            else "Keep the emotional tone proportionate to the message."
        )
        preference_instruction = self._preference_instruction(profile_memories)
        relationship_stage = self.curiosity.relationships.stage(profile_memories, memory_count)
        relationship_instruction = {
            "NEW": "Relationship context: little durable familiarity is recorded; this does not mean a new conversation. Be welcoming without being overfamiliar.",
            "KNOWN": "Relationship context: the user is familiar; use a relaxed, respectful tone.",
            "REGULAR": "Relationship context: this is a regular user; use natural rapport and relevant continuity.",
            "FRIEND": "Relationship context: the user is a trusted regular; be warmer and more casual when it fits.",
            "CLOSE_FRIEND": "Relationship context: the user is a close, trusted regular; be warm without inventing shared experiences.",
        }[relationship_stage]
        onboarding_instruction = "" if continuing or guest_possible else self._onboarding_instruction(
            profile_memories,
            authenticated_display_name=authenticated_display_name,
            action=action,
        )
        instruction = "\n".join(
            part
            for part in (
                action_instruction,
                topic_instruction,
                emotion_instruction,
                preference_instruction,
                relationship_instruction,
                social_instruction,
                onboarding_instruction,
            )
            if part
        )
        return ConversationPlan(action, topic, emotion, instruction)
