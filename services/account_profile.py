"""Username-only updates to the existing authenticated account registry."""
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from storage.atomic_json import write_json

_LOCK = threading.RLock()


def normalize_username(value):
    if not isinstance(value, str) or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        raise ValueError('username_invalid')
    name = ' '.join(value.split())
    if len(name.replace(' ', '')) < 3 or len(name) > 120:
        raise ValueError('username_invalid')
    return name


def update_registered_username(directory, account, value):
    """Use a server-resolved account, never an email selected by the request.

    Legacy cross-environment lookup is read-only: a test-runtime edit shadows
    its known account in that runtime's existing JSON registry, never writes
    through to another environment's account database.
    """
    name = normalize_username(value)
    root = Path(directory).resolve()
    email = str(account['email']).strip().lower()
    source = Path(account.get('source') or root / 'users_db.json').resolve()
    with _LOCK:
        if source == root / 'jupiter_accounts.sqlite3':
            with closing(sqlite3.connect(f'{source.as_uri()}?mode=rw', uri=True, timeout=15)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                updated = db.execute('UPDATE registered_accounts SET username=? WHERE email=? COLLATE NOCASE', (name, email))
                if updated.rowcount != 1:
                    raise OSError('account_registry_unavailable')
            return name
        registry = root / 'users_db.json'
        try:
            entries = json.loads(registry.read_text(encoding='utf-8-sig')) if registry.exists() else {}
        except (ValueError, OSError):
            raise OSError('account_registry_unavailable') from None
        if not isinstance(entries, dict):
            raise OSError('account_registry_unavailable')
        key = next((key for key in entries if key.strip().lower() == email), None)
        if key is None and source == registry:
            # A vanished local record must not be recreated as an account.
            raise OSError('account_registry_unavailable')
        entries[key or email] = name
        write_json(registry, entries)
    return name
