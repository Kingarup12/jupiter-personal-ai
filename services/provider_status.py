"""Secret-free, capability-scoped provider configuration and health status."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from config.credential_policy import MANAGED_GOOGLE_CREDENTIALS
from config.provider_settings import PROVIDER_CONFIGURATIONS, vision_configuration_state
from services.oauth_config import gmail_platform_configuration
from core.security import redact


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "config" / "provider_audit_baseline.json"


def _data_path() -> Path:
    data_root = Path(os.getenv("JUPITER_DATA_DIR", "").strip() or ROOT / "data")
    data_root.mkdir(parents=True, exist_ok=True)
    return data_root / "provider_status.db"


def _baseline() -> dict[str, Any]:
    try:
        payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_data_path())
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_runtime_status (
            provider TEXT NOT NULL,
            capability TEXT NOT NULL,
            health TEXT NOT NULL,
            classification TEXT NOT NULL,
            reason TEXT,
            observed_at REAL NOT NULL,
            PRIMARY KEY(provider, capability)
        )
        """
    )
    return connection


def record_provider_status(
    provider: str,
    capability: str,
    health: str,
    classification: str,
    reason: str = "",
) -> None:
    """Persist only normalized state and a short safe reason."""

    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO provider_runtime_status (
                provider, capability, health, classification, reason, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, capability) DO UPDATE SET
                health=excluded.health,
                classification=excluded.classification,
                reason=excluded.reason,
                observed_at=excluded.observed_at
            """,
            (
                str(provider).casefold()[:60],
                str(capability).casefold()[:80],
                str(health).upper()[:80],
                str(classification).upper()[:80],
                redact(" ".join(str(reason or "").split()), 300),
                time.time(),
            ),
        )


def _configuration_state(provider: str) -> tuple[bool, str]:
    key = str(provider or "").casefold()
    if key == "google":
        return any(policy.enabled for policy in MANAGED_GOOGLE_CREDENTIALS), "Managed Google credential pool"
    if key == "gmail":
        platforms = gmail_platform_configuration()
        return all(platforms.values()), ", ".join(
            f"{name.title()}={'configured' if ready else 'missing'}"
            for name, ready in platforms.items()
        )
    if key == "google_cloud_vision":
        configured, _, note = vision_configuration_state()
        return configured, note
    scope_by_provider = {
        configuration.provider: configuration
        for configuration in PROVIDER_CONFIGURATIONS.values()
    }
    configuration = scope_by_provider.get(key)
    return (configuration.enabled, configuration.credential_scope) if configuration else (True, "No credential required")


def provider_status(provider: str, capability: str = "general") -> dict[str, Any]:
    key = str(provider or "").casefold()
    capability_key = str(capability or "general").casefold()
    configured, configuration_note = _configuration_state(key)
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT * FROM provider_runtime_status WHERE provider=? AND capability IN (?, 'general') "
            "ORDER BY capability=? DESC, observed_at DESC LIMIT 1",
            (key, capability_key, capability_key),
        ).fetchone()
    runtime = dict(row) if row else None
    baseline = ((_baseline().get("providers") or {}).get(key) or {})
    evidence = runtime or baseline
    health = str(evidence.get("health") or ("NOT_CHECKED" if configured else "CONFIG_MISSING"))
    classification = str(evidence.get("classification") or (
        "CONFIGURED_ROUTABLE" if configured else "CONFIGURATION_MISSING_INVALID"
    ))
    reason = redact(evidence.get("reason") or "", 300)
    # Configuration observations expire when the *current* loader succeeds;
    # this is readiness, not provider authentication or a user's Gmail grant.
    if configured and health in {"CONFIG_MISSING", "CONFIG_INVALID", "PLATFORM_CONFIG_INCOMPLETE"}:
        health, classification = "CONFIGURED", "SPECIALIST_ONLY"
        reason = "Current configuration is ready; provider operation remains unverified."
    # A new valid runtime success supersedes old audit evidence. Missing local
    # configuration remains authoritative regardless of an old success.
    if not configured:
        health = "CONFIG_MISSING"
        classification = "CONFIGURATION_MISSING_INVALID"
        reason = configuration_note
        if key == "gmail":
            health = "PLATFORM_CONFIG_INCOMPLETE"
        elif key == "google_cloud_vision":
            _, health, reason = vision_configuration_state()
    specialist = key in {"bytez", "faceplusplus", "google_cloud_vision", "gmail", "openweather"}
    routable = configured and health in {"HEALTHY", "VERIFIED_ACTIVE", "NOT_CHECKED"}
    if key == "faceplusplus" and configured and health == "VERIFIED_SPECIALIST":
        routable = True
    if specialist and classification == "SPECIALIST_ONLY" and configured:
        routable = health in {"HEALTHY", "VERIFIED_ACTIVE", "VERIFIED_SPECIALIST"}
    return {
        "provider": key,
        "capability": capability_key,
        "configured": configured,
        "classification": classification,
        "health": health,
        "routable": routable,
        "specialist_only": specialist,
        "configuration": configuration_note,
        "reason": reason,
        "observed_at": evidence.get("observed_at"),
    }


def all_provider_statuses() -> list[dict[str, Any]]:
    return [
        provider_status(name, {"openweather": "weather", "google_cloud_vision": "vision"}.get(name, "general"))
        for name in (
            "google", "groq", "nvidia", "opencode", "openrouter",
            "cloudflare", "bytez", "faceplusplus", "google_cloud_vision",
            "gmail", "openweather",
        )
    ]
