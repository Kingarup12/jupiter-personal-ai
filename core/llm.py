"""Unified LLM interface for Jupiter.

Every module that needs an LLM call goes through this class.
The actual provider (Ollama, OpenAI, Gemini, llama.cpp) is resolved
by the backend registry in core.backends.
"""

from config.settings import TEMPERATURE
from core.logger import Logger


class LLM:

    @staticmethod
    def generate(
        model,
        system_prompt,
        user_prompt,
        temperature=None,
    ):
        if temperature is None:
            temperature = TEMPERATURE

        try:
            from core.backends import get_backend

            backend = get_backend()
            return backend.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
            )
        except Exception as e:
            Logger.error("LLM request failed.")
            Logger.error(str(e))
            raise
