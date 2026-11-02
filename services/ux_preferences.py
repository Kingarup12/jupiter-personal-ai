"""Bounded per-user UX preferences.

Only allowlisted keys with enumerated values are ever stored — never CSS, JavaScript, HTML, raw conversation text
or anything that could change Jupiter's source. Storage is per user (DATA_DIR/users/<id>/ux_preferences.json, the
same layout as the personality store), so one user's preference can never affect another user.

`classify_ux_request` separates:
  USER_PREFERENCE        a safe personal presentation setting → stored for this user only
  GLOBAL_PRODUCT_ISSUE   a bug/default problem for everyone → never stored as a preference
  INVALID                arbitrary code/CSS/self-modification or nonsensical settings → refused
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config.settings import DATA_DIR, USERS_DIR, _safe_user_id

ALLOWED: dict[str, tuple[Any, ...]] = {
    "calculator_density": ("standard", "compact", "expanded"),
    "calculator_large_result": (False, True),
    "disable_auto_components": (False, True),
    "live_transcript_visible": (True, False),
    "live_transcript_size": ("medium", "small", "large"),
}
DEFAULTS: dict[str, Any] = {key: values[0] for key, values in ALLOWED.items()}


def _path(user_id: str) -> Path:
    return Path(USERS_DIR) / _safe_user_id(user_id) / "ux_preferences.json"


def sanitize(values: Any) -> dict[str, Any]:
    """Keep only allowlisted keys whose value is one of the enumerated options."""
    if not isinstance(values, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, value in values.items():
        options = ALLOWED.get(str(key))
        if options is not None and any(value is option or (value == option and type(value) is type(option))
                                       for option in options):
            clean[str(key)] = value
    return clean


def ux_preferences_for(user_id: str) -> dict[str, Any]:
    if not user_id:
        return dict(DEFAULTS)
    try:
        stored = json.loads(_path(user_id).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        stored = {}
    return {**DEFAULTS, **sanitize(stored.get("preferences") if isinstance(stored, dict) else {})}


def update_ux_preferences(user_id: str, updates: Any) -> dict[str, Any]:
    if not user_id:
        raise ValueError("A signed-in user is required for personal preferences.")
    clean = sanitize(updates)
    merged = {**ux_preferences_for(user_id), **clean}
    from storage.atomic_json import write_json
    path = _path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"schema_version": 1, "preferences": {k: merged[k] for k in ALLOWED}})
    return merged


# ── request classification ─────────────────────────────────────────────────────────────────────────────────────
_CODE_OR_SELF_MODIFY = re.compile(
    r"\b(?:css|javascript|\bjs\b|html|stylesheet|inject|<script|script tag|eval|execute this code|run this code|"
    r"rewrite (?:your|jupiter'?s?) (?:source|code)|change (?:your|jupiter'?s?) (?:source|code)|modify (?:your|the app'?s?) code|"
    r"#[0-9a-f]{3,8}\b|font-family|z-index)", re.I)
_PROGRAMMING_QUESTION = re.compile(r"\b(?:how (?:do|can|would) i|in (?:python|java|react|flutter|css|html)|write (?:a|the) (?:code|program))\b", re.I)
_GLOBAL_ISSUE = re.compile(r"\b(?:bug|broken|wrong (?:result|answer)|not working|doesn'?t work|crash(?:es|ed)?|glitch|"
                           r"for everyone|all users|by default for everybody)\b", re.I)
_UI_TARGET = re.compile(r"\b(?:calculator|calc|result|transcripts?|transcriptions?|captions?|subtitles?|component|widget)\b",
                        re.I)


def classify_ux_request(text: str) -> dict[str, Any] | None:
    """Return {'kind', 'updates', 'summary'} for a UX customisation request, or None if it is not one."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not t or len(t) > 400 or not _UI_TARGET.search(t):
        return None
    if _PROGRAMMING_QUESTION.search(t):
        return None                                   # a coding question about calculators, not a Jupiter setting
    directive = re.search(r"\b(?:make|set|keep|show|hide|don'?t|do not|stop|turn|switch|use|always|never|i want|i prefer|"
                          r"fix|rewrite|change|modify|edit|update|"
                          r"please|can you|could you)\b", t)
    if not directive:
        return None
    if _CODE_OR_SELF_MODIFY.search(t):
        return {"kind": "INVALID", "updates": {},
                "summary": "I can't store custom code, CSS or changes to Jupiter itself. I can make the calculator "
                           "compact or expanded, enlarge results, stop opening tools automatically, or show/hide the "
                           "Live transcript and change its text size."}
    if _GLOBAL_ISSUE.search(t):
        return {"kind": "GLOBAL_PRODUCT_ISSUE", "updates": {}, "summary": ""}
    updates: dict[str, Any] = {}
    if re.search(r"\bcalc(?:ulator)?\b", t):
        if re.search(r"\b(?:compact|smaller|small|tiny|minimal|narrow)\b", t) and not re.search(r"\bresult\b", t):
            updates["calculator_density"] = "compact"
        elif re.search(r"\b(?:expanded|bigger|larger|wider|big|full)\b", t) and not re.search(r"\bresult\b", t):
            updates["calculator_density"] = "expanded"
        elif re.search(r"\b(?:normal|standard|default|regular) (?:size|calculator|layout)\b|\breset\b", t):
            updates["calculator_density"] = "standard"
    if re.search(r"\b(?:result|answer|display|number)s?\b", t) and re.search(r"\b(?:larger|bigger|big|large)\b", t):
        updates["calculator_large_result"] = True
    elif re.search(r"\b(?:result|answer|display)s?\b", t) and re.search(r"\b(?:normal|smaller|default)\b", t):
        updates["calculator_large_result"] = False
    no_auto = re.search(r"\b(?:don'?t|do not|stop|never|no longer)\b.{0,40}\b(?:show|open|pop up|display|auto)", t)
    if no_auto and re.search(r"\b(?:automatically|auto(?:matic)?|by itself|on its own|for coding|every time|unless i ask)\b", t):
        updates["disable_auto_components"] = True
    elif re.search(r"\b(?:show|open)\b.{0,30}\b(?:automatically|again)\b", t) and re.search(r"\b(?:calculator|component|widget)s?\b", t):
        updates["disable_auto_components"] = False
    if re.search(r"\b(?:transcripts?|transcriptions?|captions?|subtitles?)\b", t):
        if re.search(r"\b(?:hide|turn off|disable|remove|no)\b", t):
            updates["live_transcript_visible"] = False
        elif re.search(r"\b(?:show|turn on|enable|display)\b", t):
            updates["live_transcript_visible"] = True
        if re.search(r"\b(?:smaller|small|tiny)\b", t):
            updates["live_transcript_size"] = "small"
        elif re.search(r"\b(?:larger|bigger|big|large)\b", t):
            updates["live_transcript_size"] = "large"
        elif re.search(r"\b(?:normal|medium|default) (?:size|text)\b", t):
            updates["live_transcript_size"] = "medium"
    updates = sanitize(updates)
    if not updates:
        return None
    return {"kind": "USER_PREFERENCE", "updates": updates, "summary": _describe(updates)}


def _describe(updates: dict[str, Any]) -> str:
    parts = []
    if "calculator_density" in updates:
        parts.append(f"the calculator is now {updates['calculator_density']}")
    if "calculator_large_result" in updates:
        parts.append("calculator results are shown larger" if updates["calculator_large_result"]
                     else "calculator results use the normal size")
    if "disable_auto_components" in updates:
        parts.append("I won't open tools like the calculator automatically — just ask when you want one"
                     if updates["disable_auto_components"] else "I'll open tools like the calculator automatically again")
    if "live_transcript_visible" in updates:
        parts.append("the Live transcript is " + ("shown" if updates["live_transcript_visible"] else "hidden"))
    if "live_transcript_size" in updates:
        parts.append(f"Live transcript text is {updates['live_transcript_size']}")
    return "Done — " + "; ".join(parts) + ". This only applies to your account."
