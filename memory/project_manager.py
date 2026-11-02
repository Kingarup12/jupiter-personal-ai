"""A read-model for project continuity built from project memories."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectSnapshot:
    name: str
    status: str
    notes: tuple[str, ...]
    next_task: str | None


class ProjectManager:
    def snapshots(self, memories):
        by_topic = {}
        for memory in memories:
            if memory.domain != "PROJECT":
                continue
            snapshot = by_topic.setdefault(memory.topic, [])
            snapshot.append(memory)
        results = []
        for name, records in by_topic.items():
            records.sort(key=lambda record: record.last_updated or record.created_at, reverse=True)
            latest = records[0]
            text = " ".join(record.summary.lower() for record in records)
            status = "completed" if "completed" in text or "finished" in text else "active"
            next_task = next(
                (record.summary for record in records if "next" in record.summary.lower() or "todo" in record.summary.lower()),
                None,
            )
            results.append(ProjectSnapshot(name, status, tuple(record.summary for record in records[:8]), next_task))
        return results
