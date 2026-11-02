"""Jupiter's explicit executive layer.

The executive layer is intentionally separate from answer reasoning.  It makes
small, inspectable decisions about *how* to handle a turn before an LLM is
asked to write anything: user intent, emotional fit, memory use, follow-up
goals, and whether a message is worth considering for long-term memory.
"""

from dataclasses import dataclass, field

from brain.conversation_manager import ConversationManager


@dataclass(frozen=True)
class ExecutivePlan:
    objective: str
    action: str
    topic: str | None
    emotion: str
    energy: str
    should_retrieve: bool
    retrieval_domains: tuple[str, ...]
    should_consider_memory: bool
    should_reflect: bool
    instruction: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


class ExecutivePlanner:
    """A deterministic, low-latency controller for the companion loop."""

    MEMORY_SKIP_WORDS = {
        "hi", "hello", "hey", "heyy", "hii", "yo", "bye", "byee",
        "thanks", "thank", "okay", "ok", "cool", "lol", "haha",
        "hmm", "hmmm", "accha", "acha", "achha", "oh", "ohh", 
        "thik", "theek", "hu", "hoon", "haan", "han", "na", 
        "mai", "main", "mera", "meri", "tum", "tumhara", "aap", "batao",
        "samajh", "nhi", "nahi", "aa", "raha", "gaya", "kya", "kyu", "kaise"
    }
    DOMAIN_KEYWORDS = {
        "PROJECT": {"project", "build", "app", "jupiter", "bug", "code", "task"},
        "GOAL": {"goal", "dream", "plan", "exam", "prepare", "preparation"},
        "EDUCATION": {"study", "college", "school", "exam", "wbjee", "course"},
        "WORK": {"job", "career", "internship", "work", "office"},
        "DEVICE": {"laptop", "gpu", "ram", "phone", "computer"},
        "PREFERENCE": {"prefer", "like", "favorite", "favourite", "short", "detailed"},
        "RELATIONSHIP": {"friend", "mother", "father", "brother", "family"},
        "HEALTH": {"health", "sleep", "exercise", "sick", "stress"},
    }

    def __init__(self, user_id=None):
        self.conversation = ConversationManager(user_id=user_id)

    def evaluate(
        self,
        user_message: str,
        conversation_messages: list[dict],
        profile_memories: list,
        memory_count: int,
        motivation_instruction: str = "",
        authenticated_profile: dict | None = None,
    ) -> ExecutivePlan:
        social_plan = self.conversation.plan(
            user_message, conversation_messages, profile_memories, memory_count,
            authenticated_profile=authenticated_profile,
        )
        lowered = user_message.lower()
        words = {
            token.strip(".,?!:;()[]{}\"")
            for token in lowered.split()
            if token.strip(".,?!:;()[]{}\"")
        }
        reasons = [f"conversation action={social_plan.action.lower()}"]

        if social_plan.action == "LISTEN":
            objective = "help the user feel heard and supported"
            energy = "calm"
        elif social_plan.action == "ANSWER":
            objective = "solve the user's stated request"
            energy = "focused"
        elif social_plan.action == "GREET":
            objective = "welcome the user and establish a natural next step"
            energy = "warm"
        elif social_plan.action == "END":
            objective = "close the conversation warmly"
            energy = "soft"
        else:
            objective = "continue the user's current topic usefully"
            energy = "playful" if any(token in words for token in {"lol", "haha", "meme", "funny"}) else "normal"

        domains = tuple(
            domain
            for domain, keywords in self.DOMAIN_KEYWORDS.items()
            if words.intersection(keywords)
        )
        if not domains and social_plan.topic:
            domains = ("GENERAL",)

        is_small_talk = bool(words) and words.issubset(self.MEMORY_SKIP_WORDS)
        should_retrieve = social_plan.action not in {"GREET", "END"} or bool(domains)
        should_consider_memory = (
            not is_small_talk and social_plan.action not in {"GREET", "END"}
        )
        should_reflect = social_plan.action in {"LISTEN", "CONTINUE"} and not is_small_talk
        if domains:
            reasons.append("targeted retrieval=" + ",".join(domains).lower())
        if should_consider_memory:
            reasons.append("message may contain durable context")

        instruction_parts = [
            "Executive objective: " + objective + ".",
            "Conversation energy: " + energy + ".",
            social_plan.instruction,
        ]
        if motivation_instruction:
            instruction_parts.append(motivation_instruction)

        return ExecutivePlan(
            objective=objective,
            action=social_plan.action,
            topic=social_plan.topic,
            emotion=social_plan.emotion.label,
            energy=energy,
            should_retrieve=should_retrieve,
            retrieval_domains=domains,
            should_consider_memory=should_consider_memory,
            should_reflect=should_reflect,
            instruction="\n".join(part for part in instruction_parts if part),
            reasons=tuple(reasons),
        )
