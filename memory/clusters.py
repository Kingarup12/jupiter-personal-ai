"""Stable memory clusters used for reflection and retrieval diversity."""

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryCluster:
    key: str
    label: str
    memories: tuple


class MemoryClusterer:
    def cluster(self, memories):
        groups = defaultdict(list)
        for memory in memories:
            key = f"{memory.domain}:{memory.category}".upper()
            groups[key].append(memory)
        clusters = []
        for key, members in groups.items():
            members.sort(key=lambda memory: memory.last_updated or memory.created_at, reverse=True)
            clusters.append(MemoryCluster(key, key.replace(":", " · ").title(), tuple(members)))
        return sorted(clusters, key=lambda cluster: len(cluster.memories), reverse=True)
