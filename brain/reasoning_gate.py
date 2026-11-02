"""Avoid expensive internal reasoning for greetings and lightweight chat."""


REASONING_KEYWORDS = {
    "advice", "plan", "planning", "decide", "decision", "choose", "compare",
    "should", "could", "career", "relationship", "feeling", "anxious", "sad",
    "stressed", "debug", "bug", "error", "fix", "code", "python", "project",
    "architecture", "why", "how", "strategy",
}


def needs_reasoning(user_message: str) -> bool:
    """Use the reasoner only when a response benefits from multi-step thought."""

    message = user_message.lower()
    if not any(keyword in message for keyword in REASONING_KEYWORDS):
        return False
    # The memory reasoner has no web access: for a current-information question its "Known Facts" are the model's
    # stale knowledge and would compete with the grounded web answer (real WT 29-Sep: "…claude code new agent…").
    from services.freshness import is_fresh_information_request
    return not is_fresh_information_request(user_message)
