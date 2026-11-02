"""Deterministic monthly companion summary over existing user memories."""

from datetime import datetime

from memory.interest_model import InterestModel
from memory.project_manager import ProjectManager


class MonthlySummarizer:
    def __init__(self):
        self.interests = InterestModel()
        self.projects = ProjectManager()

    def build(self, memories, month=None):
        month = month or datetime.now().strftime("%Y-%m")
        relevant = [
            memory for memory in memories
            if (memory.last_updated or memory.created_at).startswith(month)
        ]
        if not relevant:
            return None
        project_names = [project.name for project in self.projects.snapshots(relevant)]
        interests = list(self.interests.scores(relevant))[:3]
        events = [memory.summary for memory in relevant if memory.memory_type == "EPISODIC"][:5]
        parts = [f"{len(relevant)} meaningful memory updates"]
        if project_names:
            parts.append("projects: " + ", ".join(project_names))
        if interests:
            parts.append("active interests: " + ", ".join(interests))
        if events:
            parts.append("events: " + "; ".join(events))
        return "; ".join(parts) + "."
