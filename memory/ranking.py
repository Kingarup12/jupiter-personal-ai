from datetime import datetime

from memory.schema import Memory


class MemoryRanker:

    # ---------------------------------
    # Rank Memories
    # ---------------------------------

    def rank(self, user_message: str, memories: list[Memory]):

        ranked = []

        for memory in memories:

            score = self.score(
                memory,
                user_message
            )

            ranked.append((score, memory))

        ranked.sort(
            key=lambda item: item[0],
            reverse=True
        )

        return [memory for score, memory in ranked]

    # ---------------------------------
    # Calculate Score
    # ---------------------------------

    def score(self, memory: Memory, user_message: str):

        score = 0

        message = user_message.lower()

        text = (
            f"{memory.category} "
            f"{memory.topic} "
            f"{memory.summary}"
        ).lower()

        # ---------------------------------
        # Exact Topic Match
        # ---------------------------------

        if memory.topic.lower() in message:

            score += 100

        # Domain words are a useful lightweight supplement to semantic search.
        if memory.domain.lower() in message:
            score += 12

        if memory.emotion != "neutral" and memory.emotion in message:
            score += 8

        # ---------------------------------
        # Keyword Matching
        # ---------------------------------

        for word in set(message.split()):

            word = word.strip(".,?!:;()[]{}\"'")

            if len(word) < 3:
                continue

            if word in text:

                score += 20

        # ---------------------------------
        # Importance
        # ---------------------------------

        if memory.importance == "Critical":

            score += 50

        elif memory.importance == "High":

            score += 30

        elif memory.importance == "Medium":

            score += 15

        elif memory.importance == "Low":

            score += 5

        # ---------------------------------
        # Confidence
        # ---------------------------------

        score += int(memory.confidence * 20)

        # ---------------------------------
        # Usage Count
        # ---------------------------------
        # Marker 221 (real owner store: "What is the user's current latest mail" matched "what" on every question, was
        # used 40 times and so ranked first forever): how often a memory was retrieved is not evidence it is relevant.

        # ---------------------------------
        # Visibility
        # ---------------------------------

        if memory.visibility == "PUBLIC":

            score += 10

        elif memory.visibility == "PERSONAL":

            score += 8

        elif memory.visibility == "PRIVATE":

            score += 3

        # SECRET memories are handled in ContextBuilder

        # ---------------------------------
        # Recent Usage Bonus
        # ---------------------------------

        if memory.last_accessed:

            try:

                last = datetime.fromisoformat(
                    memory.last_accessed
                )

                days = (
                    datetime.now() - last
                ).days

                score += max(0, 30 - days)

            except Exception:

                pass

        return score
