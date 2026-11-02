"""Graph traversal over Jupiter's atomic fact triples."""

from collections import deque


class KnowledgeGraph:
    def __init__(self, facts):
        self.facts = list(facts)

    def neighbors(self, entity, limit=20):
        key = entity.strip().lower()
        return [
            fact for fact in self.facts
            if fact.subject.lower() == key or fact.object.lower() == key
        ][:limit]

    def traverse(self, start, max_depth=2, limit=30):
        """Return connected fact paths, breadth-first, for explainable lookup."""

        queue = deque([(start, [], 0)])
        visited = {start.strip().lower()}
        paths = []
        while queue and len(paths) < limit:
            entity, path, depth = queue.popleft()
            for fact in self.neighbors(entity):
                next_entity = fact.object if fact.subject.lower() == entity.lower() else fact.subject
                next_path = path + [fact]
                paths.append(next_path)
                key = next_entity.lower()
                if depth < max_depth and key not in visited:
                    visited.add(key)
                    queue.append((next_entity, next_path, depth + 1))
        return paths

    def describe(self, entity, limit=8):
        return [
            f"{fact.subject} — {fact.predicate} → {fact.object}"
            for fact in self.neighbors(entity, limit)
        ]
