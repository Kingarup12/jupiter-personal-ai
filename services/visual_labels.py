"""Camera Live ON-DEMAND visual grounding (one-shot "mark / label / highlight / point out / locate this").

There is no continuous detection: a spatial provider is called ONLY because the user explicitly asked Jupiter to put
something on the screen (see services/visual_intent.py), at most once per logical Live turn. Object boxes come from
Google Cloud Vision object localization (real boxes + scores) and person boxes from Face++ detection. Nothing is
inferred from language. Identity labels exist ONLY for people the user has explicitly enrolled in Jupiter's
consent-gated known-person store; everyone else is "Person". No sensitive attributes, no open-world identification,
no frames are stored (the frame is analysed in memory and dropped).

Coordinates are normalized to the CANONICAL (unmirrored) camera frame; display-space boxes/sides are derived for the
user's (possibly selfie-mirrored) preview.
"""

from __future__ import annotations

import io
import re
import threading
import time
from typing import Any, Callable

MIN_GROUNDING_INTERVAL_S = 1.0         # safety ceiling per user (the Live turn budget is one job per request)
OBJECT_MIN_SCORE = 0.5                 # candidates considered at all
CONFIDENT_SCORE = 0.6                  # a box is only drawn for a confident detection
OWNER_SELF_LABELS = frozenset({"self", "me", "myself", "owner", "boss"})

# Provider names -> concise user-facing labels. Unlisted names are title-cased as given by the detector.
LABEL_MAP = {
    "mobile phone": "Phone", "cell phone": "Phone", "telephone": "Phone",
    "bottle": "Bottle", "water bottle": "Water bottle", "laptop": "Laptop", "computer keyboard": "Keyboard",
    "computer monitor": "Monitor", "person": "Person", "man": "Person", "woman": "Person", "boy": "Person",
    "girl": "Person", "human face": "Person", "tableware": "Dish", "drink": "Drink", "coffee cup": "Cup",
    "mug": "Mug", "glasses": "Glasses", "sunglasses": "Sunglasses", "headphones": "Headphones",
    "computer mouse": "Mouse",
}
# What a spoken target may match (target -> accepted detector labels).
TARGET_SYNONYMS = {
    "phone": {"phone", "mobile phone", "cell phone", "telephone"},
    "mobile": {"phone", "mobile phone", "cell phone"},
    "bottle": {"bottle", "water bottle"},
    "water bottle": {"bottle", "water bottle"},
    "cup": {"cup", "coffee cup", "mug"},
    "mug": {"mug", "cup", "coffee cup"},
    "laptop": {"laptop", "computer"},
    "computer": {"laptop", "computer", "computer monitor"},
    "glasses": {"glasses", "sunglasses"},
    "headphones": {"headphones", "headphone"},
    "person": {"person", "man", "woman", "boy", "girl"},
    "smartphone": {"phone", "mobile phone", "cell phone", "telephone"},
    "mouse": {"mouse", "computer mouse"},
    "keys": {"key", "keys"},
    "television": {"television", "tv"},
    "bag": {"bag", "backpack", "handbag", "luggage & bags"},
    "shoe": {"shoe", "footwear"},
    "plant": {"plant", "houseplant", "flowerpot", "flower"},
    "book": {"book"},
    "pen": {"pen", "pencil"},
    "glass": {"glass", "wine glass"},
}


def _label(name: str) -> str:
    key = " ".join(str(name or "").split()).casefold()
    return LABEL_MAP.get(key) or key.title()


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def target_matches(target: str, detector_name: str) -> bool:
    goal = re.sub(r"^(?:my|the|a|an|your)\s+", "", " ".join(str(target or "").casefold().split()))
    name = " ".join(str(detector_name or "").casefold().split())
    if not goal or not name:
        return False
    if goal == name or goal in TARGET_SYNONYMS.get(name, set()) or name in TARGET_SYNONYMS.get(goal, set()):
        return True
    # "red water bottle" -> "water bottle"/"bottle"; never partial-word matches ("app" != "apple").
    words = goal.split()
    return any(" ".join(words[i:]) in ({name} | TARGET_SYNONYMS.get(name, set())) for i in range(len(words)))


def first_name(person_name: str) -> str:
    return (str(person_name or "").split() or [""])[0][:40]


def person_display_labels(references: list[dict], *, owner_account: bool, owner_first_name: str = "",
                          show_titles: bool = False) -> dict[str, str]:
    """Map enrolled person_name -> overlay label using ONLY user-provided metadata.

    First name by default; duplicate first names use the stored user-provided title (relationship); the real
    owner's own enrollment is shown as "<First> (Boss)". Nothing is inferred."""
    labels: dict[str, str] = {}
    counts: dict[str, int] = {}
    for ref in references:
        counts[first_name(ref.get("person_name", "")).casefold()] = counts.get(
            first_name(ref.get("person_name", "")).casefold(), 0) + 1
    for ref in references:
        name = str(ref.get("person_name") or "")
        first = first_name(name)
        title = " ".join(str(ref.get("relationship") or "").split())[:40]
        is_owner_self = owner_account and (title.casefold() in OWNER_SELF_LABELS
                                           or (owner_first_name and first.casefold() == owner_first_name.casefold()))
        if is_owner_self:
            labels[name] = f"{first} (Boss)"
        elif (counts.get(first.casefold(), 0) > 1 or show_titles) and title and title.casefold() not in OWNER_SELF_LABELS:
            labels[name] = f"{first} ({title.title()})"
        else:
            labels[name] = first
    return labels


def face_memory_state(references: list[dict], *, owner_account: bool, profile_first_name: str = "") -> dict[str, Any]:
    """Truthful known-person enrollment state for "do you remember my face?" (user-provided metadata only)."""
    labels = person_display_labels(references, owner_account=owner_account, owner_first_name=profile_first_name)
    self_ref = next((r for r in references
                     if " ".join(str(r.get("relationship") or "").split()).casefold() in OWNER_SELF_LABELS
                     or (profile_first_name and first_name(r.get("person_name", "")).casefold()
                         == profile_first_name.casefold())), None)
    return {"enrolled_count": len(references), "self_enrolled": bool(self_ref),
            "self_label": labels.get(str(self_ref.get("person_name") or ""), "") if self_ref else ""}


def enrolled_references_readonly(user_id: str, data_dir: str | None = None) -> list[dict]:
    """Enrolled-person metadata for the Live path WITHOUT constructing the biometric store: no key generation, no
    schema writes, no Face++ call. Read-only SQLite; a missing store simply means nobody is enrolled."""
    import sqlite3
    from pathlib import Path
    try:
        if data_dir is None:
            from config.settings import DATA_DIR as data_dir
        path = Path(data_dir) / "face_identity.sqlite3"
        if not path.is_file() or not user_id:
            return []
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=0.5)
        try:
            connection.row_factory = sqlite3.Row
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(face_references)")}
            relationship = "relationship" if "relationship" in columns else "NULL AS relationship"
            rows = connection.execute(
                f"SELECT person_name, {relationship} FROM face_references WHERE user_email=? AND deleted_at IS NULL",
                (str(user_id).strip().lower(),)).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
    except Exception:
        return []


def _short(label: str) -> str:
    value = " ".join(str(label or "").split())[:32]
    return value[:1].upper() + value[1:]


class VisualLabelService:
    """One-shot spatial grounding. Never polled; each call is one explicit user request."""

    def __init__(self, *, vision=None, faces=None, clock: Callable[[], float] = time.monotonic):
        self._vision = vision
        self._faces = faces
        self._clock = clock
        self._lock = threading.Lock()
        self._last_request: dict[str, float] = {}
        self.provider_calls = 0

    def _vision_client(self):
        if self._vision is None:
            from services.specialists import CloudVisionSpecialist
            self._vision = CloudVisionSpecialist()
        return self._vision

    def _face_store(self):
        if self._faces is None:
            from config.settings import DATA_DIR
            from services.face_identity import UserFaceIdentityStore
            self._faces = UserFaceIdentityStore(DATA_DIR)
        return self._faces

    def references(self, user_id: str) -> list[dict]:
        try:
            faces = self._face_store()
            return list(faces.list_references(user_id) or []) if faces else []
        except Exception:
            return []

    def _admit(self, user_id: str) -> bool:
        now = self._clock()
        with self._lock:
            if now - self._last_request.get(user_id, -1e9) < MIN_GROUNDING_INTERVAL_S:
                return False
            self._last_request[user_id] = now
            return True

    @staticmethod
    def _image_size(image: bytes) -> tuple[int, int]:
        try:
            from PIL import Image
            with Image.open(io.BytesIO(image)) as picture:
                return int(picture.width), int(picture.height)
        except Exception:
            return 0, 0

    def _faces_with_labels(self, user_id: str, image: bytes, references: list[dict], *, owner_account: bool,
                           owner_first_name: str, show_titles: bool) -> list[dict[str, Any]]:
        width, height = self._image_size(image)
        labels = person_display_labels(references, owner_account=owner_account,
                                       owner_first_name=owner_first_name, show_titles=show_titles)
        self.provider_calls += 1
        matches = self._face_store().identify_with_boxes(email=user_id, image=image)
        people = []
        for match in matches or []:
            rect = match.get("rect") or {}
            if not width or not height or not rect:
                continue
            known = match.get("state") == "KNOWN" and bool(match.get("person_name"))
            people.append({
                "label": labels.get(match.get("person_name") or "", "Person") if known else "Person",
                "known": known, "person": first_name(match.get("person_name") or "") if known else "",
                "box": {"x": round(_clamp01(rect["left"] / width), 4), "y": round(_clamp01(rect["top"] / height), 4),
                        "w": round(_clamp01(rect["width"] / width), 4), "h": round(_clamp01(rect["height"] / height), 4)},
            })
        return people

    def ground(self, *, user_id: str, image: bytes | None, intent, owner_account: bool = False,
               owner_first_name: str = "", mirrored: bool = False, show_titles: bool = False) -> dict[str, Any]:
        """ONE bounded grounding job for an explicit visual action. Returns a result for the SAME Live turn."""
        from services.visual_intent import display_box, screen_side
        started = time.monotonic()
        target = intent.person or intent.display or intent.canonical or intent.target or "it"
        result: dict[str, Any] = {"action": intent.action, "target": target, "success": False, "reason": "",
                                  "displayed_label": intent.display or intent.person or "", "overlay_label": "",
                                  "box": None, "display_box": None, "raw_side": "", "display_side": "",
                                  "box_displayed": False, "provider": "", "latency_ms": 0, "kind": "object",
                                  "mirrored": bool(mirrored), "side_query": bool(getattr(intent, "side_query", False))}

        def finish(**updates):
            result.update(updates)
            if result.get("box") and result.get("success"):
                result["raw_side"], result["display_side"] = screen_side(result["box"], mirrored=mirrored)
                result["display_box"] = display_box(result["box"], mirrored=mirrored)
                result["box_displayed"] = True
            result["latency_ms"] = int((time.monotonic() - started) * 1000)
            return result

        if not image or len(image) > 1_500_000:
            return finish(reason="no_frame")
        if not self._admit(user_id):
            return finish(reason="throttled")
        references = self.references(user_id)
        if intent.person:
            result.update(kind="person", provider="faceplusplus")
            if not any(first_name(r.get("person_name", "")).casefold() == intent.person.casefold() for r in references):
                return finish(reason="not_enrolled")          # never searches arbitrary people
            try:
                people = self._faces_with_labels(user_id, image, references, owner_account=owner_account,
                                                 owner_first_name=owner_first_name, show_titles=show_titles)
            except Exception:
                return finish(reason="unavailable")
            hit = next((p for p in people if p["known"] and p["person"].casefold() == intent.person.casefold()), None)
            if not hit:
                return finish(reason="person_not_found")
            return finish(success=True, box=hit["box"], overlay_label=hit["label"], displayed_label=hit["label"])
        result["provider"] = "google_cloud_vision"
        try:
            self.provider_calls += 1
            detections = self._vision_client().localize_objects(image)
        except Exception as error:
            text = str(error).casefold()
            return finish(reason="quota" if ("quota" in text or "billing" in text or "429" in text) else "unavailable")
        objects = []
        for item in detections or []:
            score = float(item.get("score") or 0)
            box = item.get("box") or {}
            if score < OBJECT_MIN_SCORE or not box:
                continue
            objects.append({"label": _label(item.get("name", "")), "name": str(item.get("name") or ""),
                            "score": round(score, 3), "box": {k: round(_clamp01(box.get(k, 0)), 4) for k in ("x", "y", "w", "h")}})
        goal = intent.canonical
        if goal == "person":
            persons = [o for o in objects if o["label"] == "Person" and o["score"] >= CONFIDENT_SCORE]
            hit = max(persons, key=lambda o: o["box"]["w"] * o["box"]["h"], default=None)
            if not hit:
                return finish(reason="not_found", kind="person")
            label = "Person"
            if references and owner_account:
                try:
                    people = self._faces_with_labels(user_id, image, references, owner_account=owner_account,
                                                     owner_first_name=owner_first_name, show_titles=show_titles)
                    me = next((p for p in people if p["known"] and "(Boss)" in p["label"]), None)
                    label = me["label"] if me else label
                except Exception:
                    pass                                       # recognition failure is non-fatal: still "Person"
            return finish(success=True, box=hit["box"], overlay_label=label, displayed_label=label, kind="person")
        if goal:
            candidates = [o for o in objects if target_matches(goal, o["name"]) or target_matches(goal, o["label"])
                          or (intent.target and target_matches(intent.target, o["name"]))]
        elif intent.held:
            # "Label what I'm holding" with nothing mentioned yet: the most prominent confident non-person object.
            candidates = [o for o in objects if o["label"] != "Person"]
            candidates = sorted(candidates, key=lambda o: o["score"] * (o["box"]["w"] * o["box"]["h"]) ** 0.5)[-1:]
        else:
            return finish(reason="unresolved")
        hit = max(candidates, key=lambda o: o["score"], default=None)
        if not hit:
            return finish(reason="not_found")
        if hit["score"] < CONFIDENT_SCORE:
            return finish(reason="low_confidence")             # never claim a low-confidence find
        display = intent.display if intent.display and len(intent.display.split()) <= 3 else ""
        return finish(success=True, box=hit["box"], overlay_label=_short(display or hit["label"]),
                      displayed_label=display or hit["label"].lower())


_service: VisualLabelService | None = None


def visual_label_service() -> VisualLabelService:
    global _service
    if _service is None:
        _service = VisualLabelService()
    return _service
