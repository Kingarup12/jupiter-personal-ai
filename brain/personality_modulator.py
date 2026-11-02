"""
Module for adjusting Jupiter's personality expression based on context.

This module provides deterministic logic to modulate tone and energy
based on relationship stage, emotional context, time of day, and
user communication style signals.
"""

from datetime import datetime

class PersonalityModulator:
    """Adjust Jupiter's expression within her fixed personality."""
    
    def modulate(self, profile: dict) -> str:
        """Return a personality modulation instruction based on current context.
        
        Args:
            profile: The user's loaded profile dict from UserProfileStore.load()
            
        Returns:
            A string instruction to prepend to the system context, or empty string.
        """
        instructions = []
        
        # Time of day
        current_hour = datetime.now().hour
        is_late_night = current_hour >= 23 or current_hour < 5
        
        # Relationship
        relationship = profile.get("relationship", {})
        stage = relationship.get("stage", "NEW")
        
        if is_late_night:
            if stage in ["REGULAR", "FRIEND", "CLOSE_FRIEND"]:
                instructions.append("It is late night. Adopt a softer, calmer tone and gently suggest resting if appropriate.")
            else:
                instructions.append("It is late night. Adopt a softer, calmer tone.")
                
        # Emotional state
        emotional_state = profile.get("emotional_state", {})
        if emotional_state:
            label = emotional_state.get("label", "").lower()
            if label in ["sad", "stressed", "anxious", "upset"]:
                instructions.append("User is currently feeling sad or stressed. Be empathetic, supportive, less playful, and avoid forced humor.")
            elif label in ["excited", "happy", "joyful"]:
                instructions.append("User is feeling excited or happy. Match their energy and be playful!")
                
        # Relationship stage specific and Pronoun logic
        if stage == "NEW":
            instructions.append("Relationship stage is NEW. Be welcoming, modern, and respectful without sounding formal or distant. Use neutral address, or 'Tum' when the user's tone is casual; reserve 'Aap' for formal or culturally respectful context. Use very few emojis.")
        elif stage == "KNOWN":
            instructions.append("Relationship stage is KNOWN. Be friendly. You can start using 'Tum' for the user. Do not overuse emojis.")
        elif stage == "REGULAR":
            instructions.append("Relationship stage is REGULAR. Be warm and natural. Use 'Tum'. Avoid excessive emojis, act like a real person.")
        elif stage == "FRIEND":
            instructions.append("Relationship stage is FRIEND. Be casual and warm. Use 'Tum' or 'Tu'. No generic emoji spam.")
        elif stage == "CLOSE_FRIEND":
            instructions.append("Relationship stage is CLOSE_FRIEND. Be very warm and highly casual. Use 'Tu' naturally. Feel free to use light callbacks to previous conversations.")
            
        # Communication style signals
        signals = profile.get("conversation_style_signals", {})
        samples = int(signals.get("samples") or 0)
        avg_message_length = float(signals.get("avg_message_length") or 0)
        hinglish_ratio = float(signals.get("hinglish_ratio") or 0.0)
        
        if samples >= 10 and avg_message_length < 6:
            instructions.append("User tends to write short messages. Keep your replies concise to match their style.")
            
        if hinglish_ratio > 0.3:
            instructions.append("User uses a significant amount of Hinglish. Incorporate some Hinglish casually into your responses.")
            
        return " ".join(instructions).strip()
