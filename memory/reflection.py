from collections import Counter

from memory.schema import Memory


class MemoryReflection:

    def reflect(self, memories):

        if len(memories) < 5:
            return []

        category_counter = Counter()

        for memory in memories:
            category_counter[memory.category] += 1

        new_memories = []

        # AI-related pattern
        ai_keywords = (
            "ai",
            "llm",
            "machine learning",
            "deep learning",
            "python",
            "pytorch",
            "tensorflow",
            "ollama",
            "jupiter"
        )

        ai_count = 0

        for memory in memories:

            text = (
                memory.topic + " " +
                memory.summary
            ).lower()

            if any(word in text for word in ai_keywords):
                ai_count += 1

        if ai_count >= 3:

            new_memories.append(

                Memory(

                    memory_id="reflection.ai_interest",

                    category="Identity",

                    topic="Technical Interest",

                    summary="User has a strong long-term interest in Artificial Intelligence.",

                    memory_type="SEMANTIC",
                    domain="PREFERENCE",

                    facts=[
                        {
                            "subject": "User",
                            "predicate": "long_term_interest",
                            "object": "Artificial Intelligence"
                        }
                    ],

                    attributes={
                        "interest": "Artificial Intelligence"
                    },

                    importance="High",

                    retention="Forever",

                    visibility="PUBLIC",
                    confidence=0.75,
                    source="inference"

                )

            )

        return new_memories
