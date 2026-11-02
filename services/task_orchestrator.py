"""Task planning and bounded parallel preflight, separate from model routing."""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from core.provider_contracts import TaskType
from services.freshness import is_fresh_information_request
from services.model_routing import TaskClassifier, TaskRequirements


_CURRENT_INFO = re.compile(r"(?i)\b(latest|current|today|right now|news|recent)\b")
_TRANSLATE = re.compile(r"(?i)\b(translate|translation|in (?:english|hindi|bengali|spanish|french|german))\b")
_HUGE_CONTEXT = re.compile(r"(?i)\b(whole|entire|large)\b.{0,30}\b(codebase|repository|repo|document|project)\b")
_FACE_RELEVANT = re.compile(
    r"(?i)\b(face|faces|person|people|who is|identify|verify|match"
    # Known-person lookups (recognition still requires the user's own enrolled people; otherwise no Face++ call).
    r"|is (?:this|that|he|she) my (?:sister|brother|mother|mom|mum|father|dad|wife|husband|son|daughter|friend|best friend"
    r"|girlfriend|boyfriend|partner|grandmother|grandma|grandfather|grandpa|aunt|uncle|cousin|niece|nephew|colleague"
    r"|teacher|boss|roommate)"
    r"|do you (?:remember|recognize|recognise|know) (?:him|her|them|this person)|who just (?:walked|came) in)\b")
_SHOPPING = re.compile(r"(?i)\b(buy|purchase|shopping|deal|price|best)\b.{0,80}\b(product|phone|laptop|camera|headphones|watch|tablet|tv|appliance)\b")
_DIFFICULT_CODING = re.compile(r"(?i)\b(repository|repo|architecture|refactor|migration|concurrency|distributed|codebase|multi-file)\b")
_DOCUMENT_REASONING = re.compile(r"(?i)\b(analy[sz]e|reason|critique|compare|evaluate|infer|contradiction|tradeoffs|prove)\b")
PREFLIGHT_BUDGET_SECONDS = float(os.getenv("JUPITER_PREFLIGHT_BUDGET_S", "8"))
_WEATHER_LOCATION = re.compile(r"(?i)\b(?:in|for|at|near)\s+([A-Za-z][A-Za-z .'-]{1,80}?)(?=\s+(?:today|tomorrow|this|next|on)\b|[?.!,]|$)")


@dataclass(frozen=True)
class WorkItem:
    name: str
    task_type: TaskType
    independent: bool = True
    specialist: str | None = None


@dataclass(frozen=True)
class OrchestrationPlan:
    primary_task: TaskType
    preflight: tuple[WorkItem, ...]
    workers: tuple[WorkItem, ...] = ()
    synthesis_task: TaskType | None = None
    max_parallel_workers: int = 1
    required_modalities: frozenset[str] = frozenset({"text"})
    required_capabilities: frozenset[str] = frozenset()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreflightResult:
    plan: OrchestrationPlan
    capability: Any = None
    relevant_memories: tuple[Any, ...] = ()
    review_question: Any = None
    safety: dict[str, Any] = field(default_factory=dict)


class TaskOrchestrator:
    """Answer what work is needed without choosing models or credentials."""

    @staticmethod
    def plan(messages: list[dict[str, Any]], attachment_mimes: set[str] | None = None) -> OrchestrationPlan:
        requirements: TaskRequirements = TaskClassifier.classify(messages, attachment_mimes)
        latest = next((str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "user"), "")
        primary = requirements.task_type
        has_weather_facts = any(
            item.get("role") == "system" and "OpenWeather tool output" in str(item.get("content") or "")
            for item in messages
        )
        has_gmail_facts = any(
            item.get("role") == "system" and "Gmail tool output" in str(item.get("content") or "")
            for item in messages
        )
        if has_weather_facts:
            primary = TaskType.HARD_REASONING if "should" in latest.casefold() else TaskType.COMPLEX_CHAT
        elif has_gmail_facts:
            primary = TaskType.EMAIL_READING
        elif _TRANSLATE.search(latest):
            primary = TaskType.TRANSLATION
        elif _SHOPPING.search(latest):
            primary = TaskType.SHOPPING_RESEARCH
            requirements = TaskRequirements(primary, frozenset({"text"}), frozenset({"web_search"}))
        elif _HUGE_CONTEXT.search(latest) or len(latest) > 24000:
            primary = TaskType.HUGE_CONTEXT
        elif primary == TaskType.CODING and len(latest) < 900 and not _DIFFICULT_CODING.search(latest):
            primary = TaskType.FAST_CODING
        elif primary == TaskType.WEB_RESEARCH and (_CURRENT_INFO.search(latest) or is_fresh_information_request(latest)):
            # A current-information question is a grounded quick answer, not a multi-worker research synthesis.
            primary = TaskType.QUICK_CURRENT_INFO
        if any(mime.startswith("video/") for mime in attachment_mimes or set()):
            primary = TaskType.VIDEO_UNDERSTANDING
            requirements = TaskRequirements(
                TaskType.VIDEO_UNDERSTANDING,
                frozenset({"text", "video"}),
                frozenset({"vision"}),
            )
        elif "application/pdf" in (attachment_mimes or set()):
            primary = TaskType.PDF_COMPLEX if _DOCUMENT_REASONING.search(latest) or _DIFFICULT_CODING.search(latest) or len(latest) > 1200 else TaskType.PDF_EXTRACTION
        preflight = (
            WorkItem("intent", TaskType.INTENT_CLASSIFICATION),
            WorkItem("capability", TaskType.CAPABILITY_CLASSIFICATION),
            WorkItem("memory", TaskType.MEMORY_RERANKING),
            WorkItem("safety", TaskType.STRUCTURED_EXTRACTION),
        )
        workers: tuple[WorkItem, ...] = ()
        synthesis = None
        if primary in {TaskType.HARD_REASONING, TaskType.HUGE_CONTEXT, TaskType.PDF_COMPLEX, TaskType.WEB_RESEARCH, TaskType.SHOPPING_RESEARCH}:
            workers = (
                WorkItem("reasoning", TaskType.HARD_REASONING),
                WorkItem("memory_context", TaskType.MEMORY_RERANKING),
                WorkItem("document_or_research", primary, specialist="task_specific"),
            )
            synthesis = TaskType.FINAL_SYNTHESIS
        return OrchestrationPlan(
            primary_task=primary,
            preflight=preflight,
            workers=workers,
            synthesis_task=synthesis,
            max_parallel_workers=min(4, max(1, len(workers) or len(preflight))),
            required_modalities=requirements.modalities,
            required_capabilities=requirements.capabilities,
            metadata={"selective_parallelism": bool(workers)},
        )

    def run_preflight(
        self,
        messages: list[dict[str, Any]],
        attachment_mimes: set[str] | None = None,
        *,
        capability_detector: Callable[[], Any] | None = None,
        memory_retriever: Callable[[], list[Any]] | None = None,
        memory_review_preparer: Callable[[], Any] | None = None,
        safety_check: Callable[[], dict[str, Any]] | None = None,
    ) -> PreflightResult:
        plan = self.plan(messages, attachment_mimes)
        jobs: dict[str, Callable[[], Any]] = {}
        if capability_detector:
            jobs["capability"] = capability_detector
        if memory_retriever:
            jobs["memory"] = memory_retriever
        if memory_review_preparer:
            jobs["memory_review"] = memory_review_preparer
        if safety_check:
            jobs["safety"] = safety_check
        results: dict[str, Any] = {}
        if jobs:
            # Preflight runs before a streamed reply sends its first byte. Every job is optional (capability falls
            # back to the deterministic detector, memories to none), so a stalled network call (e.g. an embedding
            # request) is abandoned at the shared deadline instead of silencing the stream. Straggler threads are
            # not waited for (a `with` block would join them and defeat the deadline).
            executor = ThreadPoolExecutor(max_workers=min(4, len(jobs)), thread_name_prefix="jupiter-preflight")
            deadline = time.monotonic() + PREFLIGHT_BUDGET_SECONDS
            try:
                futures = {name: executor.submit(callback) for name, callback in jobs.items()}
                for name, future in futures.items():
                    try:
                        results[name] = future.result(timeout=max(0.0, deadline - time.monotonic()))
                    except Exception:
                        results[name] = None
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        return PreflightResult(
            plan=plan,
            capability=results.get("capability"),
            relevant_memories=tuple(results.get("memory") or ()),
            review_question=results.get("memory_review"),
            safety=results.get("safety") or {"allowed": True},
        )

    @staticmethod
    def run_independent_workers(
        plan: OrchestrationPlan,
        handlers: dict[str, Callable[[], Any]],
    ) -> dict[str, Any]:
        """Run only named independent complex-task workers, never duplicate answers."""
        allowed = {item.name for item in plan.workers if item.independent}
        selected = {name: callback for name, callback in handlers.items() if name in allowed}
        if not selected:
            return {}
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(
            max_workers=min(4, plan.max_parallel_workers, len(selected)),
            thread_name_prefix="jupiter-specialist",
        ) as executor:
            futures = {name: executor.submit(callback) for name, callback in selected.items()}
            for name, future in futures.items():
                results[name] = future.result()
        return results

    @staticmethod
    def weather_location(text: str) -> str | None:
        match = _WEATHER_LOCATION.search(str(text or ""))
        return " ".join(match.group(1).split()) if match else None

    @staticmethod
    def face_analysis_relevant(text: str, attachment_mimes: set[str]) -> bool:
        return any(mime.startswith("image/") for mime in attachment_mimes) and bool(_FACE_RELEVANT.search(str(text or "")))
