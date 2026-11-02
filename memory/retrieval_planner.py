"""Plan a balanced memory query before retrieval instead of blindly searching."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalPlan:
    domains: tuple[str, ...]
    include_recent_episode: bool
    reason: str


class RetrievalPlanner:
    def plan(self, executive_plan):
        domains = executive_plan.retrieval_domains or ("GENERAL",)
        if executive_plan.action == "LISTEN":
            domains = tuple(dict.fromkeys((*domains, "EMOTION", "RELATIONSHIP")))
        return RetrievalPlan(
            domains=domains,
            include_recent_episode=executive_plan.action in {"CONTINUE", "LISTEN"},
            reason="executive plan selected " + ", ".join(domain.lower() for domain in domains),
        )
