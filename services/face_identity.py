"""Consent-gated, per-user Face++ enrollment mappings."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from services.specialists import FacePlusPlusSpecialist, SpecialistUnavailable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserFaceIdentityStore:
    def __init__(self, data_dir: str | Path):
        root = Path(data_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "face_identity.sqlite3"
        self.key_path = root / ".biometric_secret_key"
        self.cipher = Fernet(self._key())
        self._initialize()

    def _key(self) -> bytes:
        configured = os.getenv("JUPITER_CREDENTIAL_ENCRYPTION_KEY", "").strip()
        if configured:
            return configured.encode("ascii")
        if self.key_path.is_file():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        temporary = self.key_path.with_suffix(".tmp")
        temporary.write_bytes(key)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.key_path)
        return key

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS face_references (
                    id TEXT PRIMARY KEY,
                    user_email TEXT NOT NULL COLLATE NOCASE,
                    person_name TEXT NOT NULL,
                    faceset_outer_id TEXT NOT NULL,
                    face_token_encrypted BLOB NOT NULL,
                    source_attachment_id TEXT,
                    consented_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_face_references_user
                    ON face_references(user_email, deleted_at, created_at DESC);
                """
            )
            # Additive migration: optional user-provided relationship label ("sister"), never inferred attributes.
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(face_references)")}
            if "relationship" not in columns:
                connection.execute("ALTER TABLE face_references ADD COLUMN relationship TEXT")

    @staticmethod
    def faceset_for_user(email: str) -> str:
        return "jupiter_" + hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:24]

    def list_references(self, email: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,person_name,relationship,source_attachment_id,consented_at,created_at FROM face_references WHERE user_email=? AND deleted_at IS NULL ORDER BY created_at DESC",
                (email.strip().lower(),),
            ).fetchall()
            return [dict(row) for row in rows]

    def enroll(
        self,
        *,
        email: str,
        person_names: list[str],
        image: bytes,
        source_attachment_id: str,
        explicit_consent: bool,
        relationship: str | None = None,
    ) -> list[dict]:
        FacePlusPlusSpecialist.require_enrollment_consent(explicit_consent, True)
        relation = " ".join(str(relationship or "").split())[:40].casefold() or None
        specialist = FacePlusPlusSpecialist()
        faces = specialist.detect(image)
        tokens = [str(face.get("face_token") or "") for face in faces if face.get("face_token")]
        names = [" ".join(str(name or "").split())[:120] for name in person_names]
        if not tokens:
            raise SpecialistUnavailable("No face was detected for enrollment.")
        if len(tokens) != len(names) or any(not name for name in names):
            raise ValueError("Provide one name for each detected face.")
        outer_id = self.faceset_for_user(email)
        specialist.ensure_faceset(outer_id)
        specialist.add_faces(outer_id, tokens)
        created = []
        with self._connect() as connection:
            for token, name in zip(tokens, names):
                row = {
                    "id": uuid.uuid4().hex,
                    "user_email": email.strip().lower(),
                    "person_name": name,
                    "faceset_outer_id": outer_id,
                    "face_token_encrypted": self.cipher.encrypt(token.encode("utf-8")),
                    "source_attachment_id": source_attachment_id,
                    "consented_at": _now(),
                    "created_at": _now(),
                    "deleted_at": None,
                    "relationship": relation,
                }
                connection.execute(
                    "INSERT INTO face_references (id,user_email,person_name,faceset_outer_id,face_token_encrypted,source_attachment_id,consented_at,created_at,deleted_at,relationship) "
                    "VALUES (:id,:user_email,:person_name,:faceset_outer_id,:face_token_encrypted,:source_attachment_id,:consented_at,:created_at,:deleted_at,:relationship)",
                    row,
                )
                created.append({key: row[key] for key in ("id", "person_name", "relationship", "source_attachment_id", "consented_at", "created_at")})
        return created

    def resolve_token(self, email: str, face_token: str) -> str | None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT person_name,face_token_encrypted FROM face_references WHERE user_email=? AND deleted_at IS NULL",
                (email.strip().lower(),),
            ).fetchall()
            for row in rows:
                try:
                    if self.cipher.decrypt(bytes(row["face_token_encrypted"])).decode("utf-8") == face_token:
                        return str(row["person_name"])
                except (InvalidToken, UnicodeDecodeError):
                    continue
        return None

    def has_known_people(self, email: str) -> bool:
        return bool(self.list_references(email))

    def identify(self, *, email: str, image: bytes) -> list[dict]:
        # Recognition only makes sense against THIS user's explicitly enrolled people: with none, Face++ is
        # never called at all (no detection, no search).
        if not self.list_references(email):
            return []
        specialist = FacePlusPlusSpecialist()
        faces = specialist.detect(image)
        outer_id = self.faceset_for_user(email)
        results = []
        for position, face in enumerate(faces, start=1):
            probe = str(face.get("face_token") or "")
            if not probe:
                continue
            match = specialist.search(probe, outer_id)
            name = self.resolve_token(email, match.provider_face_token or "") if match.provider_face_token else None
            known = match.state == "KNOWN" and bool(name)
            results.append({
                "position": position,
                "state": "KNOWN" if known else ("PROBABLE" if match.state in {"KNOWN", "PROBABLE"} else "UNKNOWN"),
                "confidence": match.confidence,
                # A weak/uncertain match never carries a name: Jupiter must not claim or hint an identity.
                "person_name": name if known else None,
            })
        return results

    def identify_with_boxes(self, *, email: str, image: bytes) -> list[dict]:
        """identify() plus each detected face's pixel rectangle (on-demand "mark <person>"). Same consent/known-only rules."""
        if not self.list_references(email):
            return []
        specialist = FacePlusPlusSpecialist()
        faces = specialist.detect(image)
        outer_id = self.faceset_for_user(email)
        results = []
        for face in faces:
            probe = str(face.get("face_token") or "")
            rect = face.get("face_rectangle") or {}
            if not probe or not rect:
                continue
            match = specialist.search(probe, outer_id)
            name = self.resolve_token(email, match.provider_face_token or "") if match.provider_face_token else None
            known = match.state == "KNOWN" and bool(name)
            results.append({
                "state": "KNOWN" if known else "UNKNOWN",
                "person_name": name if known else None,           # uncertain matches never carry a name
                "rect": {key: float(rect.get(key) or 0) for key in ("left", "top", "width", "height")},
            })
        return results

    def rename(self, *, email: str, reference_id: str, person_name: str, relationship: str | None = None) -> bool:
        name = " ".join(str(person_name or "").split())[:120]
        if not name:
            raise ValueError("A name is required.")
        relation = " ".join(str(relationship or "").split())[:40].casefold() or None
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE face_references SET person_name=?, relationship=COALESCE(?, relationship) "
                "WHERE id=? AND user_email=? AND deleted_at IS NULL",
                (name, relation, reference_id, email.strip().lower()),
            ).rowcount
        return bool(updated)

    def delete(self, *, email: str, reference_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM face_references WHERE id=? AND user_email=? AND deleted_at IS NULL",
                (reference_id, email.strip().lower()),
            ).fetchone()
            if not row:
                return False
            try:
                token = self.cipher.decrypt(bytes(row["face_token_encrypted"])).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError):
                token = ""
            if token:
                try:
                    FacePlusPlusSpecialist().remove_faces(str(row["faceset_outer_id"]), [token])
                except SpecialistUnavailable:
                    # User-directed deletion is authoritative locally. A
                    # provider outage must not leave the per-user identity
                    # mapping active inside Jupiter.
                    pass
            connection.execute("UPDATE face_references SET deleted_at=? WHERE id=?", (_now(), reference_id))
            return True

    def delete_by_source(self, *, email: str, attachment_id: str) -> list[str]:
        with self._connect() as connection:
            ids = [str(row["id"]) for row in connection.execute(
                "SELECT id FROM face_references WHERE user_email=? AND source_attachment_id=? AND deleted_at IS NULL",
                (email.strip().lower(), attachment_id),
            ).fetchall()]
        return [reference_id for reference_id in ids if self.delete(email=email, reference_id=reference_id)]
