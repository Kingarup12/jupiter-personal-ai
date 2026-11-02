"""Assistant-transcript hygiene for Live: internal structured detection data never reaches the user as speech text.

Real WT (marker 151): asked to "make a box around it", the provider improvised Gemini-style detection output
(`[{"box_2d": [...], "label": ...}]`), which appeared verbatim as the assistant's transcript. The application now owns
marking (see live_visual_grounding), and this streaming filter removes any structured payload that still appears:
bracketed JSON (tracked across transcript chunks), code fences, and coordinate/confidence key names.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"```(?:json|javascript|js)?", re.I)
# Detection-only key names are removed anywhere; ordinary words ("label", "point", "score") only as quoted JSON keys.
_KEYS = re.compile(r"\"?\b(?:box_2d|bbox|bounding_box|[xy]_?(?:min|max))\b\"?\s*:?"
                   r"|\"(?:label|box|confidence|score|point|points|mask)\"\s*:", re.I)
_BARE_COORDS = re.compile(r"\(\s*\d{1,4}\s*(?:,\s*\d{1,4}\s*){1,3}\)")


class StructuredOutputFilter:
    """Streaming: bracket depth carries across chunks of ONE generation; reset() at each generation boundary."""

    def __init__(self) -> None:
        self.depth = 0
        self.removed = 0

    def reset(self) -> None:
        self.depth = 0

    def clean(self, text: str) -> str:
        value = _FENCE.sub("", str(text or ""))
        kept: list[str] = []
        for char in value:
            if char in "[{":
                self.depth += 1
                self.removed += 1
                continue
            if char in "]}":
                if self.depth:
                    self.depth -= 1
                self.removed += 1
                continue
            if self.depth:
                self.removed += 1
                continue
            kept.append(char)
        cleaned = "".join(kept)
        cleaned = _KEYS.sub("", cleaned)
        cleaned = _BARE_COORDS.sub("", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        return cleaned if cleaned.strip(" \t\r\n,.;:") else ""
