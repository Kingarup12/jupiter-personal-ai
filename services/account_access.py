"""Encrypted per-account Google API credentials and profile photos."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sqlite3
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image, UnidentifiedImageError

from core.provider_contracts import CredentialScope


MIGRATION_ID = "api_key_onboarding_sessions_v1"
MAX_PROFILE_PHOTO_BYTES = 5 * 1024 * 1024
GOOGLE_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountAccessStore:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "account_access.sqlite3"
        self.photo_dir = self.data_dir / "profile_photos"
        self.photo_dir.mkdir(exist_ok=True)
        self._fernet = Fernet(self._load_or_create_master_key())
        self._migrate()
        from services.optional_account_providers import migrate
        migrate(self)

    def _load_or_create_master_key(self) -> bytes:
        configured_value = (
            os.getenv("JUPITER_CREDENTIAL_ENCRYPTION_KEY", "").strip()
            or os.getenv("JUPITER_ACCOUNT_SECRET_KEY", "").strip()
        )
        configured = configured_value.encode("ascii")
        if configured:
            Fernet(configured)
            return configured
        key_path = self.data_dir / ".account_secret_key"
        if key_path.exists():
            return key_path.read_bytes().strip()
        key = Fernet.generate_key()
        temporary = key_path.with_suffix(".tmp")
        temporary.write_bytes(key)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, key_path)
        return key

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _migrate(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS account_access (
                    email TEXT PRIMARY KEY COLLATE NOCASE,
                    encrypted_google_key BLOB,
                    key_prefix TEXT,
                    key_suffix TEXT,
                    validation_status TEXT NOT NULL DEFAULT 'missing',
                    validated_at TEXT,
                    updated_at TEXT NOT NULL,
                    photo_filename TEXT,
                    photo_mime TEXT,
                    photo_updated_at TEXT
                )
                """
            )
            existing = {
                row[1] for row in connection.execute("PRAGMA table_info(account_access)")
            }
            additions = {
                "credential_id": "TEXT",
                "provider": "TEXT NOT NULL DEFAULT 'google'",
                "credential_scope": "TEXT NOT NULL DEFAULT 'USER_GOOGLE'",
                "encryption_version": "INTEGER NOT NULL DEFAULT 1",
                "credential_status": "TEXT NOT NULL DEFAULT 'UNVERIFIED'",
                "created_at": "TEXT",
                "last_verified_at": "TEXT",
                "failure_count": "INTEGER NOT NULL DEFAULT 0",
                "last_failure": "TEXT",
                "disabled_reason": "TEXT",
                "account_role": "TEXT",
            }
            for name, definition in additions.items():
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE account_access ADD COLUMN {name} {definition}"
                    )
            now = _utc_now()
            connection.execute(
                """
                UPDATE account_access
                SET credential_id=COALESCE(credential_id, lower(hex(randomblob(16)))),
                    created_at=COALESCE(created_at, updated_at, ?),
                    last_verified_at=COALESCE(last_verified_at, validated_at),
                    credential_status=CASE validation_status
                        WHEN 'connected' THEN 'ACTIVE'
                        WHEN 'invalid' THEN 'INVALID'
                        WHEN 'unverifiable' THEN 'UNVERIFIED'
                        ELSE credential_status
                    END
                """,
                (now,),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS gmail_oauth (
                    email TEXT PRIMARY KEY COLLATE NOCASE,
                    encrypted_token BLOB NOT NULL,
                    scopes_json TEXT NOT NULL,
                    connected_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def gmail_connection_metadata(self, email: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT scopes_json, connected_at, updated_at FROM gmail_oauth "
                "WHERE email=? COLLATE NOCASE",
                (email,),
            ).fetchone()
        if row is None:
            return {"connected": False, "scopes": [], "connected_at": None}
        try:
            scopes = json.loads(row["scopes_json"] or "[]")
        except (TypeError, ValueError):
            scopes = []
        return {
            "connected": True,
            "scopes": scopes if isinstance(scopes, list) else [],
            "connected_at": row["connected_at"],
            "updated_at": row["updated_at"],
        }

    def gmail_token(self, email: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT encrypted_token FROM gmail_oauth WHERE email=? COLLATE NOCASE",
                (email,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = self._fernet.decrypt(row["encrypted_token"]).decode("utf-8")
            token = json.loads(value)
        except (InvalidToken, TypeError, ValueError):
            return None
        return token if isinstance(token, dict) else None

    def store_gmail_token(self, email: str, token: dict, scopes: list[str] | tuple[str, ...]):
        now = _utc_now()
        encrypted = self._fernet.encrypt(
            json.dumps(token, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        scopes_json = json.dumps(sorted(set(scopes)), separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO gmail_oauth (
                    email, encrypted_token, scopes_json, connected_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    encrypted_token=excluded.encrypted_token,
                    scopes_json=excluded.scopes_json,
                    updated_at=excluded.updated_at
                """,
                (email, encrypted, scopes_json, now, now),
            )

    def remove_gmail_token(self, email: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM gmail_oauth WHERE email=? COLLATE NOCASE", (email,)
            )

    def credential_metadata(self, email: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT key_prefix, key_suffix, validation_status, validated_at, "
                "encrypted_google_key FROM account_access WHERE email=? COLLATE NOCASE",
                (email,),
            ).fetchone()
        if not row or not row["encrypted_google_key"]:
            return {"status": "missing", "masked_key": None, "validated_at": None}
        prefix = row["key_prefix"] or "Key"
        suffix = row["key_suffix"] or ""
        return {
            "status": row["validation_status"] or "missing",
            "masked_key": f"{prefix}{'•' * 12}{suffix}",
            "validated_at": row["validated_at"],
        }

    def stored_key(self, email: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT encrypted_google_key FROM account_access WHERE email=? COLLATE NOCASE",
                (email,),
            ).fetchone()
        if not row or not row["encrypted_google_key"]:
            return None
        try:
            return self._fernet.decrypt(row["encrypted_google_key"]).decode("utf-8")
        except InvalidToken:
            return None

    def credential_for_scope(self, email: str, scope: str) -> str | None:
        """Resolve a secret for one request without exposing it through an API."""

        if scope != CredentialScope.USER_GOOGLE.value:
            from services.optional_account_providers import resolve
            return resolve(self, email, scope)
        metadata = self.credential_record_metadata(email)
        # Older builds persisted a temporary, model-scoped quota result as a
        # credential-wide state. Keep that key eligible so the model-scoped
        # QuotaGovernor can admit it again after its cooldown expires. Genuine
        # authentication failures remain blocked.
        if metadata.get("status") not in {"ACTIVE", "UNVERIFIED", "QUOTA_EXHAUSTED"}:
            return None
        return self.stored_key(email)

    def optional_provider_metadata(self, email: str) -> list[dict]:
        from services.optional_account_providers import metadata
        return metadata(self, email)

    def save_optional_provider(self, email: str, provider_id: str, fields: dict) -> None:
        from services.optional_account_providers import save
        save(self, email, provider_id, fields)

    def remove_optional_provider(self, email: str, provider_id: str) -> None:
        from services.optional_account_providers import remove
        remove(self, email, provider_id)

    def record_optional_provider_status(self, email: str, scope: str, status: str) -> None:
        from services.optional_account_providers import record_status
        record_status(self, email, scope, status)

    def credential_record_metadata(self, email: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT credential_id, provider, credential_scope,
                       encryption_version, credential_status, created_at,
                       updated_at, last_verified_at, failure_count,
                       disabled_reason
                FROM account_access WHERE email=? COLLATE NOCASE
                """,
                (email,),
            ).fetchone()
        if row is None:
            return {
                "provider": "google",
                "credential_scope": CredentialScope.USER_GOOGLE.value,
                "status": "MISSING",
            }
        return {
            "credential_id": row["credential_id"],
            "provider": row["provider"],
            "credential_scope": row["credential_scope"],
            "encryption_version": row["encryption_version"],
            "status": row["credential_status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_verified_at": row["last_verified_at"],
            "failure_count": row["failure_count"],
            "disabled_reason": row["disabled_reason"],
        }

    def role_override(self, email: str) -> str | None:
        """Return the owner-managed role value without exposing credentials."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT account_role FROM account_access WHERE email=? COLLATE NOCASE",
                (email,),
            ).fetchone()
        role = str(row["account_role"] or "").strip().casefold() if row else ""
        return role if role in {"user", "admin"} else None

    def set_role_override(self, email: str, role: str) -> None:
        normalized_role = str(role or "").strip().casefold()
        if normalized_role not in {"user", "admin"}:
            raise ValueError("invalid_account_role")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO account_access (email, validation_status, updated_at, account_role)
                VALUES (?, 'missing', ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    account_role=excluded.account_role,
                    updated_at=excluded.updated_at
                """,
                (email, now, normalized_role),
            )

    def store_valid_key(self, email: str, api_key: str):
        now = _utc_now()
        encrypted = self._fernet.encrypt(api_key.encode("utf-8"))
        prefix = api_key[:4]
        suffix = api_key[-4:]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO account_access (
                    email, encrypted_google_key, key_prefix, key_suffix,
                    validation_status, validated_at, updated_at,
                    credential_id, provider, credential_scope,
                    encryption_version, credential_status, created_at,
                    last_verified_at, failure_count, last_failure, disabled_reason
                ) VALUES (?, ?, ?, ?, 'connected', ?, ?, lower(hex(randomblob(16))),
                          'google', 'USER_GOOGLE', 1, 'ACTIVE', ?, ?, 0, NULL, NULL)
                ON CONFLICT(email) DO UPDATE SET
                    encrypted_google_key=excluded.encrypted_google_key,
                    key_prefix=excluded.key_prefix,
                    key_suffix=excluded.key_suffix,
                    validation_status='connected',
                    validated_at=excluded.validated_at,
                    updated_at=excluded.updated_at,
                    provider='google',
                    credential_scope='USER_GOOGLE',
                    encryption_version=excluded.encryption_version,
                    credential_status='ACTIVE',
                    last_verified_at=excluded.last_verified_at,
                    failure_count=0,
                    last_failure=NULL,
                    disabled_reason=NULL
                """,
                (email, encrypted, prefix, suffix, now, now, now, now),
            )

    def update_validation_status(self, email: str, status: str):
        now = _utc_now()
        credential_status = {
            "connected": "ACTIVE",
            "invalid": "INVALID",
            "needs_reverify": "NEEDS_REVERIFY",
            "unverifiable": "UNVERIFIED",
            "disabled": "DISABLED",
            "expired": "EXPIRED",
            "quota_exhausted": "QUOTA_EXHAUSTED",
            "auth_error": "AUTH_ERROR",
        }.get(status, "UNVERIFIED")
        with self._connect() as connection:
            connection.execute(
                "UPDATE account_access SET validation_status=?, validated_at=?, updated_at=?, "
                "credential_status=?, last_verified_at=?, "
                "failure_count=CASE WHEN ?='ACTIVE' THEN 0 ELSE failure_count + 1 END "
                "WHERE email=? COLLATE NOCASE AND encrypted_google_key IS NOT NULL",
                (status, now, now, credential_status, now, credential_status, email),
            )

    def photo_metadata(self, email: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT photo_filename, photo_mime, photo_updated_at FROM account_access "
                "WHERE email=? COLLATE NOCASE",
                (email,),
            ).fetchone()
        return {
            "available": bool(row and row["photo_filename"]),
            "mime_type": row["photo_mime"] if row else None,
            "updated_at": row["photo_updated_at"] if row else None,
        }

    def save_profile_photo(self, email: str, encoded: str, claimed_mime: str) -> dict:
        email = email.strip().lower()
        if len(encoded) > ((MAX_PROFILE_PHOTO_BYTES + 2) // 3) * 4:
            raise ValueError("image_size_invalid")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as error:
            raise ValueError("invalid_image_data") from error
        if not raw or len(raw) > MAX_PROFILE_PHOTO_BYTES:
            raise ValueError("image_size_invalid")
        detected_mime, width, height = _inspect_image(raw)
        if detected_mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("image_type_invalid")
        if claimed_mime and claimed_mime.lower() != detected_mime:
            raise ValueError("image_type_mismatch")
        if width < 1 or height < 1 or width > 8000 or height > 8000:
            raise ValueError("image_dimensions_invalid")
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[detected_mime]
        # Keep the original, already-cropped bytes: validation must not introduce
        # a second lossy encode. A content revision also avoids stale replacements.
        revision = hashlib.sha256(raw).hexdigest()[:24]
        filename = hashlib.sha256(email.encode("utf-8")).hexdigest() + "-" + revision + extension
        destination = self.photo_dir / filename
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, destination)
        now = _utc_now()
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT photo_filename FROM account_access WHERE email=? COLLATE NOCASE", (email,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO account_access (email, validation_status, updated_at, photo_filename, photo_mime, photo_updated_at)
                VALUES (?, 'missing', ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET photo_filename=excluded.photo_filename,
                    photo_mime=excluded.photo_mime, photo_updated_at=excluded.photo_updated_at
                """,
                (email, now, filename, detected_mime, now),
            )
        if previous and previous["photo_filename"] and previous["photo_filename"] != filename:
            old_path = (self.photo_dir / previous["photo_filename"]).resolve()
            if old_path.parent == self.photo_dir.resolve() and old_path.is_file():
                old_path.unlink(missing_ok=True)
        return {"mime_type": detected_mime, "updated_at": now, "revision": revision,
                "width": width, "height": height}

    def photo_path(self, email: str) -> tuple[Path, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT photo_filename, photo_mime FROM account_access WHERE email=? COLLATE NOCASE",
                (email,),
            ).fetchone()
        if not row or not row["photo_filename"]:
            return None
        path = (self.photo_dir / row["photo_filename"]).resolve()
        if path.parent != self.photo_dir.resolve() or not path.is_file():
            return None
        return path, row["photo_mime"]


async def validate_google_api_key(api_key: str) -> str:
    """Return valid, invalid, or temporary without logging credential material."""
    candidate = (api_key or "").strip()
    if (not candidate or len(candidate) < 20 or len(candidate) > 200
            or any(character.isspace() for character in candidate)):
        return "invalid"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=6.0)) as client:
            response = await client.get(GOOGLE_MODELS_URL, headers={"x-goog-api-key": candidate})
    except (httpx.TimeoutException, httpx.NetworkError):
        return "temporary"
    if response.status_code == 200:
        return "valid"
    if response.status_code in {400, 401, 403}:
        return "invalid"
    return "temporary"


def invalidate_sessions_once(data_dir: str) -> bool:
    """Invalidate persisted auth sessions once for this additive migration."""
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker_path = root / "migration_state.json"
    state = {}
    if marker_path.exists():
        try:
            state = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    completed = set(state.get("completed", []))
    if MIGRATION_ID in completed:
        return False
    sessions_path = root / "sessions.json"
    temporary_sessions = root / "sessions.json.tmp"
    temporary_sessions.write_text("{}", encoding="utf-8")
    os.replace(temporary_sessions, sessions_path)
    completed.add(MIGRATION_ID)
    temporary_marker = root / "migration_state.json.tmp"
    temporary_marker.write_text(json.dumps({"completed": sorted(completed)}), encoding="utf-8")
    os.replace(temporary_marker, marker_path)
    return True


def _inspect_image(raw: bytes) -> tuple[str, int, int]:
    """Decode, not just sniff headers; never transform the canonical photo."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(image.format)
                if not mime:
                    raise ValueError("image_type_invalid")
                width, height = image.size
                if min(width, height) < 1 or max(width, height) > 8000:
                    raise ValueError("image_dimensions_invalid")
                image.verify()
            # verify() checks container integrity; load() also rejects truncated
            # pixel streams. Neither writes/re-encodes the supplied image.
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
            return mime, width, height
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("image_dimensions_invalid") from None
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise ValueError("invalid_image_data") from None
    except ValueError as error:
        if str(error) in {"image_type_invalid", "image_dimensions_invalid"}:
            raise
        raise ValueError("invalid_image_data") from None
