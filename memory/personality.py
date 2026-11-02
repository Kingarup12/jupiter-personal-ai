"""Canonical, user-scoped response-personality preferences.

Personality describes *how Jupiter should respond*.  Durable facts about the
user live in P0-P3 memory and are never mirrored into this store.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import DATA_DIR, USERS_DIR, _safe_user_id


PERSONALITY_CATEGORIES = (
    "base_style",
    "warm",
    "enthusiastic",
    "headers_lists",
    "emoji",
    "humor",
    "sarcasm",
    "conversational_style",
    "response_length",
    "explanation_depth",
    "examples",
    "technical_depth",
    "language_style",
    "custom_instruction",
    "other",
)

PERSONALITY_LABELS = {
    "base_style": "Base style and tone",
    "warm": "Warm",
    "enthusiastic": "Enthusiastic",
    "headers_lists": "Headers & lists",
    "emoji": "Emoji",
    "humor": "Humor",
    "sarcasm": "Sarcasm",
    "conversational_style": "Conversational style",
    "response_length": "Response length",
    "explanation_depth": "Explanation",
    "examples": "Examples",
    "technical_depth": "Technical depth",
    "language_style": "Language style",
    "custom_instruction": "Custom instruction",
    "other": "Other learned behavior",
}

PERSONALITY_DEFAULTS = {
    "base_style": "Default",
    "warm": "More",
    "enthusiastic": "More",
    "headers_lists": "Default",
    "emoji": "More",
    "humor": "More",
    "sarcasm": "Adaptive",
    "conversational_style": "Natural",
    "response_length": "Balanced",
    "explanation_depth": "Direct",
    "examples": "When helpful",
    "technical_depth": "Adaptive",
}

PERSONALITY_OPTIONS = {
    "base_style": {"Default", "Professional", "Friendly", "Candid", "Quirky", "Efficient", "Cynical"},
    "warm": {"More", "Default", "Less"},
    "enthusiastic": {"More", "Default", "Less"},
    "headers_lists": {"More", "Default", "Less"},
    "emoji": {"More", "Default", "Less"},
    "humor": {"Less", "Default", "More"},
    "sarcasm": {"Off", "Light", "Adaptive", "More"},
    "conversational_style": {"Professional", "Natural", "Casual"},
    "response_length": {"Concise", "Balanced", "Detailed"},
    "explanation_depth": {"Direct", "Step-by-step", "Deep explanation"},
    "examples": {"Rarely", "When helpful", "Frequently"},
    "technical_depth": {"Simple", "Adaptive", "Advanced"},
}

LEGACY_CATEGORY_MAP = {
    "tone": "base_style",
    "emoji_level": "emoji",
    "formatting": "headers_lists",
    "explanation_style": "explanation_depth",
}


def _legacy_value(category: str, value: str) -> str:
    normalized = value.casefold()
    if category == "base_style":
        if "formal" in normalized:
            return "Professional"
        if "friend" in normalized or "casual" in normalized:
            return "Friendly"
    if category in {"emoji", "headers_lists"}:
        if any(term in normalized for term in ("minimal", "avoid", "prose")):
            return "Less"
        if any(term in normalized for term in ("playful", "bullet", "useful")):
            return "More"
    if category == "explanation_depth":
        if "step" in normalized:
            return "Step-by-step"
        if any(term in normalized for term in ("deep", "thorough", "detail")):
            return "Deep explanation"
        return "Direct"
    return value


class PersonalityStore:
    """One structured source of truth with replace-on-update semantics."""

    DEFAULT = {
        "schema_version": 2,
        "preferences": {},
        "history": [],
        "updated_at": None,
    }

    def __init__(self, user_id: str, path: Path | None = None):
        self.user_id = _safe_user_id(user_id)
        self.path = Path(path or (USERS_DIR / self.user_id / "personality.json"))

    def load(self) -> dict[str, Any]:
        payload = deepcopy(self.DEFAULT)
        loaded: dict[str, Any] = {}
        if self.path.is_file():
            try:
                candidate = json.loads(self.path.read_text(encoding="utf-8"))
                loaded = candidate if isinstance(candidate, dict) else {}
            except (OSError, ValueError, TypeError):
                loaded = {}
        preferences = loaded.get("preferences")
        if isinstance(preferences, dict):
            for source_key, entry in preferences.items():
                category = LEGACY_CATEGORY_MAP.get(source_key, source_key)
                if category not in PERSONALITY_CATEGORIES or not isinstance(entry, dict):
                    continue
                value = _legacy_value(category, str(entry.get("value") or "").strip())
                if value:
                    payload["preferences"][category] = {**entry, "value": value}
        for category, value in PERSONALITY_DEFAULTS.items():
            payload["preferences"].setdefault(category, {
                "value": value,
                "confidence": 1.0,
                "source": "jupiter:default-personality",
                "updated_at": None,
                "user_confirmed": False,
                "authority": "default",
            })
        history = loaded.get("history")
        if isinstance(history, list):
            payload["history"] = [item for item in history[-100:] if isinstance(item, dict)]
        payload["updated_at"] = loaded.get("updated_at")
        return payload

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        clean = deepcopy(self.DEFAULT)
        clean["preferences"] = dict(payload.get("preferences") or {})
        clean["history"] = list(payload.get("history") or [])[-100:]
        clean["updated_at"] = datetime.now().isoformat()
        from storage.atomic_json import write_json
        write_json(self.path, clean)
        return clean

    @staticmethod
    def parse_explicit(message: str) -> dict[str, str]:
        """Extract only direct, high-confidence response-style instructions."""

        text = " ".join(str(message or "").split())
        lowered = text.casefold()
        changes: dict[str, str] = {}

        concise = (
            r"\b(?:reply|answer|respond|responses?|replies)\b.{0,24}\b(?:short|brief|briefly|concise)\b",
            r"\b(?:keep it short|be concise|short answers?|brief answers?)\b",
            r"\b(?:chhota|short)\s+(?:reply|answer|jawab)\b",
        )
        detailed = (
            r"\b(?:reply|answer|respond|explain)\b.{0,24}\b(?:longer|detailed|in detail|thorough)\b",
            r"\b(?:give me|use)\s+(?:longer|detailed)\s+(?:answers?|explanations?)\b",
        )
        if any(re.search(pattern, lowered) for pattern in concise):
            changes["response_length"] = "Concise"
        elif any(re.search(pattern, lowered) for pattern in detailed):
            changes["response_length"] = "Detailed"

        if re.search(r"\b(?:talk|speak|reply|respond|sound)\b.{0,24}\b(?:casual|casually|like a friend|friendly)\b", lowered):
            changes["base_style"] = "Friendly"
        elif re.search(r"\b(?:talk|speak|reply|respond|sound|be)\b.{0,18}\bformal(?:ly)?\b", lowered):
            changes["base_style"] = "Professional"

        if re.search(r"\b(?:use|speak|reply|talk|bolo|baat)\b.{0,28}\bhinglish\b|\bhinglish\b.{0,28}\b(?:use|speak|reply|talk|karo)\b", lowered):
            changes["language_style"] = "Hinglish when appropriate"
        elif re.search(r"\b(?:use|speak|reply|talk)\b.{0,24}\benglish\b", lowered):
            changes["language_style"] = "English"
        elif re.search(r"\b(?:use|speak|reply|talk|bolo|baat)\b.{0,24}\bbengali\b", lowered):
            changes["language_style"] = "Bengali when appropriate"
        elif re.search(r"\b(?:use|speak|reply|talk|bolo|baat)\b.{0,24}\bhindi\b", lowered):
            changes["language_style"] = "Hindi when appropriate"

        if re.search(r"\b(?:explain|show|teach)\b.{0,28}\b(?:step[- ]by[- ]step|in steps)\b", lowered):
            changes["explanation_depth"] = "Step-by-step"
        elif re.search(r"\b(?:keep|make)\b.{0,18}\b(?:advice|explanations?)\b.{0,18}\bpractical\b|\bpractical\s+(?:advice|explanations?)\b", lowered):
            changes["explanation_depth"] = "Direct"
        elif re.search(r"\b(?:explain|teach)\b.{0,24}\b(?:simply|simple terms|easy terms)\b", lowered):
            changes["technical_depth"] = "Simple"

        if re.search(r"\b(?:no|avoid|don't use|do not use|fewer|less|not too many)\b.{0,16}\bemojis?\b", lowered):
            changes["emoji"] = "Less"
        elif re.search(r"\b(?:use|add|more)\b.{0,16}\bemojis?\b", lowered):
            changes["emoji"] = "More"

        if re.search(r"\b(?:use|format as|reply with)\b.{0,18}\b(?:bullet points?|bullets?)\b", lowered):
            changes["headers_lists"] = "More"
        elif re.search(r"\b(?:avoid|don't use|do not use)\b.{0,18}\b(?:bullet points?|bullets?)\b", lowered):
            changes["headers_lists"] = "Less"
        if re.search(r"\b(?:more|be more)\s+(?:warm|caring|empathetic)\b", lowered):
            changes["warm"] = "More"
        elif re.search(r"\b(?:less|not so)\s+(?:warm|caring|empathetic)\b", lowered):
            changes["warm"] = "Less"
        if re.search(r"\b(?:more|be more)\s+(?:enthusiastic|energetic|excited)\b", lowered):
            changes["enthusiastic"] = "More"
        elif re.search(r"\b(?:less|not so)\s+(?:enthusiastic|energetic|excited)\b", lowered):
            changes["enthusiastic"] = "Less"
        if re.search(r"\b(?:more|be more)\s+(?:funny|humorous|witty)\b", lowered):
            changes["humor"] = "More"
        elif re.search(r"\b(?:less|not so)\s+(?:funny|humorous|witty)\b|\b(?:avoid|no)\s+(?:humou?r|jokes?)\b", lowered):
            changes["humor"] = "Less"
        if re.search(r"\b(?:avoid|no|turn off)\s+sarcasm\b", lowered):
            changes["sarcasm"] = "Off"
        elif re.search(r"\b(?:more|be more)\s+sarcastic\b", lowered):
            changes["sarcasm"] = "More"
        elif re.search(r"\b(?:light|subtle)\s+sarcasm\b", lowered):
            changes["sarcasm"] = "Light"
        if re.search(r"\b(?:more|be more)\s+(?:casual|informal)\b", lowered):
            changes["conversational_style"] = "Casual"
        elif re.search(r"\b(?:more|be more)\s+(?:professional|formal)\b", lowered):
            changes["conversational_style"] = "Professional"
        elif re.search(r"\b(?:sound|talk|speak|reply)\b.{0,18}\b(?:natural|naturally)\b", lowered):
            changes["conversational_style"] = "Natural"
        if re.search(r"\b(?:use|give)\s+(?:more|frequent)\s+examples?\b", lowered):
            changes["examples"] = "Frequently"
        elif re.search(r"\b(?:avoid|rarely use|fewer)\s+examples?\b", lowered):
            changes["examples"] = "Rarely"
        if re.search(r"\b(?:advanced|expert-level|more technical)\b", lowered):
            changes["technical_depth"] = "Advanced"
        return changes

    @staticmethod
    def is_ambiguous_instruction(message: str) -> bool:
        lowered = " ".join(str(message or "").casefold().split())
        return bool(re.search(
            r"\b(?:i liked|keep doing that|answer like before|same style|the way you answered|continue like that)\b",
            lowered,
        ))

    def update(
        self,
        changes: dict[str, Any],
        *,
        source: str,
        confidence: float = 1.0,
        user_confirmed: bool = False,
        authority: str = "explicit_user",
    ) -> dict[str, Any]:
        payload = self.load()
        now = datetime.now().isoformat()
        for category, raw_value in changes.items():
            category = LEGACY_CATEGORY_MAP.get(category, category)
            if category not in PERSONALITY_CATEGORIES:
                continue
            value = _legacy_value(category, " ".join(str(raw_value or "").split())[:1000])
            if category in PERSONALITY_OPTIONS and value not in PERSONALITY_OPTIONS[category]:
                continue
            if not value:
                continue
            previous = payload["preferences"].get(category)
            if previous and previous.get("authority") == "manual_edit" and authority != "manual_edit":
                continue
            if previous and str(previous.get("value") or "").casefold() == value.casefold():
                continue
            payload["preferences"][category] = {
                "value": value,
                "confidence": min(1.0, max(0.0, float(confidence))),
                "source": str(source or "explicit_user")[:300],
                "updated_at": now,
                "user_confirmed": bool(user_confirmed),
                "authority": str(authority or "explicit_user")[:40],
            }
            payload["history"].append({
                "category": category,
                "value": value,
                "previous_value": previous.get("value") if previous else None,
                "source": str(source or "explicit_user")[:300],
                "confidence": min(1.0, max(0.0, float(confidence))),
                "user_confirmed": bool(user_confirmed),
                "authority": str(authority or "explicit_user")[:40],
                "updated_at": now,
            })
        return self.save(payload)

    def observe_explicit(self, message: str, *, source: str) -> dict[str, str]:
        changes = self.parse_explicit(message)
        if changes:
            self.update(changes, source=source, confidence=1.0, user_confirmed=True)
        return changes

    def update_from_memory(self, memory) -> dict[str, Any]:
        attributes = memory.attributes if isinstance(memory.attributes, dict) else {}
        changes = dict(attributes.get("personality_preferences") or {})
        changes.update(dict(attributes.get("communication_style") or {}))
        key_map = {
            "reply_length": "response_length",
            "formality": "base_style",
            "preferred_language": "language_style",
            "emoji_level": "emoji",
            "formatting": "headers_lists",
            "explanation_style": "explanation_depth",
        }
        for source_key, target_key in key_map.items():
            if attributes.get(source_key) is not None:
                changes[target_key] = attributes[source_key]
        normalized = {
            key_map.get(key, key): value
            for key, value in changes.items()
            if key_map.get(key, key) in PERSONALITY_CATEGORIES and value is not None
        }
        if not normalized:
            normalized = self.parse_explicit(str(getattr(memory, "summary", "") or ""))
        return self.update(
            normalized,
            source=str(getattr(memory, "source", "") or "memory"),
            confidence=float(getattr(memory, "confidence", 0.8) or 0.8),
            user_confirmed=not bool(attributes.get("inferred", False)),
        )

    def set_manual(self, changes: dict[str, Any]) -> dict[str, Any]:
        payload = self.load()
        now = datetime.now().isoformat()
        for category, raw_value in changes.items():
            category = LEGACY_CATEGORY_MAP.get(category, category)
            if category not in PERSONALITY_CATEGORIES:
                continue
            value = _legacy_value(category, " ".join(str(raw_value or "").split())[:1000])
            if category in PERSONALITY_OPTIONS and value not in PERSONALITY_OPTIONS[category]:
                continue
            previous = payload["preferences"].get(category)
            if value:
                payload["preferences"][category] = {
                    "value": value,
                    "confidence": 1.0,
                    "source": "settings:personality",
                    "updated_at": now,
                    "user_confirmed": True,
                    "authority": "manual_edit",
                }
            else:
                payload["preferences"].pop(category, None)
            if previous and str(previous.get("value") or "") != value:
                payload["history"].append({
                    "category": category,
                    "value": value,
                    "previous_value": previous.get("value"),
                    "source": "settings:personality",
                    "confidence": 1.0,
                    "user_confirmed": True,
                    "authority": "manual_edit",
                    "updated_at": now,
                })
        return self.save(payload)

    def reset_structured_defaults(self) -> dict[str, Any]:
        """Reset visible structured controls without touching freeform policy."""

        payload = self.load()
        now = datetime.now().isoformat()
        for category, value in PERSONALITY_DEFAULTS.items():
            previous = payload["preferences"].get(category)
            payload["preferences"][category] = {
                "value": value,
                "confidence": 1.0,
                "source": "settings:personality-defaults",
                "updated_at": now,
                "user_confirmed": True,
                "authority": "manual_edit",
            }
            if not previous or str(previous.get("value") or "") != value:
                payload["history"].append({
                    "category": category,
                    "value": value,
                    "previous_value": previous.get("value") if previous else None,
                    "source": "settings:personality-defaults",
                    "confidence": 1.0,
                    "user_confirmed": True,
                    "authority": "manual_edit",
                    "updated_at": now,
                })
        # custom_instruction, language_style, other, Auto Updates (stored by
        # MemoryService), and the separate Buddy placeholder are preserved.
        return self.save(payload)

    def extract_ambiguous(
        self,
        message: str,
        recent_conversation: list[dict] | None = None,
        *,
        source: str,
    ) -> dict[str, str]:
        """Use the dedicated small/private route only for implicit style feedback."""

        if not self.is_ambiguous_instruction(message):
            return {}
        from core.provider_contracts import AIRequest, PrivacyClass, TaskType
        from services.ai_gateway import AIGateway

        context = "\n".join(
            f"{item.get('role', 'user')}: {str(item.get('content') or '')[:800]}"
            for item in list(recent_conversation or [])[-4:]
            if isinstance(item, dict)
        )
        response = AIGateway().generate(AIRequest(
            request_id=f"personality-{datetime.now().timestamp()}",
            user_id=self.user_id,
            conversation_id=None,
            job_id=None,
            task_type=TaskType.PERSONALITY_EXTRACTION,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract only the response-style preference implied by the latest user "
                        "message. Return one JSON object whose keys may only be base_style, warm, "
                        "enthusiastic, headers_lists, emoji, humor, sarcasm, conversational_style, "
                        "response_length, explanation_depth, examples, technical_depth, "
                        "language_style, custom_instruction, or other. "
                        "Use short plain-string values. Return {} when the preference cannot be "
                        "established. Do not extract biography, identity, relationships, projects, "
                        "or other factual memory."
                    ),
                },
                {
                    "role": "user",
                    "content": (f"Recent context:\n{context}\n\n" if context else "")
                    + f"Latest user message:\n{message}",
                },
            ],
            required_modalities=frozenset({"text"}),
            max_output=350,
            privacy_class=PrivacyClass.PRIVATE,
            no_persist=True,
        ))
        raw = str(response.content or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        changes = {
            key: " ".join(str(value or "").split())[:1000]
            for key, value in parsed.items()
            if key in PERSONALITY_CATEGORIES and isinstance(value, str) and value.strip()
        }
        if changes:
            self.update(
                changes,
                source=source,
                confidence=0.78,
                user_confirmed=False,
                authority="contextual_inference",
            )
        return changes

    def capsule(self, max_items: int = 16) -> str:
        preferences = self.load().get("preferences") or {}
        lines = []
        for category in PERSONALITY_CATEGORIES:
            entry = preferences.get(category)
            value = " ".join(str((entry or {}).get("value") or "").split())
            if value:
                lines.append(f"- {PERSONALITY_LABELS[category]}: {value}")
        selected = lines[:max_items]
        if selected:
            selected.extend([
                "- Compose all settings together; no single preference overrides correctness, safety, or the current context.",
                "- Mirror the language used in the current message (English, Hindi, or Hinglish) unless the user explicitly saved another language preference.",
                "- Humor is permission, not a quota: use it naturally when relevant, and do not force a joke into every reply.",
                "- Reduce humor and sarcasm when the user is frustrated; disable sarcasm and teasing for sensitive, security, privacy, medical, legal, financial, grief, crisis, or otherwise serious contexts.",
                "- Never insult or humiliate the user. Harmless teasing may target a clearly harmless mistake, technology, or the situation—not the user's worth.",
                "- The Emoji preference remains authoritative even when Humor or Sarcasm is high.",
                "- For a simple short message, answer naturally without expanding it into an essay; continue to respect Response length.",
            ])
        return "\n".join(selected)

    def overlaps(self, text: str) -> bool:
        """Conservatively detect a duplicate of an active response preference."""

        normalized = set(re.findall(r"[a-z0-9]+", str(text or "").casefold()))
        if not normalized:
            return False
        for entry in (self.load().get("preferences") or {}).values():
            candidate = set(re.findall(r"[a-z0-9]+", str((entry or {}).get("value") or "").casefold()))
            if not candidate:
                continue
            overlap = len(normalized & candidate) / max(1, len(normalized | candidate))
            if normalized == candidate or overlap >= 0.8:
                return True
        return False

    def public_payload(self) -> dict[str, Any]:
        payload = self.load()
        return {
            "items": [
                {
                    "category": category,
                    "label": PERSONALITY_LABELS[category],
                    **entry,
                }
                for category in PERSONALITY_CATEGORIES
                if isinstance((entry := payload["preferences"].get(category)), dict)
            ],
            "updated_at": payload.get("updated_at"),
        }

    def remove_chat_provenance(self, chat_id: str) -> bool:
        payload = self.load()
        prefix = f"conversation:{chat_id}:"
        removed = [item for item in payload["history"] if str(item.get("source") or "").startswith(prefix)]
        if not removed:
            return False
        remaining = [item for item in payload["history"] if item not in removed]
        for category in {str(item.get("category") or "") for item in removed}:
            latest = next((item for item in reversed(remaining) if item.get("category") == category), None)
            if latest:
                payload["preferences"][category] = {
                    key: latest.get(key)
                    for key in ("value", "confidence", "source", "updated_at", "user_confirmed", "authority")
                }
            else:
                payload["preferences"].pop(category, None)
        payload["history"] = remaining
        self.save(payload)
        return True
