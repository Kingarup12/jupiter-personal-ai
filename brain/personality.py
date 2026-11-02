"""Jupiter's permanent, version-controlled identity.

The persona is intentionally stored in small Markdown files.  This keeps the
assistant's non-negotiable identity separate from learned user memories and
makes personality changes deliberate, reviewable edits instead of accidental
prompt drift.
"""

from pathlib import Path


PERSONA_DIR = Path(__file__).with_name("persona")
PERSONA_FILES = (
    "identity.md",
    "speech_style.md",
    "humor.md",
    "curiosity.md",
    "relationship.md",
    "group_conversation.md",
    "boundaries.md",
    "emotional_style.md",
    "behavior_rules.md",
    "capabilities.md",           # what Jupiter can do per device; update with every new feature
)


def load_persona() -> str:
    """Load Jupiter's complete identity in a deterministic order.

    A missing file is ignored so an incomplete local checkout still starts, but
    an empty persona is never silently substituted when files are present.
    """

    sections = []
    for filename in PERSONA_FILES:
        path = PERSONA_DIR / filename
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(content)

    return "\n\n".join(sections)


SYSTEM_PROMPT = load_persona()
