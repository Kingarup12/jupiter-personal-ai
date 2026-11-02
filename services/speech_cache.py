"""Persistent, user-isolated Gemini speech cache for Read and Live Voice."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4


MAX_USER_CACHE_BYTES = 48 * 1024 * 1024
MAX_USER_MANIFESTS = 500
ORPHAN_RETENTION_SECONDS = 7 * 24 * 60 * 60
MIN_PLAYBACK_PROTECTION_SECONDS = 120
_CACHE_KEY = re.compile(r"^[a-f0-9]{64}$")


def canonical_speech_text(text: str) -> str:
    return " ".join(str(text or "").split())


def speech_text_hash(text: str) -> str:
    return hashlib.sha256(canonical_speech_text(text).encode("utf-8")).hexdigest()


def _data_root() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    configured = os.getenv("JUPITER_DATA_DIR", "").strip()
    return Path(configured) if configured else project_root / "data"


def _user_ref(email: str) -> str:
    normalized = str(email or "").strip().casefold()
    if not normalized:
        raise ValueError("An authenticated user is required for speech caching.")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _fingerprint(parts: dict[str, Any]) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_metadata(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    chunks = payload.get("chunks")
    if chunks is not None and (not isinstance(chunks, list) or not all(isinstance(item, dict) for item in chunks)):
        return {}
    return payload


class SpeechCache:
    """Stores WAV chunks and completed response manifests under a user hash."""

    _lock = threading.RLock()
    _protected_until: dict[str, float] = {}
    _writing_keys: set[str] = set()
    _generation_locks: dict[str, list] = {}

    @classmethod
    @contextmanager
    def generation_lock(cls, identity: dict):
        """Coalesce identical in-process synthesis; remove locks after all waiters.

        The caller repeats disk lookup under this lock. Unrelated accounts and
        messages do not block one another or share results.
        """
        key = _fingerprint(identity)
        with cls._lock:
            entry = cls._generation_locks.setdefault(key, [threading.RLock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with cls._lock:
                entry[1] -= 1
                if not entry[1]:
                    cls._generation_locks.pop(key, None)

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else _data_root() / "speech_cache"

    @classmethod
    def _protect(cls, cache_key: str, duration_ms: int = 0) -> None:
        lease_seconds = max(
            MIN_PLAYBACK_PROTECTION_SECONDS,
            int(duration_ms or 0) / 1000 + 30,
        )
        cls._protected_until[cache_key] = max(
            cls._protected_until.get(cache_key, 0), time.time() + lease_seconds
        )

    @classmethod
    def _is_protected(cls, cache_key: str) -> bool:
        if cache_key in cls._writing_keys:
            return True
        expires = cls._protected_until.get(cache_key, 0)
        if expires > time.time():
            return True
        cls._protected_until.pop(cache_key, None)
        return False

    @staticmethod
    def chunk_identity(
        *,
        email: str,
        conversation_id: str,
        message_id: str,
        text: str,
        provider: str,
        model: str,
        voice: str,
        language: str,
        config_version: str,
        voice_mode: str,
        chunk_index: int,
        speech_plan_fingerprint: str = "",
        speech_planner_version: str = "",
        speech_mode: str = "",
    ) -> tuple[str, dict]:
        identity = {
            "user": _user_ref(email),
            "conversation_id": str(conversation_id or "")[:180],
            "message_id": str(message_id or "")[:180],
            "text_hash": speech_text_hash(text),
            "provider": str(provider or "").casefold(),
            "model": str(model or ""),
            "voice": str(voice or ""),
            "language": str(language or "other").casefold(),
            "config_version": str(config_version or ""),
            "voice_mode": str(voice_mode or "").casefold(),
            "speech_plan_fingerprint": str(speech_plan_fingerprint or "")[:128],
            "speech_planner_version": str(speech_planner_version or "")[:64],
            "speech_mode": str(speech_mode or "normal").casefold()[:40],
            "chunk_index": max(0, int(chunk_index or 0)),
        }
        return _fingerprint(identity), identity

    @staticmethod
    def manifest_identity(
        *,
        email: str,
        conversation_id: str,
        message_id: str,
        text: str,
        provider: str,
        model: str,
        voice: str,
        language: str,
        config_version: str,
        voice_mode: str,
        speech_plan_fingerprint: str = "",
        speech_planner_version: str = "",
        speech_mode: str = "",
    ) -> tuple[str, dict]:
        identity = {
            "user": _user_ref(email),
            "conversation_id": str(conversation_id or "")[:180],
            "message_id": str(message_id or "")[:180],
            "text_hash": speech_text_hash(text),
            "provider": str(provider or "").casefold(),
            "model": str(model or ""),
            "voice": str(voice or ""),
            "language": str(language or "other").casefold(),
            "config_version": str(config_version or ""),
            "voice_mode": str(voice_mode or "").casefold(),
            "speech_plan_fingerprint": str(speech_plan_fingerprint or "")[:128],
            "speech_planner_version": str(speech_planner_version or "")[:64],
            "speech_mode": str(speech_mode or "normal").casefold()[:40],
        }
        return _fingerprint(identity), identity

    def _directories(self, email: str) -> tuple[Path, Path, Path]:
        user_directory = self.root / _user_ref(email)
        chunks = user_directory / "chunks"
        manifests = user_directory / "manifests"
        chunks.mkdir(parents=True, exist_ok=True)
        manifests.mkdir(parents=True, exist_ok=True)
        return user_directory, chunks, manifests

    def _deleted_conversations_path(self, email: str) -> Path:
        user_directory, _, _ = self._directories(email)
        return user_directory / ".deleted_conversations.json"

    def _get_deleted_conversations(self, email: str) -> dict[str, float]:
        path = self._deleted_conversations_path(email)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def is_conversation_deleted(self, email: str, conversation_id: str) -> bool:
        normalized = str(conversation_id or "")[:180]
        if not normalized:
            return False
        with self._lock:
            records = self._get_deleted_conversations(email)
            return normalized in records

    def undelete_conversation(self, email: str, conversation_id: str) -> None:
        normalized = str(conversation_id or "")[:180]
        if not normalized:
            return
        with self._lock:
            records = self._get_deleted_conversations(email)
            if normalized in records:
                records.pop(normalized, None)
                self._atomic_json(self._deleted_conversations_path(email), records)

    @staticmethod
    def _atomic_json(path: Path, payload: dict) -> None:
        temporary = path.with_name(f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_bytes(path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(payload)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _valid_wav(audio: bytes, metadata: dict) -> bool:
        return bool(
            len(audio) >= 44
            and audio.startswith(b"RIFF")
            and b"WAVE" in audio[:16]
            and isinstance(metadata.get("duration_ms"), (int, float))
            and metadata["duration_ms"] > 0
        )

    def get_chunk(self, email: str, cache_key: str) -> tuple[bytes, dict] | None:
        if not _CACHE_KEY.fullmatch(str(cache_key or "")):
            return None
        _, chunks, _ = self._directories(email)
        audio_path = chunks / f"{cache_key}.wav"
        metadata_path = chunks / f"{cache_key}.json"
        with self._lock:
            try:
                metadata = _read_metadata(metadata_path)
                audio = audio_path.read_bytes()
            except (OSError, json.JSONDecodeError):
                return None
            if not isinstance(metadata, dict):
                return None
            if (
                metadata.get("cache_key") != cache_key
                or metadata.get("user") != _user_ref(email)
                or not self._valid_wav(audio, metadata)
            ):
                audio_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                return None
            metadata["last_accessed_at"] = time.time()
            self._atomic_json(metadata_path, metadata)
            self._protect(cache_key, int(metadata.get("duration_ms") or 0))
            return audio, metadata

    def put_chunk(
        self,
        *,
        email: str,
        conversation_id: str,
        message_id: str,
        text: str,
        provider: str,
        model: str,
        voice: str,
        language: str,
        config_version: str,
        voice_mode: str,
        chunk_index: int,
        chunk_start: int,
        chunk_end: int,
        audio: bytes,
        mime_type: str,
        duration_ms: int,
        silence: dict,
        speech_plan_fingerprint: str = "",
        speech_planner_version: str = "",
        speech_mode: str = "",
    ) -> tuple[str, dict]:
        cache_key, identity = self.chunk_identity(
            email=email,
            conversation_id=conversation_id,
            message_id=message_id,
            text=text,
            provider=provider,
            model=model,
            voice=voice,
            language=language,
            config_version=config_version,
            voice_mode=voice_mode,
            chunk_index=chunk_index,
            speech_plan_fingerprint=speech_plan_fingerprint,
            speech_planner_version=speech_planner_version,
            speech_mode=speech_mode,
        )
        now = time.time()
        metadata = {
            "cache_key": cache_key,
            **identity,
            "chunk_start": max(0, int(chunk_start or 0)),
            "chunk_end": max(0, int(chunk_end or 0)),
            "text_length": len(canonical_speech_text(text)),
            "mime_type": str(mime_type or "audio/wav"),
            "duration_ms": int(duration_ms or 0),
            "silence": dict(silence or {}),
            "created_at": now,
            "last_accessed_at": now,
            "complete": True,
        }
        if not self._valid_wav(audio, metadata):
            raise ValueError("Speech cache accepts only validated non-empty WAV audio.")
        _, chunks, _ = self._directories(email)
        with self._lock:
            if self.is_conversation_deleted(email, conversation_id):
                return cache_key, metadata
            self._writing_keys.add(cache_key)
            try:
                self._atomic_bytes(chunks / f"{cache_key}.wav", audio)
                self._atomic_json(chunks / f"{cache_key}.json", metadata)
            finally:
                self._writing_keys.discard(cache_key)
            self._protect(cache_key, int(duration_ms or 0))
            self._prune(email)
        return cache_key, metadata

    def lookup_manifest(
        self,
        *,
        email: str,
        conversation_id: str,
        message_id: str,
        text: str,
        provider: str,
        model: str,
        voice: str,
        language: str,
        config_version: str,
        voice_mode: str,
        speech_plan_fingerprint: str = "",
        speech_planner_version: str = "",
        speech_mode: str = "",
    ) -> dict | None:
        if self.is_conversation_deleted(email, conversation_id):
            return None
        manifest_key, identity = self.manifest_identity(
            email=email,
            conversation_id=conversation_id,
            message_id=message_id,
            text=text,
            provider=provider,
            model=model,
            voice=voice,
            language=language,
            config_version=config_version,
            voice_mode=voice_mode,
            speech_plan_fingerprint=speech_plan_fingerprint,
            speech_planner_version=speech_planner_version,
            speech_mode=speech_mode,
        )
        _, _, manifests = self._directories(email)
        path = manifests / f"{manifest_key}.json"
        with self._lock:
            try:
                manifest = _read_metadata(path)
            except (OSError, json.JSONDecodeError):
                return None
            if not isinstance(manifest, dict):
                return None
            if (
                manifest.get("manifest_key") != manifest_key
                or any(manifest.get(key) != value for key, value in identity.items())
                or manifest.get("user") != _user_ref(email)
                or manifest.get("complete") is not True
                or not isinstance(manifest.get("chunks"), list) or not manifest["chunks"]
            ):
                return None
            for index, chunk in enumerate(manifest["chunks"]):
                cached = self.get_chunk(email, str(chunk.get("cache_key") or "")) if isinstance(chunk, dict) else None
                if (not cached or chunk.get("index") != index or cached[1].get("chunk_index") != index
                    or cached[1].get("text_hash") != chunk.get("text_hash")
                    or any(cached[1].get(key) != value for key, value in identity.items() if key != "text_hash")):
                    self._remove_variants(email, manifest_key)
                    path.unlink(missing_ok=True)
                    return None
            manifest["last_accessed_at"] = time.time()
            self._atomic_json(path, manifest)
            return manifest

    def finalize_manifest(
        self,
        *,
        email: str,
        conversation_id: str,
        message_id: str,
        text: str,
        provider: str,
        model: str,
        voice: str,
        language: str,
        config_version: str,
        voice_mode: str,
        chunks: list[dict],
        speech_plan_fingerprint: str = "",
        speech_planner_version: str = "",
        speech_mode: str = "",
        source_text: str = "",
    ) -> dict:
        if self.is_conversation_deleted(email, conversation_id):
            raise ValueError("Speech manifest cannot be created for a deleted conversation.")
        canonical_full = canonical_speech_text(text)
        if not canonical_full or not chunks:
            raise ValueError("A complete speech manifest requires text and audio chunks.")
        ordered = sorted(chunks, key=lambda item: int(item.get("index") or 0))
        if [int(item.get("index") or 0) for item in ordered] != list(range(len(ordered))):
            raise ValueError("Speech chunks are not contiguous.")

        verified: list[dict] = []
        spoken_parts: list[str] = []
        for item in ordered:
            cache_key = str(item.get("cache_key") or "")
            chunk_text = canonical_speech_text(item.get("text") or "")
            cached = self.get_chunk(email, cache_key)
            if not cached or not chunk_text:
                raise ValueError("A speech chunk is unavailable or invalid.")
            _, metadata = cached
            if (
                metadata.get("conversation_id") != str(conversation_id or "")[:180]
                or metadata.get("message_id") != str(message_id or "")[:180]
                or metadata.get("text_hash") != speech_text_hash(chunk_text)
                or metadata.get("provider") != str(provider or "").casefold()
                or metadata.get("model") != str(model or "")
                or metadata.get("voice") != str(voice or "")
                or metadata.get("language") != str(language or "other").casefold()
                or metadata.get("chunk_index") != int(item.get("index") or 0)
                or metadata.get("config_version") != str(config_version or "")
                or metadata.get("voice_mode") != str(voice_mode or "").casefold()
                or metadata.get("speech_plan_fingerprint") != str(speech_plan_fingerprint or "")[:128]
                or metadata.get("speech_planner_version") != str(speech_planner_version or "")[:64]
                or metadata.get("speech_mode") != str(speech_mode or "normal").casefold()[:40]
            ):
                raise ValueError("Speech chunk metadata does not match the response.")
            spoken_parts.append(chunk_text)
            verified.append({
                "index": int(item.get("index") or 0),
                "cache_key": cache_key,
                "text_hash": metadata["text_hash"],
                "text_length": metadata["text_length"],
                "chunk_start": int(metadata.get("chunk_start") or 0),
                "chunk_end": int(metadata.get("chunk_end") or 0),
                "mime_type": metadata["mime_type"],
                "duration_ms": int(metadata["duration_ms"]),
                "silence": metadata.get("silence") or {},
            })
        if canonical_speech_text(" ".join(spoken_parts)) != canonical_full:
            raise ValueError("Speech chunks do not cover the finalized response exactly.")

        manifest_key, identity = self.manifest_identity(
            email=email,
            conversation_id=conversation_id,
            message_id=message_id,
            text=canonical_full,
            provider=provider,
            model=model,
            voice=voice,
            language=language,
            config_version=config_version,
            voice_mode=voice_mode,
            speech_plan_fingerprint=speech_plan_fingerprint,
            speech_planner_version=speech_planner_version,
            speech_mode=speech_mode,
        )
        now = time.time()
        manifest = {
            "manifest_key": manifest_key,
            "source_text_hash": speech_text_hash(source_text) if source_text else "",
            **identity,
            "created_at": now,
            "last_accessed_at": now,
            "chunk_count": len(verified),
            "duration_ms": sum(item["duration_ms"] for item in verified),
            "complete": True,
            "chunks": verified,
        }
        _, _, manifests = self._directories(email)
        with self._lock:
            self._atomic_json(manifests / f"{manifest_key}.json", manifest)
            self._remove_obsolete_message_manifests(
                email, conversation_id, message_id, keep=manifest_key
            )
            self._prune(email)
        return manifest

    def conversation_candidates(self, email: str, conversation_id: str, messages: list[dict]) -> list[dict]:
        """Only complete Read manifests whose source message is still unchanged."""
        message_hashes = {str(m.get("message_id") or m.get("id")): speech_text_hash(m.get("text", ""))
                          for m in messages if m.get("sender") in {"ai", "assistant"}}
        _, _, manifests = self._directories(email)
        candidates = []
        with self._lock:
            for path in manifests.glob("*.json"):
                try:
                    item = _read_metadata(path)
                except (OSError, ValueError):
                    continue
                if not isinstance(item, dict):
                    continue
                if (item.get("user") == _user_ref(email) and item.get("conversation_id") == conversation_id
                    and item.get("complete") is True and item.get("source_text_hash")
                    and item.get("source_text_hash") == message_hashes.get(str(item.get("message_id")))
                    and isinstance(item.get("chunks"), list) and item["chunks"]
                    and all(isinstance(chunk, dict) and self.get_chunk(email, str(chunk.get("cache_key", ""))) for chunk in item["chunks"])):
                    # Reuse the same compatibility/identity gate as normal
                    # lookup, rather than accepting arbitrary chunk references.
                    identity_keys = ("user", "conversation_id", "message_id", "text_hash", "provider", "model", "voice", "language", "config_version", "voice_mode", "speech_plan_fingerprint", "speech_planner_version", "speech_mode")
                    if (item.get("manifest_key") == path.stem
                        and _fingerprint({key: item.get(key) for key in identity_keys}) == path.stem
                        and all(chunk.get("index") == index and (cached := self.get_chunk(email, chunk["cache_key"]))
                            and cached[1].get("chunk_index") == index
                            and cached[1].get("text_hash") == chunk.get("text_hash")
                            and all(cached[1].get(key) == item.get(key) for key in identity_keys if key != "text_hash")
                            for index, chunk in enumerate(item["chunks"]))):
                        candidates.append(item)
        return candidates

    def _remove_variants(self, email: str, manifest_key: str) -> None:
        variants = self.root / _user_ref(email) / "variants"
        # Existing download variants have one key per supported speed; derive
        # exact names rather than clearing another chat's audio.
        for hundredths in range(50, 151):
            key = hashlib.sha256(f"{manifest_key}\0{hundredths / 100:.2f}".encode()).hexdigest()
            (variants / f"{key}.mp3").unlink(missing_ok=True)

    def put_variant(self, email: str, manifest_key: str, variant_key: str, audio: bytes) -> None:
        if not _CACHE_KEY.fullmatch(manifest_key) or not _CACHE_KEY.fullmatch(variant_key):
            raise ValueError("Invalid speech cache identity")
        with self._lock:
            root, _, manifests = self._directories(email)
            # A concurrent chat deletion wins over a slow conversion.
            if not (manifests / f"{manifest_key}.json").is_file():
                return
            variants = root / "variants"
            variants.mkdir(exist_ok=True)
            self._atomic_bytes(variants / f"{variant_key}.mp3", audio)
            self._prune(email)

    def _remove_obsolete_message_manifests(
        self, email: str, conversation_id: str, message_id: str, *, keep: str
    ) -> None:
        _, _, manifests = self._directories(email)
        for path in manifests.glob("*.json"):
            if path.stem == keep:
                continue
            try:
                manifest = _read_metadata(path)
            except (OSError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                continue
            if (
                manifest.get("conversation_id") == str(conversation_id or "")[:180]
                and manifest.get("message_id") == str(message_id or "")[:180]
            ):
                self._remove_variants(email, path.stem)
                path.unlink(missing_ok=True)
        self._remove_unreferenced_chunks(email, older_than=0)

    def delete_conversation(self, email: str, conversation_id: str) -> dict:
        normalized = str(conversation_id or "")[:180]
        _, chunks, manifests = self._directories(email)
        removed_manifests = 0
        removed_chunks = 0
        with self._lock:
            if normalized:
                records = self._get_deleted_conversations(email)
                records[normalized] = time.time()
                cutoff = time.time() - ORPHAN_RETENTION_SECONDS
                pruned = {k: v for k, v in records.items() if isinstance(v, (int, float)) and v >= cutoff}
                self._atomic_json(self._deleted_conversations_path(email), pruned)
            for path in manifests.glob("*.json"):
                try:
                    payload = _read_metadata(path)
                except (OSError, json.JSONDecodeError):
                    payload = {}
                if payload.get("conversation_id") == normalized:
                    self._remove_variants(email, path.stem)
                    path.unlink(missing_ok=True)
                    removed_manifests += 1
            for path in chunks.glob("*.json"):
                try:
                    payload = _read_metadata(path)
                except (OSError, json.JSONDecodeError):
                    payload = {}
                if payload.get("conversation_id") == normalized:
                    (chunks / f"{path.stem}.wav").unlink(missing_ok=True)
                    path.unlink(missing_ok=True)
                    removed_chunks += 1
        return {"speech_manifests_deleted": removed_manifests, "speech_chunks_deleted": removed_chunks}

    def _referenced_chunks(self, manifests: Path) -> set[str]:
        referenced: set[str] = set()
        for path in manifests.glob("*.json"):
            try:
                payload = _read_metadata(path)
                referenced.update(str(item.get("cache_key") or "") for item in payload.get("chunks") or [])
            except (OSError, json.JSONDecodeError):
                continue
        return referenced

    def _remove_unreferenced_chunks(self, email: str, *, older_than: float) -> None:
        _, chunks, manifests = self._directories(email)
        referenced = self._referenced_chunks(manifests)
        cutoff = time.time() - max(0, older_than)
        for metadata_path in chunks.glob("*.json"):
            if metadata_path.stem in referenced:
                continue
            if self._is_protected(metadata_path.stem):
                continue
            try:
                payload = _read_metadata(metadata_path)
                last_access = float(payload.get("last_accessed_at") or payload.get("created_at") or 0)
            except (OSError, ValueError, json.JSONDecodeError):
                last_access = 0
            if last_access > cutoff:
                continue
            (chunks / f"{metadata_path.stem}.wav").unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)

    def _prune(self, email: str) -> None:
        user_directory, chunks, manifests = self._directories(email)
        # Derived audio is disposable and bounded independently, including
        # legacy variants whose old manifest was removed before this repair.
        variants = sorted((user_directory / "variants").glob("*.mp3"), key=lambda path: path.stat().st_mtime)
        variant_bytes = sum(path.stat().st_size for path in variants)
        cutoff = time.time() - ORPHAN_RETENTION_SECONDS
        for path in variants:
            if path.stat().st_mtime < cutoff or variant_bytes > MAX_USER_CACHE_BYTES // 2:
                variant_bytes -= path.stat().st_size
                path.unlink(missing_ok=True)
        manifest_rows: list[tuple[float, Path, set[str]]] = []
        for path in manifests.glob("*.json"):
            try:
                payload = _read_metadata(path)
                cache_keys = {
                    str(item.get("cache_key") or "") for item in payload.get("chunks") or []
                }
                manifest_rows.append(
                    (float(payload.get("last_accessed_at") or 0), path, cache_keys)
                )
            except (OSError, ValueError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
        total_bytes = sum(path.stat().st_size for path in user_directory.rglob("*") if path.is_file())
        manifest_rows.sort(key=lambda item: item[0])
        while manifest_rows and (
            len(manifest_rows) > MAX_USER_MANIFESTS or total_bytes > MAX_USER_CACHE_BYTES
        ):
            removable_index = next(
                (
                    index for index, (_, _, cache_keys) in enumerate(manifest_rows)
                    if not any(self._is_protected(cache_key) for cache_key in cache_keys)
                ),
                None,
            )
            if removable_index is None:
                break
            _, oldest, _ = manifest_rows.pop(removable_index)
            self._remove_variants(email, oldest.stem)
            try:
                total_bytes -= oldest.stat().st_size
            except OSError:
                pass
            oldest.unlink(missing_ok=True)
            self._remove_unreferenced_chunks(email, older_than=0)
            total_bytes = sum(path.stat().st_size for path in user_directory.rglob("*") if path.is_file())
        self._remove_unreferenced_chunks(email, older_than=ORPHAN_RETENTION_SECONDS)
        total_bytes = sum(path.stat().st_size for path in user_directory.rglob("*") if path.is_file())
        if total_bytes <= MAX_USER_CACHE_BYTES:
            return
        referenced = self._referenced_chunks(manifests)
        orphan_rows: list[tuple[float, Path]] = []
        for metadata_path in chunks.glob("*.json"):
            if metadata_path.stem in referenced or self._is_protected(metadata_path.stem):
                continue
            try:
                payload = _read_metadata(metadata_path)
                last_access = float(
                    payload.get("last_accessed_at") or payload.get("created_at") or 0
                )
            except (OSError, ValueError, json.JSONDecodeError):
                last_access = 0
            orphan_rows.append((last_access, metadata_path))
        orphan_rows.sort(key=lambda item: item[0])
        for _, metadata_path in orphan_rows:
            if total_bytes <= MAX_USER_CACHE_BYTES:
                break
            (chunks / f"{metadata_path.stem}.wav").unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            total_bytes = sum(
                path.stat().st_size for path in user_directory.rglob("*") if path.is_file()
            )
