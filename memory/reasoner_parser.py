from core.json_extractor import JSONExtractor

from memory.reasoning_result import ReasoningResult


class ReasonerParser:

    @staticmethod
    def parse(response: str):

        try:

            data = JSONExtractor.extract(response)

            return ReasoningResult(

                facts=data.get(
                    "facts",
                    []
                ),

                reasoning=data.get(
                    "reasoning",
                    []
                ),

                conclusion=data.get(
                    "conclusion",
                    ""
                ),

                confidence=float(

                    data.get(
                        "confidence",
                        0.0
                    )

                )

            )

        except Exception:

            return ReasoningResult()