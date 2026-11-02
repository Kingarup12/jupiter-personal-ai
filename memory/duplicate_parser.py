from core.json_extractor import JSONExtractor

from memory.duplicate_result import DuplicateResult


class DuplicateParser:

    def parse(self, response: str):

        try:

            data = JSONExtractor.extract(response)

            return DuplicateResult(

                is_duplicate=data.get(
                    "duplicate",
                    False
                ),

                memory_id=data.get(
                    "memory_id"
                ),

                similarity=float(
                    data.get(
                        "similarity",
                        0.0
                    )
                ),

                merged_summary=data.get(
                    "merged_summary"
                ),

                reasoning=data.get(
                    "reasoning",
                    ""
                )

            )

        except Exception:

            return DuplicateResult()