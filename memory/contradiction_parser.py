from core.json_extractor import JSONExtractor

from memory.contradiction_result import ContradictionResult


class ContradictionParser:

    @staticmethod
    def parse(response: str):

        try:

            data = JSONExtractor.extract(response)

            return ContradictionResult(

                contradiction=data.get(
                    "contradiction",
                    False
                ),

                confidence=float(
                    data.get(
                        "confidence",
                        0.0
                    )
                ),

                reason=data.get(
                    "reason",
                    ""
                ),

                action=data.get(
                    "action",
                    "keep"
                )

            )

        except Exception:

            return ContradictionResult()