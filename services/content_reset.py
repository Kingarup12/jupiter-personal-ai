"""Chat/memory-only reset with a private, recoverable backup and protected-data verification.

The ONLY records this module removes (per selected account):
* chats, messages, deleted-chat records/tombstones  (chat history, including unfinished/partial responses)
* memories, facts, memory sources, review queue/feedback, internal notes, episodes, monthly summaries
* the account's conversation-memory vectors (Chroma records, deleted by id — never by deleting the database)
* derived-only companion_state keys and derived-only profile.json keys (conversation caches/summaries)
* the account's spoken-reply audio cache (speech_cache/<account ref>/chunks|manifests and its deleted-chat list)

Everything else is PROTECTED and is never opened for writing here: registered accounts and IDs, usernames and
registration identity, emails, sessions/auth, Google AI Studio and every other per-user API credential, managed
provider credentials, roles and permissions, registration/approval records, Gmail OAuth, profile photos, settings,
personality preferences (profile.json non-derived keys, personality.json, settings.json), the uploaded-file library,
People I Know (profile.json -> social_graph, face references) and explicitly shared person facts.

``assert_resettable`` makes that boundary executable: any attempt to clear a protected table/file/key raises.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

RESET_TABLES = (                       # per-user memories.db, children before parents
    "messages", "chats", "deleted_chats", "chat_deletion_tombstones",
    "memory_review_feedback", "memory_review_queue", "memory_sources", "facts", "memories",
    "internal_notes", "episodes", "monthly_summaries",
)
DERIVED_COMPANION_STATE_KEYS = (
    "last_reflection_at", "last_offline_reflection_at", "topic_mentions",
    "motivation.pending_goals", "motivation.last_checkin", "curiosity.deferred",
)
# profile.json keys that are automatic conversation-derived caches/summaries (never user-set preferences).
DERIVED_PROFILE_DEFAULTS = {
    "conversation_style_signals": {"avg_message_length": None, "emoji_frequency": None, "question_frequency": None,
                                   "hinglish_ratio": None, "samples": 0},
    "curiosity_backlog": [],
    "emotional_milestones": [],
    "emotional_state": None,
    "internal_reflections": [],
    "life_chapters": [],
    "relationship": {"stage": "NEW", "signals": [], "interaction_count": 0},
}
DERIVED_MODULATION_FIELDS = ("current_modulation", "reason")
VECTOR_COLLECTION = "jupiter_memory"

# ── the protected boundary (executable) ──────────────────────────────────────────────────────────────────────────
PROTECTED_TABLES = frozenset({
    "users", "registered_accounts", "auth_sessions", "account_access", "gmail_oauth", "optional_provider_credentials",
    "account_identity", "face_references", "shared_people_facts", "proactive_settings", "registration_requests",
    "approvals", "roles",
})
PROTECTED_FILES = frozenset({
    "users_db.json", "sessions.json", "jupiter_accounts.sqlite3", "registered_accounts.db", "auth.db",
    "account_access.sqlite3", "account_identity.sqlite3", "face_identity.sqlite3", "initiative.db",
    "personality.json", "settings.json", ".account_secret_key",
})
PROTECTED_DIRECTORIES = frozenset({"profile_photos", "attachments"})
PROTECTED_PROFILE_KEYS = frozenset({"social_graph", "preferences", "communication_style", "address_style",
                                    "avoid_addresses", "goals", "preferred_name", "formal_name", "preferred_language",
                                    "personality_provenance", "personality_provenance_baseline", "user_id"})


class ProtectedDataError(RuntimeError):
    """A reset tried to touch protected account/credential/profile data."""


def assert_resettable(*, table: str | None = None, path: Path | None = None, profile_key: str | None = None) -> None:
    if table is not None and (table in PROTECTED_TABLES or table not in RESET_TABLES):
        raise ProtectedDataError(f"Table '{table}' is not a chat/memory table; reset refused.")
    if path is not None:
        parts = {part.casefold() for part in Path(path).parts}
        if Path(path).name.casefold() in {name.casefold() for name in PROTECTED_FILES} or parts & PROTECTED_DIRECTORIES:
            raise ProtectedDataError(f"'{Path(path).name}' is protected; reset refused.")
    if profile_key is not None and (profile_key in PROTECTED_PROFILE_KEYS or profile_key not in DERIVED_PROFILE_DEFAULTS
                                    and profile_key != "personality_modulation"):
        raise ProtectedDataError(f"profile.json key '{profile_key}' is protected; reset refused.")


def safe_user_id(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(email)).strip("._") or "default"


def speech_ref(email: str) -> str:
    return hashlib.sha256(str(email or "").strip().casefold().encode("utf-8")).hexdigest()


def _inside(path: Path, root: Path) -> Path:
    resolved, root = Path(path).resolve(), Path(root).resolve()
    if resolved != root and root not in resolved.parents:
        raise ProtectedDataError(f"Reset target escaped its data root: {resolved}")
    return resolved


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _unjson(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__b64__"}:
        return base64.b64decode(value["__b64__"])
    return value


@dataclass
class AccountTarget:
    email: str
    user_dir: Path
    speech_dir: Path | None


@dataclass
class ResetPlan:
    content_root: Path                    # the runtime's project data/ (users/, speech_cache/, epoch)
    targets: list[AccountTarget]
    counts: dict[str, dict[str, int]] = field(default_factory=dict)


def plan(content_root: Path, emails: Iterable[str] | None, *, known_emails: Iterable[str] = ()) -> ResetPlan:
    """Resolve the selected accounts (None = every account folder) and count what WOULD be removed (read-only).
    ``known_emails`` (from the account registries) maps folders back to emails so speech caches are found too."""
    content_root = Path(content_root).resolve()
    users_root = content_root / "users"
    if emails is None:
        by_folder = {safe_user_id(e): str(e).strip().casefold() for e in known_emails}
        folders = sorted(p for p in users_root.iterdir() if p.is_dir()) if users_root.is_dir() else []
        selected = [(by_folder.get(folder.name, folder.name), folder) for folder in folders]
    else:
        selected = [(email, users_root / safe_user_id(email)) for email in emails]
    targets = []
    for email, folder in selected:
        _inside(folder, users_root)
        speech = (content_root / "speech_cache" / speech_ref(email)) if "@" in email else None
        targets.append(AccountTarget(email=email, user_dir=folder, speech_dir=speech))
    result = ResetPlan(content_root, targets)
    for target in targets:
        result.counts[target.email] = _count(target)
    return result


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _count(target: AccountTarget) -> dict[str, int]:
    counts: dict[str, int] = {}
    database = target.user_dir / "memories.db"
    if database.is_file():
        with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
            present = _tables(connection)
            for table in RESET_TABLES:
                if table in present:
                    counts[table] = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if "companion_state" in present:
                marks = ",".join("?" * len(DERIVED_COMPANION_STATE_KEYS))
                counts["companion_state(derived)"] = connection.execute(
                    f"SELECT COUNT(*) FROM companion_state WHERE state_key IN ({marks})", DERIVED_COMPANION_STATE_KEYS).fetchone()[0]
    counts["vector_records"] = _vector_count_readonly(target)
    counts["profile_derived_keys"] = len(_profile_derived_changes(target.user_dir / "profile.json"))
    counts["speech_cache_files"] = len(_speech_files(target))
    return counts


# ── vectors (Chroma API: records by id, never the database file) ─────────────────────────────────────────────────
def _vector_client(target: AccountTarget):
    vectors = target.user_dir / "vectors"
    if not (vectors / "chroma.sqlite3").is_file():
        return None
    import chromadb
    return chromadb.PersistentClient(path=str(vectors))


def _vector_count_readonly(target: AccountTarget) -> int:
    """Dry-run count straight from chroma.sqlite3 opened read-only (a Chroma client may write on open)."""
    database = target.user_dir / "vectors" / "chroma.sqlite3"
    if not database.is_file():
        return 0
    try:
        with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM embeddings e JOIN segments s ON e.segment_id = s.id "
                "JOIN collections c ON s.collection = c.id WHERE c.name = ? OR c.name LIKE ?",
                (VECTOR_COLLECTION, VECTOR_COLLECTION + "\\_\\_%")).fetchone()[0])
    except sqlite3.Error:
        return -1                                                     # unknown schema: reported, never guessed


def _memory_collections(client) -> list[Any]:
    result = []
    for entry in client.list_collections():
        name = entry if isinstance(entry, str) else getattr(entry, "name", "")
        if name == VECTOR_COLLECTION or name.startswith(VECTOR_COLLECTION + "__"):
            result.append(client.get_collection(name=name))
    return result


def _vector_rows(target: AccountTarget, include=("documents", "metadatas", "embeddings")) -> dict[str, dict]:
    client = _vector_client(target)
    if client is None:
        return {}
    rows = {}
    for collection in _memory_collections(client):
        data = collection.get(include=list(include))
        rows[collection.name] = {key: (list(map(lambda v: v.tolist() if hasattr(v, "tolist") else v, value))
                                       if value is not None else None)
                                 for key, value in data.items() if key in {"ids", *include}}
    return rows


# ── profile.json derived keys ────────────────────────────────────────────────────────────────────────────────────
def _profile_derived_changes(profile_path: Path) -> dict[str, Any]:
    """{key: current value} for derived keys that differ from their clean default (read-only)."""
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    changes = {key: payload[key] for key, default in DERIVED_PROFILE_DEFAULTS.items()
               if key in payload and payload[key] != default}
    modulation = payload.get("personality_modulation")
    if isinstance(modulation, dict) and any(modulation.get(f) is not None for f in DERIVED_MODULATION_FIELDS):
        changes["personality_modulation"] = {f: modulation.get(f) for f in DERIVED_MODULATION_FIELDS}
    return changes


def _speech_files(target: AccountTarget) -> list[Path]:
    if target.speech_dir is None or not target.speech_dir.is_dir():
        return []
    files = [p for sub in ("chunks", "manifests") for p in (target.speech_dir / sub).rglob("*") if p.is_file()]
    marker = target.speech_dir / ".deleted_conversations.json"
    return files + ([marker] if marker.is_file() else [])


# ── backup, execute, restore ─────────────────────────────────────────────────────────────────────────────────────
def backup(reset_plan: ResetPlan, backup_root: Path) -> Path:
    """Private backup of ONLY the records that will be removed (rows, vectors, derived keys, speech files)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(backup_root) / f"chat-memory-reset-{stamp}"
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {"created_at": stamp, "content_root": str(reset_plan.content_root), "accounts": {}}
    for target in reset_plan.targets:
        folder = destination / safe_user_id(target.email)
        folder.mkdir()
        record: dict[str, Any] = {"user_dir": str(target.user_dir), "tables": {}}
        database = target.user_dir / "memories.db"
        if database.is_file():
            with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
                present = _tables(connection)
                for table in RESET_TABLES:
                    if table not in present:
                        continue
                    cursor = connection.execute(f'SELECT * FROM "{table}"')
                    columns = [c[0] for c in cursor.description]
                    rows = [[_jsonable(v) for v in row] for row in cursor.fetchall()]
                    record["tables"][table] = len(rows)
                    (folder / f"{table}.json").write_text(json.dumps({"columns": columns, "rows": rows}), encoding="utf-8")
                if "companion_state" in present:
                    marks = ",".join("?" * len(DERIVED_COMPANION_STATE_KEYS))
                    cursor = connection.execute(f"SELECT * FROM companion_state WHERE state_key IN ({marks})",
                                                DERIVED_COMPANION_STATE_KEYS)
                    columns = [c[0] for c in cursor.description]
                    (folder / "companion_state.json").write_text(json.dumps(
                        {"columns": columns, "rows": [list(r) for r in cursor.fetchall()]}), encoding="utf-8")
        (folder / "profile_derived.json").write_text(json.dumps(_profile_derived_changes(target.user_dir / "profile.json")),
                                                     encoding="utf-8")
        (folder / "vectors.json").write_text(json.dumps(_vector_rows(target)), encoding="utf-8")
        speech_files = _speech_files(target)
        for path in speech_files:
            relative = path.relative_to(target.speech_dir)
            copy = folder / "speech_cache" / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy)
        record["speech_dir"] = str(target.speech_dir) if target.speech_dir else None
        record["speech_files"] = len(speech_files)
        manifest["accounts"][target.email] = record
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return destination


def execute(reset_plan: ResetPlan) -> dict[str, dict[str, int]]:
    """Remove ONLY the planned chat/memory records. Every target is checked against the protected boundary."""
    removed: dict[str, dict[str, int]] = {}
    users_root = reset_plan.content_root / "users"
    for target in reset_plan.targets:
        _inside(target.user_dir, users_root)
        result: dict[str, int] = {}
        database = target.user_dir / "memories.db"
        if database.is_file():
            assert_resettable(path=database)
            with closing(sqlite3.connect(database, timeout=30)) as connection, connection:
                present = _tables(connection)
                for table in RESET_TABLES:
                    if table in present:
                        assert_resettable(table=table)
                        result[table] = connection.execute(f'DELETE FROM "{table}"').rowcount
                if "companion_state" in present:
                    marks = ",".join("?" * len(DERIVED_COMPANION_STATE_KEYS))
                    result["companion_state(derived)"] = connection.execute(
                        f"DELETE FROM companion_state WHERE state_key IN ({marks})", DERIVED_COMPANION_STATE_KEYS).rowcount
        client = _vector_client(target)
        result["vector_records"] = 0
        if client is not None:
            for collection in _memory_collections(client):
                ids = collection.get(include=[])["ids"]
                for start in range(0, len(ids), 500):
                    collection.delete(ids=ids[start:start + 500])
                result["vector_records"] += len(ids)
        result["profile_derived_keys"] = _reset_profile_derived(target.user_dir / "profile.json")
        files = _speech_files(target)
        for path in files:
            _inside(path, target.speech_dir).unlink()
        result["speech_cache_files"] = len(files)
        removed[target.email] = result
    return removed


def _reset_profile_derived(profile_path: Path) -> int:
    changes = _profile_derived_changes(profile_path)
    if not changes:
        return 0
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    for key in changes:
        assert_resettable(profile_key=key)
        if key == "personality_modulation":
            payload[key] = {**payload[key], **{f: None for f in DERIVED_MODULATION_FIELDS}}
        else:
            payload[key] = json.loads(json.dumps(DERIVED_PROFILE_DEFAULTS[key]))
    temporary = profile_path.with_name(profile_path.name + ".reset-tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, profile_path)
    return len(changes)


def restore(backup_dir: Path) -> dict[str, int]:
    """Put a reset back from its backup (rows re-inserted, vectors re-added, derived keys and speech files restored)."""
    manifest = json.loads((Path(backup_dir) / "manifest.json").read_text(encoding="utf-8"))
    restored = {"rows": 0, "vectors": 0, "profile_keys": 0, "speech_files": 0}
    for email, record in manifest["accounts"].items():
        folder = Path(backup_dir) / safe_user_id(email)
        user_dir = Path(record["user_dir"])
        database = user_dir / "memories.db"
        with closing(sqlite3.connect(database, timeout=30)) as connection, connection:
            for table in reversed(RESET_TABLES):
                source = folder / f"{table}.json"
                if not source.is_file():
                    continue
                data = json.loads(source.read_text(encoding="utf-8"))
                marks = ",".join("?" * len(data["columns"]))
                columns = ",".join(f'"{c}"' for c in data["columns"])
                for row in data["rows"]:
                    connection.execute(f'INSERT OR IGNORE INTO "{table}" ({columns}) VALUES ({marks})',
                                       [_unjson(v) for v in row])
                    restored["rows"] += 1
            state = folder / "companion_state.json"
            if state.is_file():
                data = json.loads(state.read_text(encoding="utf-8"))
                marks = ",".join("?" * len(data["columns"]))
                for row in data["rows"]:
                    connection.execute(f"INSERT OR REPLACE INTO companion_state ({','.join(data['columns'])}) VALUES ({marks})", row)
        derived = json.loads((folder / "profile_derived.json").read_text(encoding="utf-8"))
        if derived:
            profile_path = user_dir / "profile.json"
            payload = json.loads(profile_path.read_text(encoding="utf-8"))
            for key, value in derived.items():
                payload[key] = {**payload.get(key, {}), **value} if key == "personality_modulation" else value
            profile_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            restored["profile_keys"] += len(derived)
        vectors = json.loads((folder / "vectors.json").read_text(encoding="utf-8"))
        if vectors:
            import chromadb
            client = chromadb.PersistentClient(path=str(user_dir / "vectors"))
            for name, rows in vectors.items():
                if not rows["ids"]:
                    continue
                collection = client.get_or_create_collection(name=name)
                collection.add(ids=rows["ids"], documents=rows.get("documents"), metadatas=rows.get("metadatas"),
                               embeddings=rows.get("embeddings"))
                restored["vectors"] += len(rows["ids"])
        speech_backup = folder / "speech_cache"
        if speech_backup.is_dir() and record.get("speech_dir"):
            for path in speech_backup.rglob("*"):
                if path.is_file():
                    target = Path(record["speech_dir"]) / path.relative_to(speech_backup)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    restored["speech_files"] += 1
    return restored


# ── protected-data verification ──────────────────────────────────────────────────────────────────────────────────
def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _sqlite_rows(path: Path) -> dict[str, str]:
    """Digest of EVERY table of a protected database (row content hashed; values never returned)."""
    if not path.is_file():
        return {}
    digests = {}
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        for table in sorted(_tables(connection)):
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
            digests[table] = _digest(sorted(repr(row) for row in rows))
    return digests


def _tree_digest(path: Path) -> str | None:
    if not path.is_dir():
        return None
    return _digest(sorted((str(p.relative_to(path)), hashlib.sha256(p.read_bytes()).hexdigest())
                          for p in path.rglob("*") if p.is_file()))


def protected_snapshot(reset_plan: ResetPlan, account_roots: Iterable[Path]) -> dict[str, Any]:
    """Digests of every protected store relevant to this reset. Compare before/after: they must be identical."""
    snapshot: dict[str, Any] = {"account_roots": {}, "users": {}}
    for root in dict.fromkeys(Path(r).resolve() for r in account_roots):
        entry: dict[str, Any] = {}
        for name in sorted(PROTECTED_FILES):
            path = root / name
            if path.suffix in {".sqlite3", ".db"}:
                entry[name] = _sqlite_rows(path)
            elif path.is_file():
                if name == "sessions.json":
                    # Live sessions may be ADDED by normal sign-ins; existing ones must remain.
                    try:
                        entry[name] = sorted(_digest([k, v]) for k, v in json.loads(path.read_text(encoding="utf-8")).items())
                    except (OSError, ValueError, AttributeError):
                        entry[name] = hashlib.sha256(path.read_bytes()).hexdigest()
                else:
                    entry[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in sorted(PROTECTED_DIRECTORIES):
            entry[directory] = _tree_digest(root / directory)
        snapshot["account_roots"][str(root)] = entry
    for target in reset_plan.targets:
        user: dict[str, Any] = {}
        profile = target.user_dir / "profile.json"
        if profile.is_file():
            payload = json.loads(profile.read_text(encoding="utf-8"))
            kept = {k: v for k, v in payload.items() if k not in DERIVED_PROFILE_DEFAULTS and k != "updated_at"}
            if isinstance(kept.get("personality_modulation"), dict):
                kept["personality_modulation"] = {k: v for k, v in kept["personality_modulation"].items()
                                                  if k not in DERIVED_MODULATION_FIELDS}
            user["profile_protected_keys"] = _digest(kept)
        for name in ("personality.json", "settings.json"):
            path = target.user_dir / name
            user[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        other = [p for p in target.user_dir.rglob("*") if p.is_file()
                 and p.name not in {"memories.db", "memories.db-wal", "memories.db-shm", "profile.json"}
                 and "vectors" not in p.relative_to(target.user_dir).parts]
        user["other_user_files"] = _digest(sorted((str(p.relative_to(target.user_dir)),
                                                   hashlib.sha256(p.read_bytes()).hexdigest()) for p in other))
        database = target.user_dir / "memories.db"
        if database.is_file():
            with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
                present = _tables(connection)
                kept_tables = sorted(present - set(RESET_TABLES) - {"companion_state"})
                user["memories_db_other_tables"] = {t: _digest(sorted(repr(r) for r in connection.execute(f'SELECT * FROM "{t}"')))
                                                    for t in kept_tables}
                if "companion_state" in present:
                    marks = ",".join("?" * len(DERIVED_COMPANION_STATE_KEYS))
                    user["companion_state_kept"] = _digest(sorted(repr(r) for r in connection.execute(
                        f"SELECT * FROM companion_state WHERE state_key NOT IN ({marks})", DERIVED_COMPANION_STATE_KEYS)))
        snapshot["users"][target.email] = user
    return snapshot


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Paths of protected entries that changed. sessions.json may only GAIN entries (new sign-ins), never lose them."""
    differences: list[str] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if path.endswith("/sessions.json") and isinstance(a, list) and isinstance(b, list):
            if not set(a) <= set(b):
                differences.append(path + " (existing session removed)")
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                walk(a.get(key), b.get(key), f"{path}/{key}")
        elif a != b:
            differences.append(path)

    walk(before, after, "")
    return differences


# ── per-account content epoch (clients drop ONLY this account's cached chats) ───────────────────────────────────
def bump_account_epoch(content_root: Path, emails: Iterable[str]) -> str:
    path = Path(content_root) / "user_content_epoch.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError):
        payload = {}
    epoch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    accounts = payload.get("accounts") if isinstance(payload.get("accounts"), dict) else {}
    for email in emails:
        accounts[str(email).strip().casefold()] = epoch
    payload["accounts"] = accounts
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return epoch
