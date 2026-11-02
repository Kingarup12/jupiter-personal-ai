

from brain.contradiction_prompt import CONTRADICTION_PROMPT
from core.model_manager import Task, ModelManager

from core.logger import Logger

from memory.contradiction_parser import ContradictionParser
from memory.contradiction_result import ContradictionResult


class ContradictionDetector:

    def __init__(self):

        self.parser = ContradictionParser()

    # ---------------------------------
    # Detect Contradictions
    # ---------------------------------

    def detect(self, old_memory, new_memory):

        Logger.info("Contradiction detector started.")

        prompt = f"""
Existing Memory

Category:
{old_memory.category}

Topic:
{old_memory.topic}

Summary:
{old_memory.summary}

----------------------------

New Memory

Category:
{new_memory.category}

Topic:
{new_memory.topic}

Summary:
{new_memory.summary}
"""

        try:

            from core.backends import get_backend

            backend = get_backend()
            result = backend.chat(
                model=ModelManager().get_best_models(Task.CONTRADICTION),
                messages=[
                    {"role": "system", "content": CONTRADICTION_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )

            parsed = self.parser.parse(result)

            Logger.info("Contradiction detector finished.")

            return parsed

        except Exception as e:

            Logger.error("Contradiction detector failed.")
            Logger.error(str(e))

            return ContradictionResult()
