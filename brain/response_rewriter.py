"""Response Rewriter for Jupiter.

Post-processes LLM output to catch problems the validator misses:
- Semantic repetition (same phrase repeated differently)
- N-gram repetition (same 3+ word sequence appearing multiple times)
- Phrase loops where the LLM latches onto one phrase
"""

import re
from collections import Counter


class ResponseRewriter:
    """Detect and fix repetitive, low-quality LLM output."""

    @staticmethod
    def detect_ngram_repetition(text: str, n: int = 3, threshold: int = 2) -> list[str]:
        """Find n-grams that appear more than threshold times."""
        words = text.lower().split()
        if len(words) < n:
            return []
        ngrams = [" ".join(words[i:i+n]) for i in range(len(words) - n + 1)]
        counts = Counter(ngrams)
        return [gram for gram, count in counts.items() if count >= threshold]

    @staticmethod
    def detect_phrase_loop(text: str, recent_messages: list[str]) -> bool:
        """Check if LLM is repeating a phrase from its own recent outputs."""
        text_lower = text.lower()
        for past in recent_messages:
            past_lower = past.lower()
            # Extract phrases of 4+ words from past messages
            past_words = past_lower.split()
            for length in range(6, 3, -1):
                for i in range(len(past_words) - length + 1):
                    phrase = " ".join(past_words[i:i+length])
                    if phrase in text_lower and len(phrase) > 15:
                        # Same phrase appeared in a previous response
                        count_in_current = text_lower.count(phrase)
                        if count_in_current >= 1:
                            return True
        return False

    @classmethod
    def rewrite(cls, response: str, recent_assistant_messages: list[str]) -> tuple[str, bool]:
        """Attempt to fix the response. Returns (fixed_text, was_modified).
        
        If the response is too broken to fix, returns the original with was_modified=True
        to signal the caller should consider a fallback.
        """
        # Check for n-gram repetition within the response itself
        repeated_ngrams = cls.detect_ngram_repetition(response, n=3, threshold=3)
        if repeated_ngrams:
            # The LLM latched onto a phrase — this response is broken
            return response, True

        # Reusing a factual phrase from an earlier answer is normal (names and
        # identity answers are common examples), so cross-turn overlap is not
        # treated as a generation loop.

        # Check if LLM is just parroting the user's words back
        sentences = [s.strip() for s in re.split(r'[.!?]', response) if s.strip()]
        if len(sentences) >= 2:
            # If more than half the sentences end the same way, it's looping
            endings = [s.split()[-2:] if len(s.split()) >= 2 else s.split() for s in sentences]
            ending_strs = [" ".join(e) for e in endings]
            most_common = Counter(ending_strs).most_common(1)
            if most_common and most_common[0][1] >= 3:
                return response, True

        return response, False
