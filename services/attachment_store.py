"""Authenticated, user-scoped attachment storage for Jupiter chat."""

from __future__ import annotations

import base64
import calendar
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from threading import Lock
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from core.security import untrusted_data_block


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_VIDEO_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_VIDEO_BASE64_CHARS = (MAX_VIDEO_ATTACHMENT_BYTES * 4 // 3) + 8
MAX_ATTACHMENTS_PER_MESSAGE = 4
MAX_REPORT_ATTACHMENTS = 8
_ATTACHMENT_ID = re.compile(r"^[a-f0-9]{32}$")
TTS_DOWNLOAD_SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5)
_AUDIO_VARIANT_LOCK = Lock()

_MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".py": "text/plain",
    ".js": "text/plain",
    ".ts": "text/plain",
    ".css": "text/plain",
    ".yml": "text/plain",
    ".yaml": "text/plain",
}
_MODEL_BINARY_MIMES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "application/pdf",
}
_TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "application/xml",
    "text/html",
}
_VIDEO_MIMES = frozenset({"video/mp4", "video/webm", "video/quicktime", "video/x-m4v"})


class AttachmentStoreError(ValueError):
    def __init__(self, message: str, *, code: str = "ATTACHMENT_INVALID"):
        super().__init__(message)
        self.code = code


_IMAGE_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}
_EDITABLE_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_IMAGE_PIXELS = 40_000_000


def _validated_image_mime(raw: bytes, claimed_mime: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                actual_mime = _IMAGE_MIME_BY_FORMAT.get(str(image.format or "").upper())
                if not actual_mime:
                    raise AttachmentStoreError(
                        "This decoded image format is not supported.",
                        code="IMAGE_FORMAT_UNSUPPORTED",
                    )
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise AttachmentStoreError(
                        "The image dimensions are too large.",
                        code="IMAGE_TOO_LARGE",
                    )
                image.verify()
        with Image.open(io.BytesIO(raw)) as decoded:
            decoded.load()
    except AttachmentStoreError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AttachmentStoreError(
            "The image dimensions are too large.", code="IMAGE_TOO_LARGE"
        ) from None
    except (UnidentifiedImageError, OSError, ValueError):
        raise AttachmentStoreError(
            "The selected image could not be decoded.", code="IMAGE_DECODE_FAILED"
        ) from None
    if claimed_mime and actual_mime != claimed_mime:
        raise AttachmentStoreError(
            "The selected image's decoded type does not match its extension.",
            code="IMAGE_TYPE_MISMATCH",
        )
    return actual_mime


def _validated_video_mime(raw: bytes, claimed_mime: str) -> str:
    """Perform a cheap container-header check before local video processing."""

    if claimed_mime == "video/webm":
        if raw.startswith(b"\x1aE\xdf\xa3"):
            return claimed_mime
    elif claimed_mime in {"video/mp4", "video/quicktime", "video/x-m4v"}:
        # ISO base media containers (MP4/MOV/M4V) carry the ftyp box after
        # its four-byte size. This is validation, not a claim of decodability.
        if len(raw) >= 12 and raw[4:8] == b"ftyp":
            return claimed_mime
    raise AttachmentStoreError(
        "The selected video does not match its expected container format.",
        code="VIDEO_DECODE_FAILED",
    )


def _sanitized_generated_image(raw: bytes, claimed_mime: str) -> tuple[bytes, str]:
    actual_mime = _validated_image_mime(raw, claimed_mime)
    if actual_mime not in _EDITABLE_IMAGE_MIMES:
        raise AttachmentStoreError(
            "The image provider returned an unsupported artifact.",
            code="IMAGE_FORMAT_UNSUPPORTED",
        )
    output = io.BytesIO()
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        if actual_mime == "image/jpeg":
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=95, optimize=True)
        elif actual_mime == "image/webp":
            image.save(output, format="WEBP", quality=95, method=4)
        else:
            image.save(output, format="PNG", optimize=True)
    return output.getvalue(), actual_mime


def _data_root() -> Path:
    project_root = Path(__file__).resolve().parent.parent
    configured = os.getenv("JUPITER_DATA_DIR")
    return Path(configured) if configured else project_root / "data"


def _user_key(email: str) -> str:
    normalized = str(email or "").strip().lower()
    if not normalized:
        raise AttachmentStoreError("An authenticated user is required.")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _user_dir(email: str) -> Path:
    directory = _data_root() / "attachments" / _user_key(email)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _display_name(filename: str) -> str:
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char >= " " and char not in "\x7f")
    name = name.strip().strip(".")
    if not name:
        raise AttachmentStoreError("The selected file has no usable filename.")
    return name[:180]


def _add_calendar_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _paths(email: str, attachment_id: str) -> tuple[Path, Path]:
    if not _ATTACHMENT_ID.fullmatch(str(attachment_id or "")):
        raise AttachmentStoreError("Invalid attachment ID.")
    directory = _user_dir(email)
    return directory / f"{attachment_id}.bin", directory / f"{attachment_id}.json"


def _public_metadata(metadata: dict) -> dict:
    public = {
        "id": metadata["id"],
        "name": metadata["name"],
        "mime_type": metadata["mime_type"],
        "size": metadata["size"],
        "kind": (
            "image" if metadata["mime_type"].startswith("image/")
            else "video" if metadata["mime_type"].startswith("video/")
            else "audio" if metadata["mime_type"].startswith("audio/")
            else "file"
        ),
    }
    for key in (
        "safe_filename",
        "created_at",
        "retention_policy",
        "expires_at",
        "status",
        "conversation_id",
        "source_image_id",
        "generated",
        "derivative",
        "synthid",
        "revision_mode",
        "revision_number",
        "image_quality_mode",
        "image_validation",
        "artifact_kind",
        "voice_name",
        "duration_ms",
        "source_text",
    ):
        if metadata.get(key) is not None:
            public[key] = metadata[key]
    return public


def _read_metadata(email: str, attachment_id: str) -> tuple[dict, Path, Path]:
    data_path, metadata_path = _paths(email, attachment_id)
    if not data_path.is_file() or not metadata_path.is_file():
        raise AttachmentStoreError("Attachment not found.")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AttachmentStoreError("Attachment metadata is unavailable.") from error
    if metadata.get("id") != attachment_id or metadata.get("owner") != _user_key(email):
        raise AttachmentStoreError("Attachment not found.")
    return metadata, data_path, metadata_path


def _write_metadata(path: Path, metadata: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def cleanup_staged(email: str, older_than_hours: int = 24) -> None:
    cutoff = datetime.now() - timedelta(hours=older_than_hours)
    for metadata_path in _user_dir(email).glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(metadata.get("created_at", ""))
            if metadata.get("persisted") or created_at >= cutoff:
                continue
            data_path = metadata_path.with_suffix(".bin")
            data_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError):
            continue


def save_upload(email: str, filename: str, mime_type: str, data_base64: str) -> dict:
    cleanup_staged(email)
    name = _display_name(filename)
    extension = Path(name).suffix.lower()
    expected_mime = _MIME_BY_EXTENSION.get(extension)
    if not expected_mime:
        raise AttachmentStoreError(
            "This file type is not supported yet. Use a video, image, PDF, text, Markdown, CSV, JSON, XML, HTML, or code file."
        )
    supplied_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    normalized_mime = expected_mime if supplied_mime in {"", "application/octet-stream"} else supplied_mime
    allowed_for_extension = {expected_mime}
    if extension in {".py", ".js", ".ts", ".css", ".yml", ".yaml"}:
        allowed_for_extension.update({"text/plain", "application/octet-stream"})
        normalized_mime = "text/plain"
    if normalized_mime not in allowed_for_extension:
        raise AttachmentStoreError("The selected file's type does not match its extension.")
    try:
        raw = base64.b64decode(str(data_base64 or ""), validate=True)
    except (ValueError, TypeError) as error:
        raise AttachmentStoreError("The selected file could not be decoded.") from error
    if not raw:
        raise AttachmentStoreError("Empty files cannot be attached.")
    maximum_bytes = MAX_VIDEO_ATTACHMENT_BYTES if normalized_mime in _VIDEO_MIMES else MAX_ATTACHMENT_BYTES
    if len(raw) > maximum_bytes:
        raise AttachmentStoreError(
            "Videos must be 100 MB or smaller." if normalized_mime in _VIDEO_MIMES else "Attachments must be 10 MB or smaller."
        )
    if normalized_mime.startswith("image/"):
        normalized_mime = _validated_image_mime(raw, normalized_mime)
    elif normalized_mime in _VIDEO_MIMES:
        normalized_mime = _validated_video_mime(raw, normalized_mime)

    content_hash = hashlib.sha256(raw).hexdigest()
    owner = _user_key(email)
    # Reuse an owned, byte-identical source artifact. Derived OCR/embedding
    # records key off this content hash and can therefore be reused safely.
    for candidate_path in _user_dir(email).glob("*.json"):
        try:
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            candidate.get("owner") == owner
            and candidate.get("persisted") is False
            and candidate.get("sha256") == content_hash
            and candidate.get("mime_type") == normalized_mime
            and candidate_path.with_suffix(".bin").is_file()
        ):
            return _public_metadata(candidate)

    attachment_id = uuid4().hex
    data_path, metadata_path = _paths(email, attachment_id)
    metadata = {
        "id": attachment_id,
        "owner": owner,
        "name": name,
        "safe_filename": f"{attachment_id}.bin",
        "mime_type": normalized_mime,
        "size": len(raw),
        "sha256": content_hash,
        "status": "ACTIVE",
        "created_at": datetime.now().isoformat(),
        "persisted": False,
        "test_session_id": None,
        "conversation_id": None,
        "conversation_ids": [],
        "retention_policy": "this_chat",
        "expires_at": None,
        "derived_artifacts": [],
    }
    data_path.write_bytes(raw)
    _write_metadata(metadata_path, metadata)
    return _public_metadata(metadata)


def save_report_attachment(email: str, filename: str, mime_type: str, data_base64: str) -> dict:
    """Store an authenticated report file as opaque, non-executable data.

    Reports accept file types beyond the chat/model allow-list.  The bytes are
    still bounded and account-scoped, but are never parsed or rendered by this
    path; the report UI displays metadata only unless a separately safe preview
    is added later.
    """

    cleanup_staged(email)
    name = _display_name(filename)
    supplied_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    normalized_mime = supplied_mime if re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+\-]*/[a-z0-9][a-z0-9!#$&^_.+\-]*", supplied_mime) else "application/octet-stream"
    try:
        raw = base64.b64decode(str(data_base64 or ""), validate=True)
    except (ValueError, TypeError) as error:
        raise AttachmentStoreError("The selected report file could not be decoded.") from error
    if not raw:
        raise AttachmentStoreError("Empty report files cannot be attached.")
    maximum_bytes = MAX_VIDEO_ATTACHMENT_BYTES if normalized_mime in _VIDEO_MIMES else MAX_ATTACHMENT_BYTES
    if len(raw) > maximum_bytes:
        raise AttachmentStoreError(
            "Videos must be 100 MB or smaller." if normalized_mime in _VIDEO_MIMES
            else "Report attachments must be 10 MB or smaller."
        )
    owner = _user_key(email)
    content_hash = hashlib.sha256(raw).hexdigest()
    for candidate_path in _user_dir(email).glob("*.json"):
        try:
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            candidate.get("owner") == owner
            and candidate.get("artifact_kind") == "report_attachment"
            and candidate.get("sha256") == content_hash
            and candidate.get("mime_type") == normalized_mime
            and not candidate.get("report_id")
            and candidate_path.with_suffix(".bin").is_file()
        ):
            return _public_metadata(candidate)

    attachment_id = uuid4().hex
    data_path, metadata_path = _paths(email, attachment_id)
    metadata = {
        "id": attachment_id,
        "owner": owner,
        "name": name,
        "safe_filename": f"{attachment_id}.bin",
        "mime_type": normalized_mime,
        "size": len(raw),
        "sha256": content_hash,
        "status": "ACTIVE",
        "created_at": datetime.now().isoformat(),
        "persisted": False,
        "test_session_id": None,
        "generated": False,
        "derivative": False,
        "artifact_kind": "report_attachment",
        "report_id": None,
        "conversation_id": None,
        "conversation_ids": [],
        "retention_policy": "report_draft",
        "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
        "derived_artifacts": [],
    }
    data_path.write_bytes(raw)
    _write_metadata(metadata_path, metadata)
    return _public_metadata(metadata)


def attach_report_attachment(email: str, attachment_id: str, report_id: str) -> dict:
    """Bind one owned report attachment to its canonical report record."""

    metadata, _, metadata_path = _read_metadata(email, attachment_id)
    if metadata.get("artifact_kind") != "report_attachment":
        raise AttachmentStoreError("That file is not a report attachment.")
    existing_report = str(metadata.get("report_id") or "")
    if existing_report and existing_report != str(report_id):
        raise AttachmentStoreError("That report attachment is already in use.")
    metadata["report_id"] = str(report_id)[:64]
    metadata["persisted"] = True
    metadata["retention_policy"] = "forever"
    metadata["expires_at"] = None
    _write_metadata(metadata_path, metadata)
    return _public_metadata(metadata)


def delete_report_attachment(email: str, attachment_id: str) -> dict:
    """Delete only an unsubmitted owned report attachment."""

    metadata, _, _ = _read_metadata(email, attachment_id)
    if metadata.get("artifact_kind") != "report_attachment" or metadata.get("report_id"):
        raise AttachmentStoreError("That report attachment cannot be removed now.")
    return delete_owned_attachment(email, attachment_id)


def list_uploaded_files(email: str) -> dict:
    """Return user-owned file metadata and factual configured quota state."""

    items = []
    total_bytes = 0
    now = datetime.now()
    for metadata_path in _user_dir(email).glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                metadata.get("owner") != _user_key(email)
                or metadata.get("generated") is True
                or metadata.get("artifact_kind") == "report_attachment"
                or not metadata_path.with_suffix(".bin").is_file()
            ):
                continue
            expires_at = metadata.get("expires_at")
            if expires_at and datetime.fromisoformat(expires_at) <= now:
                continue
            total_bytes += int(metadata.get("size") or 0)
            items.append(_public_metadata(metadata))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    configured = os.getenv("JUPITER_USER_FILE_QUOTA_BYTES", "").strip()
    try:
        allowance = max(1, int(configured)) if configured else None
    except ValueError:
        allowance = None
    pressure = (total_bytes / allowance) if allowance else None
    return {
        "items": items,
        "count": len(items),
        "used_bytes": total_bytes,
        "allowance_bytes": allowance,
        "allowance_configured": allowance is not None,
        "warning": "critical" if pressure is not None and pressure >= 0.9 else "warning" if pressure is not None and pressure >= 0.75 else "none",
    }


def list_generated_files(email: str) -> dict:
    """List existing durable generated attachments, never temporary/private/test files.

    Reuse the authoritative attachment store; no duplicate copies or new retention rules.
    Do not return source text, prompts, provider internals or filesystem paths.
    """
    items = []
    now = datetime.now()
    for metadata_path in _user_dir(email).glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            attachment_id = str(metadata.get("id") or "")
            if (metadata.get("owner") != _user_key(email)
                    or metadata.get("generated") is not True
                    or metadata.get("persisted") is not True
                    or metadata.get("test_session_id")
                    or metadata.get("status") != "ACTIVE"
                    or metadata_path.stem != attachment_id
                    or not _ATTACHMENT_ID.fullmatch(attachment_id)
                    or not metadata_path.with_suffix(".bin").is_file()):
                continue
            expires = metadata.get("expires_at")
            if expires and datetime.fromisoformat(expires) <= now:
                continue
            public = _public_metadata(metadata)
            items.append({key: public[key] for key in (
                "id", "name", "mime_type", "size", "kind", "created_at", "duration_ms"
            ) if key in public})
        except (AttachmentStoreError, OSError, TypeError, ValueError, KeyError):
            continue
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {"items": items[:100], "count": len(items), "has_more": len(items) > 100}


def set_attachment_retention(email: str, attachment_id: str, policy: str) -> dict:
    policy = str(policy or "").strip().lower()
    if policy not in {"this_chat", "three_months", "forever"}:
        raise AttachmentStoreError("Invalid file-retention policy.")
    metadata, _, metadata_path = _read_metadata(email, attachment_id)
    metadata["retention_policy"] = policy
    metadata["expires_at"] = (
        _add_calendar_months(datetime.now(), 3).isoformat()
        if policy == "three_months" else None
    )
    _write_metadata(metadata_path, metadata)
    return _public_metadata(metadata)


def delete_owned_attachment(email: str, attachment_id: str) -> dict:
    """Delete one exact owned source and only its declared derivatives."""

    metadata, data_path, metadata_path = _read_metadata(email, attachment_id)
    removed = []
    user_directory = _user_dir(email).resolve()
    for raw_path in metadata.get("derived_artifacts") or []:
        try:
            candidate = Path(str(raw_path)).resolve()
            if candidate.is_file() and user_directory in candidate.parents:
                candidate.unlink(missing_ok=True)
                removed.append(candidate.name)
        except (OSError, ValueError):
            continue
    data_path.unlink(missing_ok=True)
    metadata_path.unlink(missing_ok=True)
    return {"id": attachment_id, "derived_removed": removed, "source_sha256": metadata.get("sha256")}


def delete_conversation_attachments(email: str, conversation_id: str) -> dict:
    """Delete only ``this_chat`` uploads after that chat is permanently purged."""

    normalized = str(conversation_id or "").strip()
    if not normalized:
        return {"deleted": 0, "attachment_ids": []}
    deleted: list[str] = []
    for metadata_path in _user_dir(email).glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            conversations = set(metadata.get("conversation_ids") or [])
            if metadata.get("conversation_id"):
                conversations.add(str(metadata["conversation_id"]))
            if (
                metadata.get("owner") != _user_key(email)
                or metadata.get("generated") is True
                or metadata.get("retention_policy") != "this_chat"
                or normalized not in conversations
            ):
                continue
            attachment_id = str(metadata.get("id") or "")
            if attachment_id:
                delete_owned_attachment(email, attachment_id)
                deleted.append(attachment_id)
        except (AttachmentStoreError, OSError, ValueError, json.JSONDecodeError):
            continue
    return {"deleted": len(deleted), "attachment_ids": deleted}


def save_generated_artifact(
    email: str,
    raw: bytes,
    mime_type: str,
    *,
    persist: bool,
    test_session_id: str | None = None,
    conversation_id: str | None = None,
    source_image_id: str | None = None,
    provider: str | None = None,
    provider_model: str | None = None,
    provider_interaction_id: str | None = None,
    synthid: bool | None = None,
    original_prompt: str | None = None,
    compiled_prompt: str | None = None,
    image_constraints: dict | None = None,
    image_quality_mode: str | None = None,
    image_validation: str | None = None,
    revision_mode: str | None = None,
    revision_number: int | None = None,
) -> dict:
    """Store one generated image in the same user-isolated artifact store as uploads."""

    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    raw, normalized_mime = _sanitized_generated_image(raw, normalized_mime)
    extension = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }.get(normalized_mime)
    if extension is None or not raw:
        raise AttachmentStoreError("The image provider returned an unsupported artifact.")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise AttachmentStoreError("The generated image is too large to store.")
    attachment_id = uuid4().hex
    data_path, metadata_path = _paths(email, attachment_id)
    metadata = {
        "id": attachment_id,
        "owner": _user_key(email),
        "name": f"Jupiter-created-{datetime.now().strftime('%Y%m%d-%H%M%S')}{extension}",
        "mime_type": normalized_mime,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "status": "ACTIVE",
        "created_at": datetime.now().isoformat(),
        "persisted": bool(persist),
        "test_session_id": None if persist else test_session_id,
        "generated": True,
        "derivative": bool(source_image_id),
        "conversation_id": conversation_id,
        "conversation_ids": [conversation_id] if conversation_id else [],
        "source_image_id": source_image_id,
        "provider": provider,
        "provider_model": provider_model,
        "provider_interaction_id": provider_interaction_id,
        "provider_interaction_created_at": datetime.now().isoformat() if provider_interaction_id else None,
        "synthid": synthid,
        # These source-intent fields remain private store metadata. They let
        # Edit Again regenerate a faithful revision when no true free editor
        # is eligible, without making the user repeat the original prompt.
        "original_prompt": str(original_prompt or "")[:1800] or None,
        "compiled_prompt": str(compiled_prompt or "")[:6000] or None,
        "image_constraints": dict(image_constraints or {}),
        "image_quality_mode": image_quality_mode,
        "image_validation": image_validation,
        "revision_mode": revision_mode,
        "revision_number": revision_number,
    }
    data_path.write_bytes(raw)
    _write_metadata(metadata_path, metadata)
    return _public_metadata(metadata)


def save_generated_audio_artifact(
    email: str,
    raw: bytes,
    filename: str,
    *,
    voice_name: str,
    source_artifact_id: str,
    persist: bool,
    test_session_id: str | None = None,
    conversation_id: str | None = None,
    duration_ms: int | None = None,
    source_text: str = "",
) -> dict:
    """Persist generated MP3 speech through Jupiter's normal attachment store."""

    if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
        raise AttachmentStoreError("The generated audio is empty or too large to store.")
    if not (raw.startswith(b"ID3") or (len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0)):
        raise AttachmentStoreError("The speech service did not return a valid MP3 artifact.")
    name = _display_name(filename)
    if Path(name).suffix.casefold() != ".mp3":
        name = f"{Path(name).stem or 'Jupiter_Voice'}.mp3"
    normalized_conversation = str(conversation_id or "").strip() or None
    owner = _user_key(email)

    # Reuse a previously attached copy only within the same conversation. The
    # synthesis cache can be shared for the user, while message artifacts stay
    # conversation-scoped and never cross account boundaries.
    for candidate_path in _user_dir(email).glob("*.json"):
        try:
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            candidate.get("owner") == owner
            and candidate.get("artifact_kind") == "custom_tts"
            and candidate.get("source_artifact_id") == source_artifact_id
            and candidate.get("conversation_id") == normalized_conversation
            and candidate_path.with_suffix(".bin").is_file()
        ):
            return _public_metadata(candidate)

    attachment_id = uuid4().hex
    data_path, metadata_path = _paths(email, attachment_id)
    metadata = {
        "id": attachment_id,
        "owner": owner,
        "name": name,
        "safe_filename": f"{attachment_id}.bin",
        "mime_type": "audio/mpeg",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "status": "ACTIVE",
        "created_at": datetime.now().isoformat(),
        "persisted": bool(persist),
        "test_session_id": None if persist else test_session_id,
        "generated": True,
        "derivative": False,
        "artifact_kind": "custom_tts",
        "source_artifact_id": str(source_artifact_id or "")[:128],
        "voice_name": str(voice_name or "")[:40],
        "duration_ms": max(0, int(duration_ms or 0)) or None,
        # The owned attachment keeps the exact source needed by the in-chat
        # Change Voice action. It is returned only through authenticated,
        # account-scoped attachment/conversation responses.
        "source_text": str(source_text or "")[:20_000],
        "conversation_id": normalized_conversation,
        "conversation_ids": [normalized_conversation] if normalized_conversation else [],
        "retention_policy": "this_chat",
        "expires_at": None,
        "derived_artifacts": [],
    }
    data_path.write_bytes(raw)
    _write_metadata(metadata_path, metadata)
    return _public_metadata(metadata)


def latest_generated_audio(email: str, conversation_id: str | None) -> dict | None:
    """Return the latest real custom-TTS attachment in one owned conversation."""

    normalized_conversation = str(conversation_id or "").strip() or None
    owner = _user_key(email)
    matches: list[dict] = []
    for metadata_path in _user_dir(email).glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            metadata.get("owner") == owner
            and metadata.get("artifact_kind") == "custom_tts"
            and metadata.get("conversation_id") == normalized_conversation
            and metadata_path.with_suffix(".bin").is_file()
        ):
            matches.append(metadata)
    if not matches:
        return None
    matches.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return _public_metadata(matches[0])


def get_public_metadata(email: str, attachment_id: str) -> dict:
    metadata, _, _ = _read_metadata(email, attachment_id)
    return _public_metadata(metadata)


def get_content(email: str, attachment_id: str) -> tuple[Path, dict]:
    metadata, data_path, _ = _read_metadata(email, attachment_id)
    return data_path, _public_metadata(metadata)


def _normalized_tts_download_speed(speed: object) -> float:
    try:
        value = round(float(speed), 2)
    except (TypeError, ValueError):
        raise AttachmentStoreError("Choose a supported download speed.", code="AUDIO_SPEED_INVALID") from None
    if value not in TTS_DOWNLOAD_SPEEDS:
        raise AttachmentStoreError("Choose a supported download speed.", code="AUDIO_SPEED_INVALID")
    return value


def _tts_download_filename(metadata: dict, speed: float) -> str:
    source_name = _display_name(metadata.get("name") or "Jupiter_Voice.mp3")
    stem = Path(source_name).stem or "Jupiter_Voice"
    if speed == 1.0:
        return f"{stem}.mp3"
    speed_label = f"{speed:g}x"
    return f"{stem}_{speed_label}.mp3"


def generated_audio_download(email: str, attachment_id: str, speed: object = 1.0) -> tuple[Path, str, bool]:
    """Return one owned custom-TTS MP3 or a cached local speed variant.

    The source attachment is always authenticated through ``_read_metadata``.
    Non-normal speeds are deterministic ffmpeg derivatives stored beside the
    source and recorded in its metadata so ordinary attachment cleanup removes
    them with the source. No provider/TTS request occurs here.
    """

    normalized_speed = _normalized_tts_download_speed(speed)
    metadata, source_path, metadata_path = _read_metadata(email, attachment_id)
    if (
        metadata.get("mime_type") != "audio/mpeg"
        or metadata.get("generated") is not True
        or metadata.get("artifact_kind") != "custom_tts"
    ):
        raise AttachmentStoreError("This attachment is not a Jupiter voice-over.", code="AUDIO_NOT_TTS")
    if normalized_speed == 1.0:
        return source_path, _tts_download_filename(metadata, normalized_speed), False

    speed_token = f"{int(round(normalized_speed * 100)):03d}"
    variant_path = source_path.with_name(f"{source_path.stem}.speed-{speed_token}.mp3")
    if variant_path.is_file() and variant_path.stat().st_size > 0:
        return variant_path, _tts_download_filename(metadata, normalized_speed), True

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AttachmentStoreError("Local MP3 speed conversion is unavailable.", code="AUDIO_SPEED_UNAVAILABLE")
    with _AUDIO_VARIANT_LOCK:
        if variant_path.is_file() and variant_path.stat().st_size > 0:
            return variant_path, _tts_download_filename(metadata, normalized_speed), True
        with tempfile.NamedTemporaryFile(
            prefix=f"{source_path.stem}.speed-{speed_token}-",
            suffix=".mp3",
            dir=source_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            completed = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source_path),
                    "-filter:a",
                    f"atempo={normalized_speed:.2f}",
                    "-b:a",
                    "128k",
                    str(temporary_path),
                ],
                capture_output=True,
                timeout=120,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0 or not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
                raise AttachmentStoreError("Local MP3 speed conversion failed.", code="AUDIO_SPEED_UNAVAILABLE")
            temporary_path.replace(variant_path)
        finally:
            temporary_path.unlink(missing_ok=True)

        derivatives = list(metadata.get("derived_artifacts") or [])
        variant_string = str(variant_path)
        if variant_string not in derivatives:
            derivatives.append(variant_string)
            metadata["derived_artifacts"] = derivatives[-24:]
            _write_metadata(metadata_path, metadata)
    return variant_path, _tts_download_filename(metadata, normalized_speed), False


def bind_attachment_to_conversation(email: str, attachment_id: str, conversation_id: str) -> dict:
    if not str(conversation_id or "").strip():
        raise AttachmentStoreError("A conversation is required.", code="IMAGE_REFERENCE_FORBIDDEN")
    metadata, _, metadata_path = _read_metadata(email, attachment_id)
    conversations = list(dict.fromkeys([
        *metadata.get("conversation_ids", []),
        *([metadata.get("conversation_id")] if metadata.get("conversation_id") else []),
        conversation_id,
    ]))
    existing_conversations = set(conversations) - {conversation_id}
    if metadata.get("persisted") and existing_conversations and conversation_id not in set(metadata.get("conversation_ids") or []):
        raise AttachmentStoreError(
            "That saved image belongs to a different conversation.",
            code="IMAGE_REFERENCE_FORBIDDEN",
        )
    metadata["conversation_id"] = conversation_id
    metadata["conversation_ids"] = conversations
    _write_metadata(metadata_path, metadata)
    return _public_metadata(metadata)


def image_reference_for(email: str, attachment_id: str, conversation_id: str) -> tuple[bytes, dict]:
    metadata, data_path, _ = _read_metadata(email, attachment_id)
    if metadata.get("mime_type") not in _EDITABLE_IMAGE_MIMES:
        raise AttachmentStoreError(
            "Choose a PNG, JPEG, or WebP image to edit.", code="IMAGE_FORMAT_UNSUPPORTED"
        )
    allowed_conversations = set(metadata.get("conversation_ids") or [])
    if metadata.get("conversation_id"):
        allowed_conversations.add(metadata["conversation_id"])
    if not conversation_id or conversation_id not in allowed_conversations:
        raise AttachmentStoreError(
            "That image is not available in this conversation.", code="IMAGE_REFERENCE_FORBIDDEN"
        )
    internal = dict(metadata)
    interaction_created = internal.get("provider_interaction_created_at")
    try:
        if not interaction_created or datetime.now() - datetime.fromisoformat(interaction_created) > timedelta(hours=23):
            internal.pop("provider_interaction_id", None)
    except (TypeError, ValueError):
        internal.pop("provider_interaction_id", None)
    internal["data"] = data_path.read_bytes()
    return internal.pop("data"), internal


def conversation_images(email: str, conversation_id: str) -> list[dict]:
    if not conversation_id:
        return []
    images: list[dict] = []
    for metadata_path in _user_dir(email).glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        conversations = set(metadata.get("conversation_ids") or [])
        if metadata.get("conversation_id"):
            conversations.add(metadata["conversation_id"])
        if (
            metadata.get("owner") == _user_key(email)
            and metadata.get("mime_type") in _EDITABLE_IMAGE_MIMES
            and conversation_id in conversations
            and metadata_path.with_suffix(".bin").is_file()
        ):
            images.append(metadata)
    images.sort(key=lambda item: item.get("last_used_at") or item.get("created_at") or "", reverse=True)
    return [_public_metadata(item) for item in images]


def remove_staged(email: str, attachment_id: str) -> bool:
    metadata, data_path, metadata_path = _read_metadata(email, attachment_id)
    if metadata.get("persisted"):
        raise AttachmentStoreError("A sent attachment cannot be removed from storage here.")
    data_path.unlink(missing_ok=True)
    metadata_path.unlink(missing_ok=True)
    return True


def model_parts_for(
    email: str,
    attachment_ids: list[str],
    *,
    persist: bool,
    test_session_id: str | None = None,
    conversation_id: str | None = None,
) -> tuple[list[dict], list[dict]]:
    unique_ids = list(dict.fromkeys(str(item) for item in attachment_ids if item))
    if len(unique_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise AttachmentStoreError(f"Attach at most {MAX_ATTACHMENTS_PER_MESSAGE} files per message.")
    model_parts: list[dict] = []
    public: list[dict] = []
    for attachment_id in unique_ids:
        metadata, data_path, metadata_path = _read_metadata(email, attachment_id)
        raw = data_path.read_bytes()
        mime_type = metadata["mime_type"]
        if mime_type in _TEXT_MIMES:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise AttachmentStoreError(f'"{metadata["name"]}" is not valid UTF-8 text.') from error
            model_parts.append({
                "type": "text",
                "text": untrusted_data_block(
                    f'attached file "{metadata["name"]}"', text
                ),
            })
        elif mime_type in _MODEL_BINARY_MIMES:
            model_parts.append({
                "type": "binary",
                "name": metadata["name"],
                "mime_type": mime_type,
                "data": raw,
            })
        elif mime_type in _VIDEO_MIMES:
            # Raw video is deliberately handled by VideoUnderstandingPipeline
            # in the request layer. It must never become an arbitrary generic
            # image/document binary part for a model adapter.
            pass
        else:
            raise AttachmentStoreError(f'Jupiter cannot read "{metadata["name"]}" yet.')
        # A private re-use of an existing saved artifact must never downgrade
        # the original file to staged/ephemeral state.
        metadata["persisted"] = bool(metadata.get("persisted") or persist)
        metadata["test_session_id"] = None if persist else test_session_id
        metadata["last_used_at"] = datetime.now().isoformat()
        if conversation_id:
            allowed_conversations = set(metadata.get("conversation_ids") or [])
            if metadata.get("conversation_id"):
                allowed_conversations.add(metadata["conversation_id"])
            if metadata.get("persisted") and allowed_conversations and conversation_id not in allowed_conversations:
                raise AttachmentStoreError(
                    "That saved attachment belongs to a different conversation.",
                    code="ATTACHMENT_REFERENCE_FORBIDDEN",
                )
            conversations = list(dict.fromkeys([
                *metadata.get("conversation_ids", []),
                *([metadata.get("conversation_id")] if metadata.get("conversation_id") else []),
                conversation_id,
            ]))
            metadata["conversation_id"] = conversation_id
            metadata["conversation_ids"] = conversations
        _write_metadata(metadata_path, metadata)
        public.append(_public_metadata(metadata))
    return model_parts, public


def cleanup_ephemeral_attachments(email: str, attachment_ids: list[str]) -> int:
    """Remove only unsaved artifacts used by a no-persist request."""

    removed = 0
    for attachment_id in dict.fromkeys(str(item) for item in attachment_ids if item):
        try:
            metadata, data_path, metadata_path = _read_metadata(email, attachment_id)
            if metadata.get("persisted"):
                continue
            data_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            removed += 1
        except AttachmentStoreError:
            continue
    return removed


def cleanup_testing_session(email: str, test_session_id: str) -> int:
    removed = 0
    for metadata_path in _user_dir(email).glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("persisted") or metadata.get("test_session_id") != test_session_id:
                continue
            metadata_path.with_suffix(".bin").unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            removed += 1
        except (OSError, json.JSONDecodeError):
            continue
    return removed
