"""Pure interruption policy. Models cannot override its privacy/preference gates."""
from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryDecision:
    action: str
    reason: str


def decide_delivery(*, priority="normal", importance="NORMAL", busy=False,
                    mid_answer=False, boundary=False, quiet=False,
                    recent_interruption=False, enabled=True, expires_soon=False,
                    allow_nonurgent=False):
    if not enabled:
        return DeliveryDecision("DEFER_UNTIL_IDLE", "preference_disabled")
    if quiet:
        return DeliveryDecision("DEFER_UNTIL_IDLE", "quiet_hours")
    if mid_answer:
        return DeliveryDecision("DELIVER_NEXT_SAFE_TURN", "answer_in_progress")
    if recent_interruption:
        return DeliveryDecision("DEFER_UNTIL_IDLE", "interruption_cooldown")
    if not busy or boundary:
        return DeliveryDecision("DELIVER_NOW", "topic_boundary" if boundary else "idle")
    if importance == "CRITICAL":
        return DeliveryDecision("DEFER_UNTIL_TOPIC_BOUNDARY", "critical_conversation")
    if priority == "urgent" and expires_soon and importance not in {"IMPORTANT", "CRITICAL"}:
        return DeliveryDecision("DELIVER_NOW", "urgent_deadline")
    if priority in {"urgent", "important"}:
        return DeliveryDecision("DELIVER_NEXT_SAFE_TURN", "important_at_safe_turn")
    if importance == "IMPORTANT":
        return DeliveryDecision("DEFER_UNTIL_TOPIC_BOUNDARY", "conversation_more_important")
    if allow_nonurgent and importance == "CASUAL":
        return DeliveryDecision("DELIVER_NOW", "casual_interruptions_allowed")
    return DeliveryDecision("DEFER_UNTIL_IDLE", "low_priority_wait")
