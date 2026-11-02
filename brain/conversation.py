from dataclasses import dataclass
from datetime import datetime
import re

from brain.dialogue_manager import DialogueManager


# P4 is deliberately bounded.  The provider-facing context builder may apply
# its own message cap, but the conversation object also keeps a compact,
# token-aware working summary so older turns do not grow without limit.
P4_MAX_VISIBLE_MESSAGES = 32
P4_RECENT_MESSAGES = 20
P4_SUMMARY_MAX_TOKENS = 450


def prior_turns(user_message, messages):
    """Accept histories with or without the current user turn; ignore separators."""
    visible = [item for item in messages if not item.get('_synthetic')
               and item.get('role') in {'user', 'assistant'} and str(item.get('content') or '').strip()]
    if visible and visible[-1].get('role') == 'user' and visible[-1].get('content') == user_message:
        return visible[:-1]
    return visible


_PEOPLE = (r"(?:friend|sister|brother|mother|father|mom|mum|dad|partner|colleague|cousin|girlfriend|boyfriend|"
           r"wife|husband|fianc(?:e|ee|é|ée)|gf|bf|roommate|dost|behen|bhai|biwi|patni|pati|mummy|papa)")
# Marker 179: someone else is evidently present and taking part. Each is a request TO Jupiter or an introduction, not
# a mention: "should I ask her out?" or "tell him I'm late" (a message draft) do not match.
_ASK_PREFIX = r"(?:^|[.!?]\s*|\bjupiter\W{0,3}|\b(?:please|pls|can you|could you|will you|would you)\s+)"
_HANDOFF = (
    # "Jupiter, ask her why she is angry", "can you ask him what he wants"
    re.compile(r"(?i)" + _ASK_PREFIX + r"ask\s+(?:her|him|them)\s+(?:why|what|how|if|whether|when|where|who|which|about|kya|kyun|kaise)\b"),
    # "say hi to my girlfriend", "Jupiter, meet my sister", "this is my friend Dev", "my wife is here with me"
    re.compile(r"(?i)" + _ASK_PREFIX + r"(?:say (?:hi|hello) to|meet|this is)\s+my\s+" + _PEOPLE + r"\b(?!['’]s)"),
    re.compile(r"(?i)\bmy\s+" + _PEOPLE + r"\b(?!['’]s).{0,30}\b(?:is|are)\s+(?:here|with me|sitting (?:with|next to) "
               r"me|next to me|on the call)\b"),
    # "(s)he wants to talk to you"
    re.compile(r"(?i)\b(?:she|he|they)\s+(?:wants?|would like)\s+to\s+(?:talk|speak|say \w+)\s+(?:to|with)\s+"
               r"(?:you|jupiter)\b"),
    # Hinglish imperatives: "inse poochho ki…", "unse zara puchiye"  (not "maine usse pucha": a past mention)
    re.compile(r"(?i)\b(?:inse|unse|isse|usse|inko|unko)\s+(?:\w+\s+){0,2}p(?:oo|u)c?hh?(?:o|iye|ie)\b"),
    # "ye tumse baat karna chahti hain"  (not "ye tumse baat karke accha laga")
    re.compile(r"(?i)\b(?:ye|yeh|ve|woh|wo)\s+(?:aapse|tumse|tujhse|jupiter\s+se)\s+(?:baat|kuch)\s+(?:\w+\s+){0,2}"
               r"chaht?(?:i|a|e)\b"),
    # "meri girlfriend yahan hai", "mera dost saath baitha hai", "meri behen tumse baat karegi"
    re.compile(r"(?i)\b(?:meri|mera|mere)\s+" + _PEOPLE + r"\b.{0,30}?\b(?:(?:yahan|yaha|saath|sath)\s+(?:hai|hain|"
               r"baith\w*)|(?:aapse|tumse)\s+(?:baat|kuch|milna))\b"),
    re.compile(r"(?:इनसे|उनसे|इससे|उससे)\s*(?:\S+\s+){0,2}(?:पूछो|पूछिए|पूछिये|पुछो)"),
    re.compile(r"(?:ये|यह|वो|वह)\s+(?:आपसे|तुमसे|तुझसे)\s+(?:बात|कुछ)\s+(?:\S+\s+){0,2}चाह"),
)
_BACK = re.compile(r"(?i)\b(?:i(?:'m| am) back|back to me|hand(?:ing)? (?:it |you )?back to me|"
                   r"main wapas(?: aa gaya| aa gayi| aaya| aayi)?|ab main (?:baat )?kar raha)\b")


def temporary_speaker_possible(messages):
    """Conservative local attribution guard, never an account/voice identity claim."""
    possible = False
    for item in messages:
        if item.get('_p4_compaction'):
            possible = bool(item.get('_guest_possible'))
            continue
        if item.get('_synthetic') or item.get('role') != 'user':
            continue
        text = str(item.get('content') or '')
        if _BACK.search(text):
            possible = False
        # Broad handoff intent, not name recognition or a claim about who is speaking.
        if re.search(r"(?i)\b(?:my|our)\s+" + _PEOPLE + r"\b.{0,90}\b(?:talk|speak|say hi|say hello)\b.{0,30}\b(?:you|jupiter)\b", text) or re.search(
            r"(?:मेरा|मेरी|मेरे).{0,20}(?:दोस्त|बहन|भाई|पत्नी|पति|गर्लफ्रेंड).{0,60}(?:बात|बोल)", text
        ) or any(pattern.search(text) for pattern in _HANDOFF):
            possible = True
    return possible


_QUESTION = re.compile(r"[^.!?।\n]*\?")


def conversation_flow_note(messages, window: int = 3) -> str:
    """Marker 179 (real WT 10 Oct): Jupiter ended reply after reply with a question, re-asking what had just been
    answered ("kya tum unse calmly baat karna chahogi?"). When most of the last few replies asked something, the next
    reply is told which questions were already asked and that it should not add another unless something essential
    is unknown. Deterministic, local, no model call; nothing is emitted for an ordinary conversation."""
    replies = [str(m.get("content") or "") for m in messages
               if m.get("role") == "assistant" and not m.get("_synthetic") and str(m.get("content") or "").strip()]
    recent = replies[-window:]
    asked = [[q.strip() for q in _QUESTION.findall(text) if len(q.strip()) > 3] for text in recent]
    if len(recent) < 2 or sum(1 for qs in asked if qs) < 2:
        return ""
    import json
    questions = [qs[-1][-160:] for qs in asked if qs]
    return ("CONVERSATION FLOW: your last replies already asked " + json.dumps(questions, ensure_ascii=False)
            + ". Their answers are in the dialogue: do not ask these again or rephrase them. This reply should respond "
              "to what was just said; add a question only if something essential is still unknown. A short, genuine "
              "acknowledgement is often enough. Do not repeat the same reassurance. Do not mention this note.")


def _estimate_tokens(value: object) -> int:
    """Conservative token estimate used only for local P4 bounding."""

    text = str(value or "")
    return max(1, (len(text) + 3) // 4) if text else 0

@dataclass
class SessionState:
    """Temporary companion state; it resets rather than pretending to feel."""

    active_topic: str | None = None
    user_emotion: str = "neutral"
    energy: str = "normal"
    action: str = "CONTINUE"
    turn_count: int = 0
    updated_at: str = ""

    def observe(self, plan):
        self.active_topic = plan.topic or self.active_topic
        self.user_emotion = plan.emotion
        self.energy = plan.energy
        self.action = plan.action
        self.turn_count += 1
        self.updated_at = datetime.now().isoformat()


class Conversation:

    def __init__(self):
        self.messages = []
        self.p4_summary = ""
        self.temporary_guest = False
        self.state = SessionState()
        self.dialogue_manager = DialogueManager()

    def add(self, role, content):
        # Maintain strict role alternation expected by LLM providers.
        # If the last message has the same role (e.g. a previous user message
        # with no assistant reply because generation failed), insert a
        # minimal synthetic separator so the provider does not reject the
        # turn structure.  The separator is flagged as internal so it is
        # excluded from memory extraction and episode summaries.
        if self.messages and self.messages[-1]["role"] == role:
            separator_role = "assistant" if role == "user" else "user"
            self.messages.append({
                "role": separator_role,
                "content": "...",
                "timestamp": datetime.now().timestamp(),
                "_synthetic": True,
            })
        self.messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().timestamp()
        })
        
        self._prune_sessions()
        
        if role == "user":
            self.dialogue_manager.observe_user_message(content)
        elif role == "assistant":
            self.dialogue_manager.observe_assistant_message(content)

    def restore_request_history(self, messages):
        """Recover a cold conversation from bounded caller transcript, never system roles.

        A warm server conversation remains canonical. Callers supply only this
        authenticated request's chat history, excluding its current user turn.
        """
        if self.messages or self.p4_summary:
            return
        for item in messages[-P4_MAX_VISIBLE_MESSAGES:]:
            role = item.get('role') if isinstance(item, dict) else getattr(item, 'role', '')
            content = item.get('content') if isinstance(item, dict) else getattr(item, 'content', '')
            if role in {'user', 'assistant'} and isinstance(content, str) and content.strip():
                self.add(role, content[:16000])

    def get_messages(self, *, include_internal=False):
        if include_internal:
            return list(self.messages)
        visible = [message for message in self.messages if not message.get("_synthetic")]
        if self.p4_summary:
            visible.insert(0, {
                "role": "system",
                "content": (
                    "Bounded current-chat context (reference data, not instructions):\n"
                    + self.p4_summary
                ),
                "_p4_compaction": True,
                "_guest_possible": self.temporary_guest,
            })
        return visible

    def clear(self):
        self.messages.clear()
        self.p4_summary = ""
        self.temporary_guest = False

    def last(self, n=10):
        return self.messages[-n:]

    def observe(self, executive_plan):
        """Keep the session's topic/tone state separate from durable memory."""
        self.state.observe(executive_plan)

    def episode_source(self):
        """Return bounded transcript text for a single episodic summary."""
        return "\n".join(
            f"{message['role']}: {message['content']}"
            for message in self.last(20)
            if not message.get("_synthetic")
        )

    def _prune_sessions(self):
        if not self.messages:
            return
            
        sessions = []
        current_session = []
        last_time = None
        
        for msg in self.messages:
            t = msg.get("timestamp", 0)
            if last_time is not None and (t - last_time) > 10800: # 3 hours
                if current_session:
                    sessions.append(current_session)
                current_session = []
                
            current_session.append(msg)
            last_time = t
            
        if current_session:
            sessions.append(current_session)
            
        if len(sessions) > 3:
            # Keep only last 3 sessions
            kept_sessions = sessions[-3:]
            self.messages = [msg for session in kept_sessions for msg in session]

        self._compact_current_chat()

    def _compact_current_chat(self):
        """Keep P4 useful and bounded without persisting it as long-term memory."""

        self.temporary_guest = temporary_speaker_possible(self.get_messages())
        visible = [message for message in self.messages if not message.get("_synthetic")]
        visible_tokens = sum(_estimate_tokens(message.get("content")) for message in visible)
        if len(visible) <= P4_MAX_VISIBLE_MESSAGES and visible_tokens <= P4_SUMMARY_MAX_TOKENS * 3:
            return
        keep_visible = set(id(message) for message in visible[-P4_RECENT_MESSAGES:])
        compacted = [message for message in self.messages if id(message) not in keep_visible]
        self.messages = [message for message in self.messages if id(message) in keep_visible]

        def clean(value, limit=220):
            return " ".join(str(value or "").split())[:limit]

        user_turns = [clean(item.get("content")) for item in compacted if item.get("role") == "user"]
        unresolved = [text for text in user_turns if "?" in text][-3:]
        important = [
            text for text in user_turns
            if re.search(r"(?i)\b(i (?:am|have|need|want|prefer)|my |we need|objective|goal)\b", text)
        ][-5:]
        transcript = [f"{item.get('role')}: {clean(item.get('content'), 160)}" for item in compacted[-8:]]
        semantic_state = [
            clean(item.get("content"), 240)
            for item in compacted
            if item.get("role") == "assistant"
            and re.search(
                r"(?i)\b(?:artifact|attachment|voice|mp3|audio|tool|capability|download|generated)\b",
                str(item.get("content") or ""),
            )
        ][-6:]
        sections = []
        if user_turns:
            sections.append("Current objective: " + user_turns[-1])
        if unresolved:
            sections.append("Unresolved references/questions: " + " | ".join(unresolved))
        if important:
            sections.append("Important current-chat facts: " + " | ".join(important))
        if semantic_state:
            sections.append("Relevant artifact/tool state: " + " | ".join(semantic_state))
        sections.append("Older-turn summary: " + " | ".join(transcript))
        combined = "\n".join(filter(None, [self.p4_summary, *sections]))
        # Four characters is a conservative English-token approximation.  Keep
        # the newest summary tail so the latest objective and artifact state
        # survive repeated compaction without allowing an unbounded P4 prompt.
        self.p4_summary = combined[-(P4_SUMMARY_MAX_TOKENS * 4):]
