from memory.schema import Memory
from memory.types import MemoryDomain, MemoryType


class MemoryValidator:

    VALID_IMPORTANCE = {
        "Critical",
        "High",
        "Medium",
        "Low",
        "Ephemeral",
    }

    VALID_RETENTION = {
        "Forever",
        "10 Years",
        "3 Months",
        "Three Calendar Months",
        "90 Days",
        "20 Days",
        "Session"
    }

    VALID_VISIBILITY = {
        "PUBLIC",
        "PERSONAL",
        "PRIVATE",
        "SECRET"
    }
    VALID_SOURCES = {"conversation", "manual", "reflection", "inference", "tool"}
    SUSPICIOUS_INSTRUCTION_PATTERNS = {
        "ignore previous instructions",
        "ignore all instructions",
        "system prompt",
        "developer message",
        "act as a system",
        "reveal your prompt",
    }

    def validate(self, memory: Memory):

        if memory is None:
            return False

        if not memory.memory_id.strip():
            return False

        if not memory.category.strip():
            return False

        if not memory.topic.strip():
            return False

        if not memory.summary.strip():
            return False

        if memory.importance not in self.VALID_IMPORTANCE:
            return False

        if memory.retention not in self.VALID_RETENTION:
            return False

        if memory.visibility not in self.VALID_VISIBILITY:
            return False

        if memory.memory_type not in MemoryType._value2member_map_:
            return False

        if memory.domain not in MemoryDomain._value2member_map_:
            return False

        source_kind = str(memory.source or "").split(":", 1)[0]
        if source_kind not in self.VALID_SOURCES:
            return False

        if not (0 <= memory.confidence <= 1):
            return False

        if not isinstance(memory.facts, list):
            return False

        if len(memory.facts) == 0:
            return False

        searchable_text = " ".join(
            [memory.topic, memory.summary]
            + [str(fact) for fact in memory.facts]
        ).lower()
        if any(pattern in searchable_text for pattern in self.SUSPICIOUS_INSTRUCTION_PATTERNS):
            return False

        return True
