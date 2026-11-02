from datetime import datetime

from core.json_extractor import JSONExtractor
from memory.schema import Memory
from memory.types import (
    expiry_for,
    normalize_domain,
    normalize_memory_type,
    retention_label,
)


class MemoryParser:

    @staticmethod
    def parse(response: str):

        # ---------------------------------
        # Extract JSON safely
        # ---------------------------------

        data = JSONExtractor.extract(response)

        # ---------------------------------
        # AI decided not to save
        # ---------------------------------

        if not data.get("save", False):
            return None

        topic = data["topic"]
        created_at = datetime.now()
        memory_type = normalize_memory_type(data.get("memory_type", "SEMANTIC"))
        domain = normalize_domain(data.get("domain"), data["category"], memory_type)

        # ---------------------------------
        # Generate Memory ID
        # ---------------------------------

        memory_id = (
            data["category"].lower().replace(" ", "_")
            + "."
            + topic.lower().replace(" ", "_")
        )

        # ---------------------------------
        # Optional fields
        # ---------------------------------

        visibility = data.get(
            "visibility",
            "PERSONAL"
        ).upper()

        confidence = float(data.get("certainty", 1.0))

        # ---------------------------------
        # Create Memory
        # ---------------------------------

        return Memory(

            memory_id=memory_id,

            category=data["category"],
            topic=topic,
            summary=data["summary"],

            memory_type=memory_type,
            domain=domain,
            emotion=str(data.get("emotion", "neutral")).strip().lower() or "neutral",

            facts=data.get("facts", []),
            entities=data.get("entities", []),
            attributes=data.get("attributes", {}),
            certainty=confidence,

            importance=data["importance"],
            retention=retention_label(memory_type),
            visibility=visibility,

            confidence=confidence,
            source=str(data.get("source", "conversation")).strip().lower() or "conversation",

            usage_count=0,
            last_accessed=created_at.isoformat(),
            created_at=created_at.isoformat(),
            last_updated=created_at.isoformat(),
            expires_at=expiry_for(memory_type, created_at),

        )
