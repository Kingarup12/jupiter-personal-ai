"""Higher-level reflection over continuity views, with auditable internal notes."""

from dataclasses import dataclass

from memory.clusters import MemoryClusterer
from memory.habits import HabitEngine
from memory.interest_model import InterestModel
from memory.project_manager import ProjectManager


@dataclass(frozen=True)
class ReflectionReport:
    notes: tuple[tuple[str, str, float], ...]


class ReflectionV2:
    def __init__(self):
        self.clusters = MemoryClusterer()
        self.interests = InterestModel()
        self.projects = ProjectManager()
        self.habits = HabitEngine()

    def reflect(self, memories):
        notes = []
        interest_scores = self.interests.scores(memories)
        if interest_scores:
            top = list(interest_scores.items())[:3]
            description = ", ".join(f"{topic} ({score}/10)" for topic, score in top)
            notes.append(("interest_model", "Current strongest interest signals: " + description + ".", 0.65))

        active_projects = [project.name for project in self.projects.snapshots(memories) if project.status == "active"]
        if active_projects:
            notes.append(("project_continuity", "Active projects: " + ", ".join(active_projects) + ".", 0.75))

        dense_clusters = [cluster.label for cluster in self.clusters.cluster(memories) if len(cluster.memories) >= 3]
        if dense_clusters:
            notes.append(("memory_cluster", "Dense memory clusters: " + ", ".join(dense_clusters[:5]) + ".", 0.6))

        for habit in self.habits.detect(memories):
            notes.append(
                (
                    "habit_hypothesis",
                    f"Possible recurring pattern: {habit['pattern']} ({habit['occurrences']} observations).",
                    habit["confidence"],
                )
            )
        return ReflectionReport(tuple(notes))
