from dataclasses import dataclass


@dataclass
class DuplicateResult:

    # Is this the same memory?
    is_duplicate: bool = False

    # Existing memory ID to update
    memory_id: str | None = None

    # Similarity score (0-1)
    similarity: float = 0.0

    # Better summary (optional)
    merged_summary: str | None = None

    # LLM reasoning
    reasoning: str = ""