from brain.reasoner_prompt import REASONER_PROMPT
from core.model_manager import Task, ModelManager

from memory.reasoner_parser import ReasonerParser
from memory.reasoning_result import ReasoningResult

from core.logger import Logger


class MemoryReasoner:

    def __init__(self):

        self.parser = ReasonerParser()

    # ---------------------------------
    # Reason About Retrieved Memories
    # ---------------------------------

    def reason(

        self,

        user_message,

        memories,

        conversation=None

    ):

        Logger.info("Reasoner started.")

        # ---------------------------------
        # Build Memory Context
        # ---------------------------------

        if memories:

            memory_text = ""

            for memory in memories:

                memory_text += (
                    f"- {memory.summary}\n"
                )

        else:

            memory_text = "No relevant memories."

        # ---------------------------------
        # Build Conversation Context
        # ---------------------------------

        conversation_text = ""

        if conversation:

            for message in conversation:

                role = message.get("role", "")

                content = message.get("content", "")

                conversation_text += (
                    f"{role}: {content}\n"
                )

        else:

            conversation_text = "No recent conversation."

        # ---------------------------------
        # Final Prompt
        # ---------------------------------

        prompt = f"""
User Message:

{user_message}

Relevant Memories:

{memory_text}

Recent Conversation:

{conversation_text}
"""

        try:

            # ---------------------------------
            # Call Ollama
            # ---------------------------------

            from core.backends import get_backend

            backend = get_backend()
            result = backend.chat(
                model=ModelManager().get_best_models(Task.MAIN_CHAT),
                messages=[
                    {"role": "system", "content": REASONER_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )

            Logger.info("Reasoner finished.")

            parsed = self.parser.parse(result)

            return parsed

        except Exception as e:

            Logger.error("Reasoner failed.")
            Logger.error(str(e))

            return ReasoningResult()
