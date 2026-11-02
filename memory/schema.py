from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Memory:

    # ----------------------------
    # Identity
    # ----------------------------

    memory_id: str

    category: str
    topic: str

    summary: str

    # Cognitive memory type
    memory_type: str = "SEMANTIC"
    # Subject domain, kept separate from lifetime/type for diverse retrieval.
    domain: str = "GENERAL"
    # A user-expressed emotional context, never a clinical diagnosis.
    emotion: str = "neutral"

    # ----------------------------
    # Structured Knowledge
    # ----------------------------

    facts: list = field(default_factory=list)

    entities: list = field(default_factory=list)

    attributes: dict = field(default_factory=dict)

    certainty: float = 1.0

    # ----------------------------
    # Memory Metadata
    # ----------------------------

    importance: str = "Medium"

    retention: str = "Forever"

    visibility: str = "PERSONAL"

    confidence: float = 1.0

    source: str = "conversation"

    # ----------------------------
    # Retrieval Statistics
    # ----------------------------

    usage_count: int = 0

    last_accessed: str = field(
        default_factory=lambda: datetime.now().isoformat()
    )

    # ----------------------------
    # Timestamps
    # ----------------------------

    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat()
    )

    last_updated: str = field(
        default_factory=lambda: datetime.now().isoformat()
    )

    # Lifecycle. Expired memories are retained as archives instead of deleted.
    expires_at: str | None = None
    archived_at: str | None = None
    superseded_by: str | None = None
    archive_reason: str | None = None
