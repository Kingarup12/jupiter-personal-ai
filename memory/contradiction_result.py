from dataclasses import dataclass


@dataclass
class ContradictionResult:

    contradiction: bool = False

    confidence: float = 0.0

    reason: str = ""

    action: str = "keep"