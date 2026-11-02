from dataclasses import dataclass, field


@dataclass
class ReasoningResult:

    # -----------------------------
    # Facts extracted from memory
    # -----------------------------

    facts: list[str] = field(default_factory=list)

    # -----------------------------
    # Step-by-step reasoning
    # -----------------------------

    reasoning: list[str] = field(default_factory=list)

    # -----------------------------
    # Final conclusion
    # -----------------------------

    conclusion: str = ""

    # -----------------------------
    # Confidence (0.0 - 1.0)
    # -----------------------------

    confidence: float = 0.0