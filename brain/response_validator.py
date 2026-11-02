"""Validate generated responses without inventing conversational failures."""

from __future__ import annotations

import re


class ResponseValidator:
    """Small post-generation guard for empty and identity-breaking output."""

    @staticmethod
    def validate(response: str, recent_conversation: list[dict], user_message: str) -> tuple[bool, str]:
        # Unicode-aware: count any letter/digit in any script, not just ASCII.
        response_clean = re.sub(r"[^\w]", "", response, flags=re.UNICODE).strip()
        if not response_clean:
            return False, "empty"

        robotic_phrases = (
            "i do not have personal feelings",
            "text-based ai",
            "as an ai language model",
            "as a large language model",
        )
        if any(phrase in response.lower() for phrase in robotic_phrases):
            return False, "robotic"

        # Only reject clear English-language identity violations.
        # Hindi verb conjugations like "raha hoon", "karta hoon" are normal
        # spoken Hindi and are NOT identity violations — the LanguageAdapter
        # handles Jupiter's own gendered verb forms separately.
        gender_violations = (
            "i am a human", "i am a man", "i am a boy",
            "main ek ladka", "main ladka hoon",
        )
        if any(phrase in response.lower() for phrase in gender_violations):
            return False, "gender_violation"

        # formal_hindi check removed: LanguageAdapter.fix_response() already
        # replaces these words in-place before this validator runs, so
        # rejecting the entire response is unnecessary and causes false errors.
        return True, ""

    @staticmethod
    def get_fallback(reason: str, user_message: str) -> str:
        """Legacy fallback kept truthful for older callers."""
        return "Jupiter couldn't produce a reliable response this time. Please retry."

