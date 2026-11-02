"""Short-lived speech-recognition aliases, separate from user biography memory."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from config.settings import DATA_DIR, USERS_DIR, _safe_user_id
from memory.types import add_calendar_months


class RecognitionLexicon:
    def __init__(self, user_id: str):
        self.user_id = _safe_user_id(user_id)
        self.path = USERS_DIR / self.user_id / "recognition_aliases.json"

    def _load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        now = datetime.now()
        active = []
        for item in payload if isinstance(payload, list) else []:
            try:
                expires = datetime.fromisoformat(str(item.get("expires_at") or ""))
            except (TypeError, ValueError):
                continue
            if expires > now:
                active.append(item)
        if len(active) != len(payload):
            self._save(active)
        return active

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(items[-200:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def active_aliases(self) -> dict[str, str]:
        return {
            str(item.get("heard") or "").casefold(): str(item.get("canonical") or "")
            for item in self._load()
            if item.get("heard") and item.get("canonical") and float(item.get("confidence") or 0) >= 0.9
        }

    def record(
        self,
        *,
        heard: str,
        canonical: str,
        confidence: float,
        reason: str,
        source_chat_id: str | None = None,
    ) -> bool:
        heard = " ".join(str(heard or "").split())[:80]
        canonical = " ".join(str(canonical or "").split())[:80]
        confidence = min(1.0, max(0.0, float(confidence or 0)))
        if not heard or not canonical or heard.casefold() == canonical.casefold() or confidence < 0.9:
            return False
        items = self._load()
        now = datetime.now()
        replacement = {
            "heard": heard,
            "canonical": canonical,
            "confidence": confidence,
            "reason": str(reason or "confirmed contextual correction")[:160],
            "source_chat_id": str(source_chat_id or "")[:180] or None,
            "updated_at": now.isoformat(),
            "expires_at": add_calendar_months(now, 3).isoformat(),
        }
        items = [
            item for item in items
            if str(item.get("heard") or "").casefold() != heard.casefold()
        ]
        items.append(replacement)
        self._save(items)
        return True

    def observe(
        self,
        *,
        heard: str,
        canonical: str,
        confidence: float,
        reason: str,
        source_chat_id: str | None = None,
    ) -> bool:
        """Accumulate conservative edit evidence before activating an alias.

        A single composer edit can be a rewrite rather than an ASR correction,
        so it remains inactive. Two consistent, bounded observations raise the
        alias above the activation threshold. Conflicting observations never
        replace an already confirmed mapping.
        """

        heard = " ".join(str(heard or "").split())[:80]
        canonical = " ".join(str(canonical or "").split())[:80]
        confidence = min(0.89, max(0.0, float(confidence or 0)))
        if (
            not heard or not canonical
            or heard.casefold() == canonical.casefold()
            or confidence < 0.7
        ):
            return False
        items = self._load()
        existing = next(
            (item for item in items if str(item.get("heard") or "").casefold() == heard.casefold()),
            None,
        )
        if existing and str(existing.get("canonical") or "").casefold() != canonical.casefold():
            if float(existing.get("confidence") or 0) >= 0.9:
                return False
            evidence_count = 1
        else:
            evidence_count = int((existing or {}).get("evidence_count") or 0) + 1
        learned_confidence = min(0.97, confidence + 0.16 * max(0, evidence_count - 1))
        now = datetime.now()
        replacement = {
            "heard": heard,
            "canonical": canonical,
            "confidence": round(learned_confidence, 3),
            "evidence_count": evidence_count,
            "reason": str(reason or "repeated composer correction")[:160],
            "source_chat_id": str(source_chat_id or "")[:180] or None,
            "updated_at": now.isoformat(),
            "expires_at": add_calendar_months(now, 3).isoformat(),
        }
        items = [
            item for item in items
            if str(item.get("heard") or "").casefold() != heard.casefold()
        ]
        items.append(replacement)
        self._save(items)
        return learned_confidence >= 0.9

    def remove_chat(self, chat_id: str) -> int:
        items = self._load()
        remaining = [item for item in items if str(item.get("source_chat_id") or "") != str(chat_id)]
        removed = len(items) - len(remaining)
        if removed:
            self._save(remaining)
        return removed
