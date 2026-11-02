"""Reply Planning Pipeline for Jupiter.

This module forces the LLM to structure its internal thinking before generating
the final response. It ensures Jupiter considers dialogue state, memory,
emotion, and personality before speaking.
"""

import re


class ReplyPlanner:
    """Constructs the instruction for the LLM to plan its reply."""
    
    @staticmethod
    def get_planning_instruction() -> str:
        """Returns the system instruction enforcing the Reply Plan pipeline."""
        return (
            "BEFORE you write your reply, you MUST think inside <thought>...</thought> tags.\n"
            "This thinking is INVISIBLE to the user. Use it to plan.\n\n"
            "Inside <thought>, answer these:\n"
            "1. UNDERSTAND: What did the user actually mean? Did they answer my question? Are they asking me something?\n"
            "2. EMOTION: How are they feeling? (happy, sad, playful, neutral, excited, bored)\n"
            "3. GOAL: What should I do? (answer, tease, empathize, share, continue)\n"
            "4. MEMORY: Any relevant memories to reference? Any callbacks?\n"
            "5. TONE: What energy level? (calm, playful, warm, excited)\n"
            "6. PLAN: Draft my exact response in 2-4 sentences.\n\n"
            "After </thought>, write ONLY the final response. No tags, no labels.\n\n"
            "Example:\n"
            "<thought>\n"
            "User said 'mai thik hu tum batao'. They answered my question (how are you) and asked me back.\n"
            "Emotion: neutral/casual. Goal: answer their question back, keep it light.\n"
            "Memory: User likes gaming. Callback: mentioned Blender last time.\n"
            "Tone: playful. Plan: Say I'm good, then casually bring up their Blender project.\n"
            "</thought>\n"
            "Main bhi mast hoon 😄 Waise, Blender ka kya scene hai? Kuch progress hua ya abhi break pe hai? 😏"
        )

    @staticmethod
    def strip_thought(response: str) -> str:
        """Removes the <thought> block from the final output."""
        # Remove everything between <thought> and </thought>
        cleaned = re.sub(r"<thought>.*?</thought>\s*", "", response, flags=re.DOTALL | re.IGNORECASE)
        # Also handle /think tags some models use
        cleaned = re.sub(r"<think>.*?</think>\s*", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
        # Fallback if tags are malformed or missing
        if not cleaned.strip():
            return response
        return cleaned.strip()
