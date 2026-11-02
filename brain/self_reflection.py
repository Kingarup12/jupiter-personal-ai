"""
Module for generating per-turn internal reflections.

This module provides a lightweight 'thinking step' that helps Jupiter
maintain conversational awareness by generating brief internal notes.
"""

import os
from core.model_manager import Task, ModelManager
from core.llm import LLM
from core.logger import Logger


class SelfReflection:
    """Generate brief per-turn internal notes for conversational awareness."""

    def __init__(self):
        self.enabled = os.getenv('JUPITER_ENABLE_REFLECTION', 'true').lower() != 'false'

    def reflect(self, user_message: str, conversation_messages: list[dict], profile: dict) -> str | None:
        """Return a short internal note (1-2 sentences) or None.

        Examples of output:
        - 'User seems tired today, keep replies light and short.'
        - 'User is excited about their new laptop, match their energy.'
        - 'Don't bring up exams right now, user seems stressed about them.'
        - 'User is opening up more today, they trust Jupiter.'
        """
        if not self.enabled:
            return None

        # Extract context
        name = profile.get("preferred_name") or profile.get("formal_name") or "User"
        emotional = profile.get("emotional_state") or {}
        emotional_state = emotional.get("label", "neutral") if isinstance(emotional, dict) else "neutral"
        relationship_stage = profile.get("relationship", {}).get("stage", "NEW")
        
        # Get last 6 messages
        recent_messages = conversation_messages[-6:] if conversation_messages else []
        conversation_context = "\n".join(
            f"{msg.get('role', 'unknown')}: {msg.get('content', '')}" 
            for msg in recent_messages
        )
        
        system_prompt = (
            "You are Jupiter's internal subconscious. Your job is to generate a brief (1-2 sentence) "
            "internal note about how Jupiter should approach the user based on their current state and recent messages.\n"
            "1. Read the user's latest message and recent conversation.\n"
            "2. Consider the user's emotional state and relationship stage.\n"
            "3. Output ONLY the 1-2 sentence internal note about tone/approach.\n"
            "4. If no special note is needed, output EXACTLY the word 'NONE'."
        )
        
        user_prompt = (
            f"User Profile Info:\n"
            f"- Name: {name}\n"
            f"- Emotion: {emotional_state}\n"
            f"- Relationship: {relationship_stage}\n\n"
            f"Recent Conversation:\n{conversation_context}\n\n"
            f"Latest User Message:\n{user_message}"
        )
        
        try:
            response = LLM.generate(ModelManager().get_best_models(Task.MEMORY_ANALYZER), system_prompt, user_prompt)
            result = response.strip()
            
            if not result or result.upper() == 'NONE':
                return None
                
            return result
        except Exception as e:
            Logger.error(f"Self-reflection skipped: {e}")
            return None
