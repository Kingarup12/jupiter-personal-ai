"""Chronological view over durable and episodic memory."""

from collections import defaultdict
from datetime import datetime


class LifeTimeline:
    def by_year(self, memories):
        years = defaultdict(list)
        for memory in memories:
            try:
                year = datetime.fromisoformat(memory.created_at).year
            except (TypeError, ValueError):
                year = "Unknown"
            years[year].append(memory)
        return {
            year: sorted(records, key=lambda record: record.created_at)
            for year, records in sorted(years.items(), key=lambda item: str(item[0]))
        }
