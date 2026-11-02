from core.model_manager import Task, ModelManager

from brain.memory_prompt import MEMORY_ANALYZER_PROMPT

from memory.parser import MemoryParser
from memory.importance import MemoryImportancePredictor

from core.logger import Logger
from core.llm import LLM


class MemoryAnalyzer:

    def __init__(self):
        self.importance = MemoryImportancePredictor()

    def analyze(self, user_message, recent_conversation=None):

        Logger.info("Memory Analyzer started.")

        context = ""

        if recent_conversation:
            # Assistant replies/apologies are not evidence about the user.
            previous_messages = [item for item in recent_conversation
                                 if item.get("role") == "user" and not item.get("_synthetic")][-4:]
            context = "\n".join(
                f"{message.get('role', 'user')}: {message.get('content', '')}"
                for message in previous_messages
            )

        prompt = user_message

        if context:
            prompt = (
                "Recent conversation, only for understanding a short reply:\n"
                f"{context}\n\nLatest user message:\n{user_message}"
            )

        result = LLM.generate(

            model=ModelManager().get_best_models(Task.MEMORY_ANALYZER),

            system_prompt=MEMORY_ANALYZER_PROMPT,

            user_prompt=prompt

        )

        try:

            memory = MemoryParser.parse(result)

            if memory:
                memory = self.importance.apply(memory, user_message)

            if memory:

                Logger.info("Memory parsed successfully.")

            else:

                Logger.info("Analyzer decided not to save memory.")

            return memory

        except Exception as e:

            Logger.error("Memory JSON parsing failed.")
            Logger.error(str(e))

            return None
