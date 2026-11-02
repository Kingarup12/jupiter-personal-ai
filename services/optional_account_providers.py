"""Optional, encrypted account credentials; never a registration requirement.

This extends the existing account credential store, not process environment or
Vault configuration. No live validation/inference runs merely to save a key.
Model eligibility remains owned by the existing catalog and routing gates.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from cryptography.fernet import InvalidToken


PROVIDERS = (
    ('groq', 'Groq', 'USER_GROQ', ('api_key',)),
    ('openrouter', 'OpenRouter', 'USER_OPENROUTER', ('api_key',)),
    ('cloudflare', 'Cloudflare Workers AI', 'USER_CLOUDFLARE', ('api_key', 'account_id')),
    ('nvidia', 'NVIDIA NIM', 'USER_NVIDIA', ('api_key',)),
    ('opencode', 'OpenCode Zen', 'USER_OPENCODE', ('api_key',)),
    ('bytez', 'Bytez', 'USER_BYTEZ', ('api_key',)),
    ('openweather', 'OpenWeather', 'USER_OPENWEATHER', ('api_key',)),
)
MODEL_USER_SCOPES = {row[0]: row[2] for row in PROVIDERS if row[0] != 'openweather'}


def provider_definitions():
    from config.provider_catalog import routing_gate
    definitions = []
    for p, n, s, f in PROVIDERS:
        allowed, _ = routing_gate(p, '')
        note = (
            'Weather lookups only; existing weather availability checks still apply.' if p == 'openweather'
            else 'Specialist-only; saving does not enable ordinary chat routing.' if p == 'bytez'
            else 'Used only on existing eligible free routes. Saving does not add models or make a provider call.' if allowed
            else 'No automatic free route under the current policy. Saving does not enable paid or restricted models.'
        )
        definitions.append(dict(id=p, name=n, scope=s, fields=list(f), required=False, usage_note=note))
    return definitions


def validate_fields(provider_id, fields):
    definition = next((row for row in PROVIDERS if row[0] == provider_id), None)
    if not definition or not isinstance(fields, dict) or set(fields) != set(definition[3]):
        raise ValueError('invalid_provider_credentials')
    cleaned = {}
    for name, value in fields.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('invalid_provider_credentials')
        cleaned[name] = value.strip()
    if 'account_id' in cleaned and not re.fullmatch(r'[0-9a-fA-F]{32}', cleaned['account_id']):
        raise ValueError('invalid_provider_credentials')
    return cleaned


def migrate(store):
    with store._connect() as connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS optional_provider_credentials (
            email TEXT NOT NULL COLLATE NOCASE, provider TEXT NOT NULL,
            encrypted_fields BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'saved',
            updated_at TEXT NOT NULL, PRIMARY KEY(email, provider))''')


def metadata(store, email):
    with store._connect() as connection:
        rows = {row['provider']: row for row in connection.execute(
            'SELECT provider, status, updated_at FROM optional_provider_credentials WHERE email=? COLLATE NOCASE', (email,))}
    return [{**definition, 'status': rows[definition['id']]['status'] if definition['id'] in rows else 'missing',
             'updated_at': rows[definition['id']]['updated_at'] if definition['id'] in rows else None}
            for definition in provider_definitions()]


def save(store, email, provider_id, fields):
    fields = validate_fields(provider_id, fields)
    encrypted = store._fernet.encrypt(json.dumps(fields, separators=(',', ':')).encode('utf-8'))
    with store._connect() as connection:
        connection.execute('''INSERT INTO optional_provider_credentials(email,provider,encrypted_fields,status,updated_at)
            VALUES(?,?,?,'saved',?) ON CONFLICT(email,provider) DO UPDATE SET
            encrypted_fields=excluded.encrypted_fields,status='saved',updated_at=excluded.updated_at''',
            (email, provider_id, encrypted, datetime.now(timezone.utc).isoformat()))


def remove(store, email, provider_id):
    if provider_id not in {row[0] for row in PROVIDERS}:
        raise ValueError('invalid_provider')
    with store._connect() as connection:
        connection.execute('DELETE FROM optional_provider_credentials WHERE email=? COLLATE NOCASE AND provider=?', (email, provider_id))


def resolve(store, email, scope):
    account_field = scope == 'USER_CLOUDFLARE_ACCOUNT_ID'
    lookup_scope = 'USER_CLOUDFLARE' if account_field else scope
    definition = next((row for row in PROVIDERS if row[2] == lookup_scope), None)
    if definition is None:
        return None
    with store._connect() as connection:
        row = connection.execute('SELECT encrypted_fields,status FROM optional_provider_credentials WHERE email=? COLLATE NOCASE AND provider=?', (email, definition[0])).fetchone()
    if not row or row['status'] == 'needs_reverify':
        return None
    try:
        fields = json.loads(store._fernet.decrypt(row['encrypted_fields']).decode('utf-8'))
        fields = validate_fields(definition[0], fields)
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        return None
    return fields.get('account_id' if account_field else 'api_key')


def record_status(store, email, scope, status):
    definition = next((row for row in PROVIDERS if row[2] == scope), None)
    if not definition or status not in {'ACTIVE', 'AUTH_FAILED'}:
        return
    with store._connect() as connection:
        connection.execute('UPDATE optional_provider_credentials SET status=? WHERE email=? COLLATE NOCASE AND provider=?',
                           ('connected' if status == 'ACTIVE' else 'needs_reverify', email, definition[0]))
