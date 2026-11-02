"""A generated reply must not promise a reminder or a call that was never scheduled (marker 220).

Real 26 Oct: "1 baje bol dena call kerke station aa raha hai" was not recognised as a reminder, so the general chat model
answered "Done, Boss! 1 baje call karke yaad dila doongi" and then "Done! Time update ho gaya hai — ab 12:54 pe call
karke yaad dila doongi." Nothing was ever scheduled. Reminders and calls are created only by the personal assistant;
when the chat model itself claims one, the claim is replaced with what is true.
"""

from __future__ import annotations

import re

from services.delivery_claims import _language

_CLAIM = re.compile(
    r"(?i)\b(?:i'?ll|i\s+will|i'?m\s+going\s+to|i\s+have|i'?ve)\s+(?:also\s+)?(?:set|scheduled|added|created|updated|moved|changed)\s+"
    r"(?:a|an|the|your|that|this)?\s*(?:reminder|alarm|call)"
    r"|\b(?:i'?ll|i\s+will)\s+(?:also\s+)?(?:remind|ring|call)\s+you\b"
    r"|\breminder\s+(?:is\s+|has\s+been\s+)?(?:set|scheduled|updated|moved)\b"
    r"|\byaad\s+dila\s+(?:d[uo]{1,2}ng[ia]|d[eo]{1,2}ng[ei]|deti\s+hoon|dunga|dungi)\b"
    r"|\bcall\s+(?:kar\s*ke|karke|kerke)\s+(?:yaad\s+dila|bata|bol)\w*"
    r"|\b(?:call|phone)\s+kar(?:\s+)?(?:u|oo)ng[ia]\b"
    r"|\bbata\s+(?:d[uo]{1,2}ng[ia]|d[eo]{1,2}ng[ei])\b"
    r"|\breminder\s+(?:set|laga|update)\s+(?:kar\s+(?:diya|di|dungi|doongi|dunga)|ho\s+gaya)"
    r"|\btime\s+update\s+ho\s+gaya\b"
    r"|याद\s+दिला\s+द(?:ूंगी|ूँगी|ूंगा|ूँगा|ेंगे)|रिमाइंडर\s+(?:सेट|लगा)"
)
_HEDGE = re.compile(
    r"(?i)\b(?:can'?t|cannot|couldn'?t|won'?t|unable|not\s+able|haven'?t|didn'?t|nahi|nahin|nhi|if\s+you(?:\s+want)?|should\s+i|"
    r"shall\s+i|do\s+you\s+want|want\s+me\s+to|kya\s+main|karun\s+kya|chahiye\?)|नहीं"
)


# Only a reply to a request that asked for something timed is checked; a reply that mentions a reminder the assistant
# really set earlier ("12:54 wale reminder ke liye") is left alone.
_REQUEST_CUE = re.compile(
    r"(?i)\b(?:remind|reminder|alarm|call|ring|phone|yaad|baje|bol\s+dena|bata\s+dena|batana|uthana|jaga|"
    r"\d{1,2}[:.]\d{2}|\d{1,2}\s*(?:am|pm)|baad|tomorrow|kal)\b|याद|बजे|रिमाइंडर")


def asks_for_timed_action(user_input: str) -> bool:
    return bool(_REQUEST_CUE.search(str(user_input or "")))


def false_reminder_claim(reply: str) -> bool:
    """A sentence of the reply promises a reminder / call (not hedged, not offered as a question)."""
    for sentence in re.split(r"(?<=[.!?।])\s+", " ".join(str(reply or "").split())):
        if _CLAIM.search(sentence) and not _HEDGE.search(sentence) and not sentence.rstrip().endswith("?"):
            return True
    return False


def reminder_not_set_reply(user_input: str) -> str:
    language = _language(user_input)
    if language == "hi":
        return ("मैंने अभी कोई रिमाइंडर सेट नहीं किया है। समय और बात बताइए, जैसे \"1 बजे याद दिला देना कि स्टेशन आ रहा है\", "
                "फिर मैं सच में सेट कर दूँगी।")
    if language == "hi-Latn":
        return ("Maine abhi koi reminder set nahi kiya hai. Time aur baat bata do, jaise \"1 baje yaad dila dena ki station "
                "aa raha hai\" — phir main sach mein set kar dungi.")
    return ("I haven't actually set a reminder for that. Tell me the time and what to remind you about (for example, "
            "\"remind me at 1 pm that the station is coming\") and I'll set it.")
