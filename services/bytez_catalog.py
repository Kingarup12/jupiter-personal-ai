"""Authenticated, cache-backed Bytez specialist catalog discovery.

This module deliberately discovers metadata only.  It never runs a model, does
not activate a model route, and keeps Bytez outside generic-chat routing until
a later per-task health and privacy review explicitly promotes a candidate.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.provider_adapters import BytezProvider
from core.provider_contracts import ProviderFailure, ProviderModel


ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "config" / "bytez_model_catalog.json"
REGISTRY_PATH = ROOT / "config" / "model_registry.json"
REPORT_JSON_PATH = ROOT / "reports" / "bytez_model_inventory.json"
REPORT_MD_PATH = ROOT / "reports" / "bytez_model_inventory.md"
REFRESH_HOURS = 24

# These are policy roles, not runnable general-chat route names.  A model only
# gets a role when Bytez itself reports an exact matching task.
SPECIALIST_TASKS: dict[str, tuple[str, int]] = {
    "video-text-to-text": ("video_understanding", 100),
    "video-classification": ("video_classification", 95),
    "object-detection": ("object_detection", 90),
    "zero-shot-object-detection": ("object_detection", 88),
    "image-segmentation": ("image_segmentation", 85),
    "depth-estimation": ("depth_estimation", 82),
    "audio-classification": ("audio_understanding", 80),
    "audio-text-to-text": ("audio_understanding", 78),
    "document-question-answering": ("document_question_answering", 76),
    "visual-question-answering": ("specialized_multimodal", 74),
    "image-text-to-text": ("specialized_multimodal", 72),
    "text-to-video": ("text_to_video", 65),
}

CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {
    "Chat": ("chat", "conversational", "text-generation"),
    "ASR": ("automatic-speech-recognition",),
    "TTS": ("text-to-speech",),
    "Audio understanding": ("audio-classification", "audio-text-to-text"),
    "Image-to-text": ("image-to-text", "image-text-to-text"),
    "Object detection": ("object-detection", "zero-shot-object-detection"),
    "Segmentation": ("image-segmentation",),
    "Depth": ("depth-estimation",),
    "Video understanding": ("video-text-to-text",),
    "Video classification": ("video-classification",),
    "Text-to-video": ("text-to-video",),
    "Document QA": ("document-question-answering",),
    "Translation": ("translation",),
    "Embeddings/features": ("feature-extraction", "sentence-similarity"),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat().replace("+00:00", "Z")


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_text_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _record(model: ProviderModel) -> dict[str, Any]:
    metadata = model.metadata
    task = str(metadata.get("bytez_task") or "unknown")
    role, priority = SPECIALIST_TASKS.get(task, ("not-routed", 0))
    params = _number(metadata.get("params"))
    # Exact task fit is the dominant factor.  Size offers only a modest,
    # metadata-based tiebreaker, never a claim about benchmarked quality.
    score = priority + (min(params, 70.0) / 70.0 * 10.0 if params is not None else 0.0)
    if model.free_eligible:
        score += 15.0
    if metadata.get("closed_or_paid"):
        score -= 1000.0
    return {
        "provider": "bytez",
        "model_id": model.model_id,
        "task": task,
        "params": metadata.get("params"),
        "meter": metadata.get("meter"),
        "meter_price": metadata.get("meter_price"),
        "ram_required": metadata.get("ram_required"),
        "available": model.available,
        "free_account_eligible": model.free_eligible,
        "free_verified": False,
        "health": "UNVERIFIED",
        "catalog_status": model.status,
        "candidate_role": role,
        "priority": priority,
        "candidate_score": round(score, 3),
        "privacy_allowed": list(metadata.get("privacy_allowed") or ["PUBLIC"]),
        "specialist_only": True,
        "routing_allowed": False,
        "notes": (
            "Excluded: closed/paid-provider route" if metadata.get("closed_or_paid")
            else "Catalog candidate only; meter eligibility does not prove remaining free credit or health."
        ),
    }


def _inventory(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_task = Counter(str(row["task"]) for row in records)
    eligible_by_task = Counter(
        str(row["task"]) for row in records if row["free_account_eligible"]
    )
    by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if row["free_account_eligible"] and row["candidate_role"] != "not-routed":
            by_role[str(row["candidate_role"])].append(row)
    top_candidates = {
        role: sorted(rows, key=lambda row: (-float(row["candidate_score"]), str(row["model_id"])))[:5]
        for role, rows in sorted(by_role.items())
    }
    grouped_counts = {
        label: sum(by_task.get(task, 0) for task in tasks)
        for label, tasks in CAPABILITY_GROUPS.items()
    }
    grouped_eligible_counts = {
        label: sum(eligible_by_task.get(task, 0) for task in tasks)
        for label, tasks in CAPABILITY_GROUPS.items()
    }
    classified_tasks = {task for tasks in CAPABILITY_GROUPS.values() for task in tasks}
    grouped_counts["Other"] = sum(count for task, count in by_task.items() if task not in classified_tasks)
    grouped_eligible_counts["Other"] = sum(
        count for task, count in eligible_by_task.items() if task not in classified_tasks
    )
    return {
        "total_catalog_models": len(records),
        "free_account_eligible_models": sum(bool(row["free_account_eligible"]) for row in records),
        "count_by_task": dict(sorted(by_task.items())),
        "free_eligible_by_task": dict(sorted(eligible_by_task.items())),
        "count_by_capability_group": grouped_counts,
        "free_eligible_by_capability_group": grouped_eligible_counts,
        "top_candidates": top_candidates,
    }


class BytezCatalogManager:
    """Persist and report catalog metadata; explicit refreshes only."""

    def load(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def is_fresh(self) -> bool:
        payload = self.load()
        if not payload or payload.get("state") != "DISCOVERED":
            return False
        raw_time = str(payload.get("refreshed_at") or "")
        try:
            refreshed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        except ValueError:
            return False
        return refreshed + timedelta(hours=REFRESH_HOURS) > _utc_now()

    @staticmethod
    def _policy() -> dict[str, Any]:
        try:
            payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        policies = payload.get("specialist_provider_policy") if isinstance(payload, dict) else {}
        return policies.get("bytez", {}) if isinstance(policies, dict) else {}

    def _update_registry_catalog_metadata(self, summary: dict[str, Any], *, state: str, error: str | None) -> None:
        try:
            registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(registry, dict):
            return
        catalogs = registry.setdefault("catalogs", {})
        if not isinstance(catalogs, dict):
            return
        catalogs["bytez"] = {
            "http_result": 200 if state == "DISCOVERED" else None,
            "error": error,
            "count": summary.get("total_catalog_models"),
            "free_account_eligible": summary.get("free_account_eligible_models"),
            "state": state,
            "specialist_only": True,
            "routing_activation": "awaiting-per-task-health-and-privacy-review",
        }
        _atomic_json_write(REGISTRY_PATH, registry)

    def _write_reports(self, payload: dict[str, Any]) -> None:
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        report = {
            "provider": "bytez",
            "generated_at": _utc_iso(),
            "catalog_refreshed_at": payload.get("refreshed_at"),
            "catalog_state": payload.get("state"),
            "catalog_error": payload.get("error"),
            "policy": self._policy(),
            **summary,
            "top_candidates": summary.get("top_candidates", {}),
            "cache_path": _display_path(CACHE_PATH),
            "inference_calls": 0,
        }
        _atomic_json_write(REPORT_JSON_PATH, report)
        lines = [
            "# Bytez specialist model inventory",
            "",
            "Metadata-only inventory. It does not run Bytez model inference or activate routes.",
            "",
            f"- Catalog state: `{report.get('catalog_state') or 'UNKNOWN'}`",
            f"- Refreshed: `{report.get('catalog_refreshed_at') or 'not available'}`",
            f"- Total catalog models: `{report.get('total_catalog_models') if report.get('total_catalog_models') is not None else 'not available'}`",
            f"- Free-account eligible models: `{report.get('free_account_eligible_models') if report.get('free_account_eligible_models') is not None else 'not available'}`",
            "- Free verified / routing activated: `0` (requires later, explicit per-task health and privacy approval)",
            "- Model-inference calls made by this inventory: `0`",
            "",
            "## Counts by Bytez task",
            "",
            "| Task | Catalog models | Free-account eligible |",
            "| --- | ---: | ---: |",
        ]
        counts = report.get("count_by_task") if isinstance(report.get("count_by_task"), dict) else {}
        eligible = report.get("free_eligible_by_task") if isinstance(report.get("free_eligible_by_task"), dict) else {}
        if counts:
            lines.extend(f"| `{task}` | {count} | {eligible.get(task, 0)} |" for task, count in counts.items())
        else:
            lines.append("| No authenticated catalog available | - | - |")
        lines.extend(["", "## Counts by requested capability group", "", "| Capability group | Catalog models | Free-account eligible |", "| --- | ---: | ---: |"])
        groups = report.get("count_by_capability_group") if isinstance(report.get("count_by_capability_group"), dict) else {}
        eligible_groups = report.get("free_eligible_by_capability_group") if isinstance(report.get("free_eligible_by_capability_group"), dict) else {}
        if groups:
            lines.extend(f"| {label} | {count} | {eligible_groups.get(label, 0)} |" for label, count in groups.items())
        else:
            lines.append("| No authenticated catalog available | - | - |")
        lines.extend(["", "## Top specialist-gap candidates", ""])
        candidates = report.get("top_candidates") if isinstance(report.get("top_candidates"), dict) else {}
        if not candidates:
            lines.append("No candidates are listed until an authenticated Bytez catalog refresh succeeds.")
        else:
            for role, rows in candidates.items():
                lines.extend([
                    f"### {role.replace('_', ' ').title()}",
                    "",
                    "| Model ID | Task | Params | Meter | Free Eligible | Candidate Role | Priority | Notes |",
                    "| --- | --- | ---: | --- | --- | --- | ---: | --- |",
                ])
                lines.extend(
                    "| `{model_id}` | `{task}` | {params} | `{meter}` | {free} | `{role}` | {priority} | {notes} |".format(
                        model_id=row["model_id"], task=row["task"], params=row["params"] if row["params"] is not None else "-",
                        meter=row["meter"] or "-", free="yes" if row["free_account_eligible"] else "no",
                        role=row["candidate_role"], priority=row["priority"], notes=row["notes"],
                    ) for row in rows
                )
                lines.append("")
        if report.get("catalog_error"):
            lines.extend(["## Discovery note", "", f"`{report['catalog_error']}`", ""])
        _atomic_text_write(REPORT_MD_PATH, "\n".join(lines))

    def write_unavailable(self, reason: str) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "provider": "bytez",
            "state": "NOT_DISCOVERED",
            "refreshed_at": None,
            "refresh_after_hours": REFRESH_HOURS,
            "error": reason,
            "records": [],
            "summary": {
                "total_catalog_models": None,
                "free_account_eligible_models": None,
                "count_by_task": {},
                "free_eligible_by_task": {},
                "count_by_capability_group": {},
                "free_eligible_by_capability_group": {},
                "top_candidates": {},
            },
            "inference_calls": 0,
        }
        _atomic_json_write(CACHE_PATH, payload)
        self._update_registry_catalog_metadata(payload["summary"], state="NOT_DISCOVERED", error=reason)
        self._write_reports(payload)
        return payload

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        cached = self.load()
        if not force and self.is_fresh() and cached is not None:
            return cached
        models = BytezProvider().list_models()
        records = sorted((_record(model) for model in models), key=lambda row: (str(row["task"]), str(row["model_id"])))
        payload = {
            "schema_version": 1,
            "provider": "bytez",
            "state": "DISCOVERED",
            "refreshed_at": _utc_iso(),
            "refresh_after_hours": REFRESH_HOURS,
            "error": None,
            "records": records,
            "summary": _inventory(records),
            "inference_calls": 0,
        }
        _atomic_json_write(CACHE_PATH, payload)
        self._update_registry_catalog_metadata(payload["summary"], state="DISCOVERED", error=None)
        self._write_reports(payload)
        return payload


__all__ = ["BytezCatalogManager", "CACHE_PATH", "REPORT_JSON_PATH", "REPORT_MD_PATH", "SPECIALIST_TASKS"]
