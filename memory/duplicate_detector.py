

from brain.duplicate_prompt import DUPLICATE_PROMPT
from core.model_manager import Task, ModelManager
from core.logger import Logger
from memory.duplicate_parser import DuplicateParser
from memory.duplicate_result import DuplicateResult


class DuplicateDetector:
    """Find an existing memory only when the model explicitly marks a duplicate."""

    def __init__(self):
        self.parser = DuplicateParser()
        self.last_result = DuplicateResult()

    def detect(self, candidate_memories, new_memory):
        Logger.info("Duplicate detector started.")
        self.last_result = DuplicateResult()

        if not candidate_memories:
            Logger.info("No similar memories found.")
            return None

        memory_text = "\n".join(
            (
                f"ID: {memory.memory_id}\n"
                f"Category: {memory.category}\n"
                f"Topic: {memory.topic}\n"
                f"Summary: {memory.summary}"
            )
            for memory in candidate_memories
        )

        prompt = f"""
Candidate Memories

{memory_text}

--------------------------------

New Memory

Category: {new_memory.category}
Topic: {new_memory.topic}
Summary: {new_memory.summary}
"""

        try:
            from core.backends import get_backend

            backend = get_backend()
            manager = ModelManager()
            best_models = manager.get_best_models(Task.DUPLICATE)
            result = backend.chat(
                model=best_models,
                messages=[
                    {"role": "system", "content": DUPLICATE_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            self.last_result = self.parser.parse(result)
        except Exception as error:
            Logger.error(f"Duplicate detector failed: {error}")
            return None

        Logger.info("Duplicate detector finished.")

        if not self.last_result.is_duplicate:
            return None

        if self.last_result.memory_id is None:
            return None

        return next(
            (
                memory
                for memory in candidate_memories
                if memory.memory_id == self.last_result.memory_id
            ),
            None,
        )
