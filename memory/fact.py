from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4


@dataclass
class Fact:

    # ---------------------------------
    # Unique ID
    # ---------------------------------

    fact_id: str

    # ---------------------------------
    # Source Memory
    # ---------------------------------

    memory_id: str

    # ---------------------------------
    # Triple
    # subject -> predicate -> object
    # ---------------------------------

    subject: str

    predicate: str

    object: str

    # ---------------------------------
    # Metadata
    # ---------------------------------

    confidence: float = 1.0

    source: str = "memory"

    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat()
    )

    last_updated: str = field(
        default_factory=lambda: datetime.now().isoformat()
    )

    # ---------------------------------
    # Helper
    # ---------------------------------

    @staticmethod
    def create(

        memory_id,

        subject,

        predicate,

        object,

        confidence=1.0

    ):

        return Fact(

            fact_id=str(uuid4()),

            memory_id=memory_id,

            subject=subject,

            predicate=predicate,

            object=object,

            confidence=confidence

        )
