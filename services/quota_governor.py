"""Credential-scoped, pre-execution quota admission for verified-free routes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from core.provider_contracts import AIRequest, CredentialScope, FailureClass, NormalizedFailure, ProviderModel


ROOT = Path(__file__).resolve().parents[1]
LIMITS_PATH = ROOT / "config" / "quota_limits.json"
_INSTANCES: dict[str, "QuotaGovernor"] = {}
_INSTANCES_LOCK = threading.Lock()


@dataclass(frozen=True)
class QuotaAssessment:
    allowed: bool
    pressure: str
    reason: str
    rpm_remaining: int | None
    rpd_remaining: int | None
    tpm_remaining: int | None
    cooldown_until: float | None = None
    concurrent_remaining: int | None = None
    shared_units_reserved: float = 0
    shared_day: str = ""
    reserved_tokens: int = 0
    reserved_minute: int | None = None
    reserved_day: str = ""


class QuotaGovernor:
    """Admit calls before execution without storing prompts, tokens, or raw user IDs."""

    def __init__(self, path: str | Path | None = None, limits_path: str | Path | None = None):
        data_root = Path(os.getenv("JUPITER_DATA_DIR", "").strip() or ROOT / "data")
        self.path = Path(path) if path else data_root / "quota_governor.db"
        self.limits_path = Path(limits_path) if limits_path else LIMITS_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_usage (
                    route_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    credential_scope TEXT NOT NULL,
                    credential_owner_ref TEXT NOT NULL,
                    minute_bucket INTEGER NOT NULL,
                    minute_requests INTEGER NOT NULL DEFAULT 0,
                    minute_tokens INTEGER NOT NULL DEFAULT 0,
                    day_bucket TEXT NOT NULL,
                    day_requests INTEGER NOT NULL DEFAULT 0,
                    day_tokens INTEGER NOT NULL DEFAULT 0,
                    audio_seconds REAL NOT NULL DEFAULT 0,
                    last_success REAL,
                    last_429 REAL,
                    cooldown_until REAL,
                    last_error TEXT
                )
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(quota_usage)").fetchall()
            }
            if "active_requests" not in columns:
                connection.execute(
                    "ALTER TABLE quota_usage ADD COLUMN active_requests INTEGER NOT NULL DEFAULT 0"
                )
            if "day_timezone" not in columns:
                connection.execute("ALTER TABLE quota_usage ADD COLUMN day_timezone TEXT NOT NULL DEFAULT 'UTC'")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_quota_usage (
                    provider_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    credential_scope TEXT NOT NULL,
                    credential_owner_ref TEXT NOT NULL,
                    active_requests INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_rate_limits (
                    route_key TEXT PRIMARY KEY,
                    request_limit INTEGER,
                    token_limit INTEGER,
                    request_remaining INTEGER,
                    token_remaining INTEGER,
                    request_reset TEXT,
                    token_reset TEXT,
                    retry_after TEXT,
                    observed_at REAL NOT NULL,
                    FOREIGN KEY(route_key) REFERENCES quota_usage(route_key) ON DELETE CASCADE
                )
                """
            )
            provider_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(provider_quota_usage)")}
            for name, definition in {
                "day_bucket": "TEXT NOT NULL DEFAULT ''",
                "day_requests": "INTEGER NOT NULL DEFAULT 0",
                "day_units": "REAL NOT NULL DEFAULT 0",
                "actual_units": "REAL NOT NULL DEFAULT 0",
                "estimated_calls": "INTEGER NOT NULL DEFAULT 0",
                "cooldown_until": "REAL",
                "last_error": "TEXT",
                "day_timezone": "TEXT NOT NULL DEFAULT 'UTC'",
            }.items():
                if name not in provider_columns:
                    connection.execute(f"ALTER TABLE provider_quota_usage ADD COLUMN {name} {definition}")
            connection.execute("""CREATE TABLE IF NOT EXISTS credential_quota_blocks (
                credential_key TEXT PRIMARY KEY, cooldown_until REAL NOT NULL, last_error TEXT
            )""")

    def _limits_payload(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.limits_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def limits_for(self, provider: str, model_id: str) -> dict[str, Any]:
        payload = self._limits_payload()
        provider_limits = (payload.get("providers") or {}).get(provider, {})
        model_limits = (payload.get("models") or {}).get(f"{provider}:{model_id}", {})
        base = provider_limits if isinstance(provider_limits, dict) else {}
        override = model_limits if isinstance(model_limits, dict) else {}
        return {**base, **override}

    @staticmethod
    def _owner_ref(scope: str, owner_id: str) -> str:
        if str(scope).startswith('USER_'):
            return hashlib.sha256(str(owner_id or "unknown").casefold().encode("utf-8")).hexdigest()[:20]
        # Managed credential IDs are non-secret policy labels. Keeping them
        # distinct prevents one invalid managed key from poisoning every
        # shared credential's health state.
        return str(owner_id or "shared")[:80]

    @classmethod
    def _quota_ref(cls, model: ProviderModel) -> str:
        scope = cls._scope_value(model)
        owner_ref = cls._owner_ref(scope, getattr(model, "credential_owner_id", "*"))
        if str(getattr(model, "provider", "")).casefold() == "google":
            group = str(getattr(model, "metadata", {}).get("google_quota_group") or "")
            if group:
                return group[:120]
        return owner_ref

    @staticmethod
    def _scope_value(model: ProviderModel) -> str:
        scope = getattr(model, "credential_scope", None)
        if scope is not None:
            return str(getattr(scope, "value", scope))
        provider_scopes = {
            "google": CredentialScope.JUPITER_SHARED_GOOGLE.value,
            "cloudflare": CredentialScope.JUPITER_SHARED_CLOUDFLARE.value,
            "groq": CredentialScope.JUPITER_SHARED_GROQ.value,
            "nvidia": CredentialScope.JUPITER_SHARED_NVIDIA.value,
            "opencode": CredentialScope.JUPITER_SHARED_OPENCODE.value,
            "openrouter": CredentialScope.JUPITER_SHARED_OPENROUTER.value,
            "bytez": CredentialScope.JUPITER_SHARED_BYTEZ.value,
        }
        return provider_scopes.get(str(getattr(model, "provider", "")).casefold(), "JUPITER_SHARED")

    @classmethod
    def _route_identity(cls, model: ProviderModel) -> tuple[str, str]:
        scope = cls._scope_value(model)
        quota_ref = cls._quota_ref(model)
        if model.provider == "google" and model.metadata.get("google_quota_group") and not quota_ref.startswith("user-google:"):
            # An account-bound configured key still shares its managed Google
            # project budget; USER_* execution scope must not duplicate it.
            scope = CredentialScope.JUPITER_SHARED_GOOGLE.value
        raw = f"{model.provider}\0{model.model_id}\0{scope}\0{quota_ref}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest(), quota_ref

    @classmethod
    def _provider_identity(cls, model: ProviderModel) -> tuple[str, str]:
        scope = cls._scope_value(model)
        quota_ref = cls._quota_ref(model)
        if model.provider == "google" and model.metadata.get("google_quota_group") and not quota_ref.startswith("user-google:"):
            scope = CredentialScope.JUPITER_SHARED_GOOGLE.value
        raw = f"{model.provider}\0{scope}\0{quota_ref}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest(), quota_ref

    @classmethod
    def _credential_identity(cls, model: ProviderModel) -> str:
        raw = f"{model.provider}\0{cls._scope_value(model)}\0{model.credential_owner_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _buckets(now: float, provider: str = "") -> tuple[int, str]:
        # Google RPD resets at midnight Pacific, including DST; other existing
        # provider pools retain their UTC reset boundary.
        day = (datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles")).date().isoformat()
               if provider.casefold() == "google" else time.strftime("%Y-%m-%d", time.gmtime(now)))
        return int(now // 60), day

    @classmethod
    def _day_current(cls, row: sqlite3.Row, now: float, provider: str) -> bool:
        day = cls._buckets(now, provider)[1]
        if provider.casefold() == "google" and row["day_timezone"] == "UTC":
            # Conservatively carry current legacy UTC usage across the first
            # migration rather than gifting another daily allowance on upgrade.
            return row["day_bucket"] in {day, cls._buckets(now)[1]}
        return row["day_bucket"] == day

    def _migrate_google_day(self, connection: sqlite3.Connection, table: str, key: str,
                            identity: str, now: float) -> None:
        row = connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (identity,)).fetchone()
        if row and row["provider"].casefold() == "google" and row["day_timezone"] == "UTC":
            bucket = self._buckets(now, "google")[1] if self._day_current(row, now, "google") else row["day_bucket"]
            connection.execute(f"UPDATE {table} SET day_bucket=?, day_timezone=? WHERE {key}=?",
                               (bucket, "America/Los_Angeles", identity))

    def _row(self, connection: sqlite3.Connection, model: ProviderModel, now: float) -> sqlite3.Row:
        route_key, owner_ref = self._route_identity(model)
        minute_bucket, day_bucket = self._buckets(now, model.provider)
        connection.execute(
            """
            INSERT OR IGNORE INTO quota_usage (
                route_key, provider, model_id, credential_scope, credential_owner_ref,
                minute_bucket, day_bucket, day_timezone
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (route_key, model.provider, model.model_id, self._scope_value(model), owner_ref, minute_bucket, day_bucket,
             "America/Los_Angeles" if model.provider.casefold() == "google" else "UTC"),
        )
        self._migrate_google_day(connection, "quota_usage", "route_key", route_key, now)
        row = connection.execute("SELECT * FROM quota_usage WHERE route_key=?", (route_key,)).fetchone()
        if int(row["minute_bucket"]) != minute_bucket or str(row["day_bucket"]) != day_bucket:
            connection.execute(
                """
                UPDATE quota_usage SET
                    minute_bucket=?, minute_requests=CASE WHEN minute_bucket=? THEN minute_requests ELSE 0 END,
                    minute_tokens=CASE WHEN minute_bucket=? THEN minute_tokens ELSE 0 END,
                    day_bucket=?, day_requests=CASE WHEN day_bucket=? THEN day_requests ELSE 0 END,
                    day_tokens=CASE WHEN day_bucket=? THEN day_tokens ELSE 0 END,
                    audio_seconds=CASE WHEN day_bucket=? THEN audio_seconds ELSE 0 END
                WHERE route_key=?
                """,
                (minute_bucket, minute_bucket, minute_bucket, day_bucket, day_bucket, day_bucket, day_bucket, route_key),
            )
            row = connection.execute("SELECT * FROM quota_usage WHERE route_key=?", (route_key,)).fetchone()
        cooldown_until = float(row["cooldown_until"] or 0)
        if cooldown_until and cooldown_until <= now:
            connection.execute(
                "UPDATE quota_usage SET cooldown_until=NULL, "
                "last_error=CASE WHEN upper(COALESCE(last_error, '')) IN "
                "('RATE_LIMIT', 'FREE_QUOTA_EXHAUSTED', 'QUOTA_EXHAUSTED') "
                "THEN NULL ELSE last_error END WHERE route_key=?",
                (route_key,),
            )
            row = connection.execute("SELECT * FROM quota_usage WHERE route_key=?", (route_key,)).fetchone()
        return row

    def _provider_row(self, connection: sqlite3.Connection, model: ProviderModel, now: float) -> sqlite3.Row:
        provider_key, owner_ref = self._provider_identity(model)
        connection.execute(
            """
            INSERT OR IGNORE INTO provider_quota_usage (
                provider_key, provider, credential_scope, credential_owner_ref, updated_at, day_bucket, day_timezone
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (provider_key, model.provider, self._scope_value(model), owner_ref, now, self._buckets(now, model.provider)[1],
             "America/Los_Angeles" if model.provider.casefold() == "google" else "UTC"),
        )
        self._migrate_google_day(connection, "provider_quota_usage", "provider_key", provider_key, now)
        day = self._buckets(now, model.provider)[1]
        connection.execute(
            "UPDATE provider_quota_usage SET day_bucket=?, day_requests=0, day_units=0, "
            "actual_units=0, estimated_calls=0 WHERE provider_key=? AND day_bucket<>?",
            (day, provider_key, day),
        )
        return connection.execute(
            "SELECT * FROM provider_quota_usage WHERE provider_key=?", (provider_key,)
        ).fetchone()

    def _shared_estimate(self, model: ProviderModel, tokens: int) -> float:
        limits = self.limits_for(model.provider, model.model_id)
        estimates = limits.get("estimates") or {}
        if not estimates:
            return 0.0
        capability = str(model.metadata.get("quota_capability") or "")
        if not capability:
            capability = next((item for item in ("image_generation", "audio_transcription", "embeddings", "tts", "safety")
                               if item in model.capabilities), "default")
        return max(0.0, float(estimates.get(capability, estimates.get("default", 0)))) + max(0, tokens) * float(estimates.get("input_token", 0)) + max(0.0, float(model.metadata.get("audio_seconds") or 0)) * float(estimates.get("audio_second", 0))

    @staticmethod
    def actual_shared_units(payload: Any) -> float | None:
        """Only explicit provider Neuron metadata is actual usage, never token counts."""
        if not isinstance(payload, dict):
            return None
        usage = payload.get("usage") or {}
        value = usage.get("neurons") if isinstance(usage, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            return float(value)
        return None

    @staticmethod
    def _header_integer(value: Any) -> int | None:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return max(0, parsed)

    def observe_rate_limit_headers(self, model: ProviderModel, headers: Any) -> None:
        """Persist only documented, non-secret provider quota metadata.

        Groq returns authoritative remaining-request/token values on ordinary
        responses.  Header names are explicitly whitelisted here so cookies,
        authorization values, request bodies, and provider output can never be
        written to the quota database.
        """

        if model.provider.casefold() != "groq":
            return
        normalized = {str(key).casefold(): value for key, value in dict(headers or {}).items()}
        fields = {
            "request_limit": self._header_integer(normalized.get("x-ratelimit-limit-requests")),
            "token_limit": self._header_integer(normalized.get("x-ratelimit-limit-tokens")),
            "request_remaining": self._header_integer(normalized.get("x-ratelimit-remaining-requests")),
            "token_remaining": self._header_integer(normalized.get("x-ratelimit-remaining-tokens")),
            "request_reset": str(normalized.get("x-ratelimit-reset-requests") or "")[:40] or None,
            "token_reset": str(normalized.get("x-ratelimit-reset-tokens") or "")[:40] or None,
            "retry_after": str(normalized.get("retry-after") or "")[:40] or None,
        }
        if not any(value is not None for value in fields.values()):
            return
        now = time.time()
        route_key, _ = self._route_identity(model)
        with self._lock, closing(self._connect()) as connection, connection:
            self._row(connection, model, now)
            connection.execute(
                """
                INSERT INTO provider_rate_limits (
                    route_key, request_limit, token_limit, request_remaining,
                    token_remaining, request_reset, token_reset, retry_after, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_key) DO UPDATE SET
                    request_limit=excluded.request_limit,
                    token_limit=excluded.token_limit,
                    request_remaining=excluded.request_remaining,
                    token_remaining=excluded.token_remaining,
                    request_reset=excluded.request_reset,
                    token_reset=excluded.token_reset,
                    retry_after=excluded.retry_after,
                    observed_at=excluded.observed_at
                """,
                (route_key, *fields.values(), now),
            )

    @staticmethod
    def _observed_remaining(official: Any, field: str, now: float) -> int | None:
        if not official or official[field + "_remaining"] is None:
            return None
        age = now - float(official["observed_at"] or 0)
        reset = str(official[field + "_reset"] or "").strip().lower()
        parts = re.findall(r"([\d.]+)(ms|s|m|h)", reset)
        seconds = 120.0
        if parts and "".join(number + unit for number, unit in parts) == reset:
            try:
                seconds = sum(float(number) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[unit] for number, unit in parts)
            except ValueError:
                seconds = 120.0
        # Observed balances are short-lived snapshots, not permanent limits.
        if age < 0 or age >= min(120.0, seconds):
            return None
        return int(official[field + "_remaining"])

    def _assess_with_connection(
        self,
        connection: sqlite3.Connection,
        model: ProviderModel,
        estimated_tokens: int,
        timestamp: float,
        *,
        priority: str,
    ) -> QuotaAssessment:
        limits = self.limits_for(model.provider, model.model_id)
        row = self._row(connection, model, timestamp)
        provider_row = self._provider_row(connection, model, timestamp)
        route_key, _ = self._route_identity(model)
        official = connection.execute(
            "SELECT * FROM provider_rate_limits WHERE route_key=?", (route_key,)
        ).fetchone()
        cooldown = max(float(row["cooldown_until"] or 0), float(provider_row["cooldown_until"] or 0))
        credential_block = connection.execute(
            "SELECT cooldown_until FROM credential_quota_blocks WHERE credential_key=?",
            (self._credential_identity(model),),
        ).fetchone()
        if credential_block:
            cooldown = max(cooldown, float(credential_block[0] or 0))
        shared_requests = limits.get("shared_daily_requests")
        shared_limit = limits.get("shared_daily_units")
        shared_estimate = self._shared_estimate(model, estimated_tokens)
        shared_remaining = max(0.0, shared_limit - float(provider_row["day_units"])) if isinstance(shared_limit, (int, float)) else None
        shared_request_remaining = max(0, shared_requests - int(provider_row["day_requests"])) if isinstance(shared_requests, int) else None
        # Images/background work cannot consume the emergency/specialist reserve.
        background = priority.casefold() != "interactive"
        preserve_reserve = background or "image_generation" in model.capabilities
        reserve = float(shared_limit or 0) * float(limits.get("reserve_fraction") or 0) if preserve_reserve else 0
        shared_blocked = shared_request_remaining == 0 or (shared_remaining is not None and shared_estimate + reserve > shared_remaining)
        rpm = limits.get("rpm") if isinstance(limits.get("rpm"), int) else None
        rpd = limits.get("rpd") if isinstance(limits.get("rpd"), int) else None
        tpm = limits.get("tpm") if isinstance(limits.get("tpm"), int) else None
        tpd = limits.get("tpd") if isinstance(limits.get("tpd"), int) else None
        tpd_remaining = max(0, tpd - int(row["day_tokens"] or 0)) if tpd is not None else None
        max_concurrent = limits.get("max_concurrent") if isinstance(limits.get("max_concurrent"), int) and limits.get("max_concurrent") > 0 else None
        active_requests = int(provider_row["active_requests"] or 0)
        concurrent_remaining = max(0, max_concurrent - active_requests) if max_concurrent is not None else None
        rpm_remaining = max(0, rpm - int(row["minute_requests"] or 0)) if rpm is not None else None
        rpd_remaining = max(0, rpd - int(row["day_requests"] or 0)) if rpd is not None else None
        tpm_remaining = max(0, tpm - int(row["minute_tokens"] or 0)) if tpm is not None else None
        if model.provider.casefold() == "groq":
            # Groq request headers describe RPD; token headers describe TPM.
            observed = self._observed_remaining(official, "request", timestamp)
            if observed is not None:
                rpd_remaining = observed if rpd_remaining is None else min(rpd_remaining, observed)
            observed = self._observed_remaining(official, "token", timestamp)
            if observed is not None:
                tpm_remaining = observed if tpm_remaining is None else min(tpm_remaining, observed)
        background_blocked = background and (limits.get("background_enabled") is False or active_requests > 0)
        # Optional reasoning yields before exhausting known per-model budgets.
        if background and rpd is not None and rpd_remaining is not None:
            background_blocked = background_blocked or rpd_remaining <= max(1, int(rpd * .15))
        exhausted = (
            cooldown > timestamp
            or rpm_remaining == 0
            or rpd_remaining == 0
            or (tpm_remaining is not None and estimated_tokens > tpm_remaining)
            or (tpd_remaining is not None and estimated_tokens > tpd_remaining)
            or concurrent_remaining == 0
            or background_blocked
            or shared_blocked
        )
        if exhausted:
            reason = (
                "cooldown" if cooldown > timestamp
                else "shared_pool_or_protected_reserve" if shared_blocked
                else "interactive_specialist_only" if background_blocked
                else "concurrent_limit" if concurrent_remaining == 0
                else "configured_limit"
            )
            return QuotaAssessment(
                False, "EXHAUSTED", reason, rpm_remaining, rpd_remaining, tpm_remaining,
                cooldown or None, concurrent_remaining,
            )
        ratios = [
            remaining / limit
            for remaining, limit in ((rpm_remaining, rpm), (rpd_remaining, rpd), (tpm_remaining, tpm), (tpd_remaining, tpd))
            if remaining is not None and limit
        ]
        if shared_remaining is not None and shared_limit:
            ratios.append(shared_remaining / shared_limit)
        if shared_request_remaining is not None and shared_requests:
            ratios.append(shared_request_remaining / shared_requests)
        fraction = min(ratios) if ratios else 1.0
        defaults = self._limits_payload().get("defaults") or {}
        high = float(defaults.get("high_pressure_remaining") or 0.15)
        medium = float(defaults.get("medium_pressure_remaining") or 0.35)
        pressure = "HIGH" if fraction <= high else "MEDIUM" if fraction <= medium else "LOW"
        return QuotaAssessment(
            True, pressure, "admitted", rpm_remaining, rpd_remaining, tpm_remaining,
            None, concurrent_remaining, shared_estimate, self._buckets(timestamp, model.provider)[1],
        )

    def assess(
        self,
        model: ProviderModel,
        estimated_tokens: int = 0,
        *,
        now: float | None = None,
        priority: str = "interactive",
    ) -> QuotaAssessment:
        timestamp = float(now if now is not None else time.time())
        with self._lock, closing(self._connect()) as connection, connection:
            return self._assess_with_connection(
                connection, model, estimated_tokens, timestamp, priority=priority
            )

    def reserve(
        self,
        model: ProviderModel,
        estimated_tokens: int = 0,
        *,
        now: float | None = None,
        priority: str = "interactive",
    ) -> QuotaAssessment:
        timestamp = float(now if now is not None else time.time())
        estimated_tokens = max(0, int(estimated_tokens or 0))
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            assessment = self._assess_with_connection(
                connection, model, estimated_tokens, timestamp, priority=priority
            )
            if not assessment.allowed:
                return assessment
            route_key, _ = self._route_identity(model)
            self._row(connection, model, timestamp)
            connection.execute(
                "UPDATE quota_usage SET minute_requests=minute_requests+1, day_requests=day_requests+1, "
                "minute_tokens=minute_tokens+?, day_tokens=day_tokens+? WHERE route_key=?",
                (estimated_tokens, estimated_tokens, route_key),
            )
            provider_key, _ = self._provider_identity(model)
            connection.execute(
                "UPDATE provider_quota_usage SET day_requests=day_requests+1, day_units=day_units+?, "
                "estimated_calls=estimated_calls+?, updated_at=? WHERE provider_key=?",
                (assessment.shared_units_reserved, int(assessment.shared_units_reserved > 0), timestamp, provider_key),
            )
            if assessment.concurrent_remaining is not None:
                provider_key, _ = self._provider_identity(model)
                connection.execute(
                    "UPDATE provider_quota_usage SET active_requests=active_requests+1, updated_at=? WHERE provider_key=?",
                    (timestamp, provider_key),
                )
        minute, day = self._buckets(timestamp, model.provider)
        return replace(assessment, reserved_tokens=estimated_tokens, reserved_minute=minute, reserved_day=day)

    def release(self, model: ProviderModel) -> None:
        """Release a provider-wide concurrency slot without recording success/failure."""

        limits = self.limits_for(model.provider, model.model_id)
        if not isinstance(limits.get("max_concurrent"), int) or limits["max_concurrent"] <= 0:
            return
        now = time.time()
        provider_key, _ = self._provider_identity(model)
        with self._lock, closing(self._connect()) as connection, connection:
            self._provider_row(connection, model, now)
            connection.execute(
                "UPDATE provider_quota_usage SET active_requests=MAX(active_requests-1, 0), updated_at=? WHERE provider_key=?",
                (now, provider_key),
            )

    def success(self, model: ProviderModel, *, input_tokens: int = 0, output_tokens: int = 0, audio_seconds: float = 0,
                admission: QuotaAssessment | None = None, actual_shared_units: float | None = None) -> None:
        now = time.time()
        route_key, _ = self._route_identity(model)
        tokens = max(0, int(input_tokens or 0))
        if model.provider.casefold() != "google":
            tokens += max(0, int(output_tokens or 0))
        # Absent usage is not evidence of zero usage (Live/older providers).
        if tokens == 0 and admission:
            tokens = admission.reserved_tokens
        with self._lock, closing(self._connect()) as connection, connection:
            row = self._row(connection, model, now)
            minute_tokens = tokens
            day_tokens = tokens
            if admission:
                if row["minute_bucket"] == admission.reserved_minute:
                    minute_tokens -= admission.reserved_tokens
                if row["day_bucket"] == admission.reserved_day:
                    day_tokens -= admission.reserved_tokens
            connection.execute(
                """UPDATE quota_usage SET minute_tokens=MAX(0, minute_tokens+?), day_tokens=MAX(0, day_tokens+?),
                   audio_seconds=audio_seconds+?, last_success=?, last_error=NULL WHERE route_key=?""",
                (minute_tokens, day_tokens, max(0.0, float(audio_seconds or 0)), now, route_key),
            )
            if admission and actual_shared_units is not None and math.isfinite(actual_shared_units) and actual_shared_units >= 0:
                provider_key, _ = self._provider_identity(model)
                self._provider_row(connection, model, now)
                # Do not move a previous day's reservation into today's pool.
                connection.execute(
                    "UPDATE provider_quota_usage SET day_units=MAX(0, day_units+?), actual_units=actual_units+?, "
                    "estimated_calls=MAX(0, estimated_calls-?) WHERE provider_key=? AND day_bucket=?",
                    (actual_shared_units - admission.shared_units_reserved, actual_shared_units,
                     int(admission.shared_units_reserved > 0), provider_key, admission.shared_day),
                )
        self.release(model)

    def failure(self, model: ProviderModel, failure: NormalizedFailure | str) -> None:
        now = time.time()
        if isinstance(failure, NormalizedFailure):
            is_limit = failure.quota_exhausted or failure.failure_class in {FailureClass.RATE_LIMIT, FailureClass.QUOTA_EXHAUSTED}
            error = failure.reason_code or failure.failure_class.value
            cooldown_seconds = int(failure.cooldown_seconds or 0)
        else:
            error = str(failure or "PROVIDER_FAILURE")[:80]
            is_limit = error.upper() in {"RATE_LIMIT", "FREE_QUOTA_EXHAUSTED", "QUOTA_EXHAUSTED"}
            cooldown_seconds = 0
        defaults = self._limits_payload().get("defaults") or {}
        cooldown_seconds = max(cooldown_seconds, int(defaults.get("cooldown_seconds") or 60)) if is_limit else max(0, cooldown_seconds)
        capacity_failure = isinstance(failure, NormalizedFailure) and (
            failure.reason_code == "PROVIDER_CAPACITY" or failure.status_code in {502, 503}
        )
        if capacity_failure:
            cooldown_seconds = max(60, cooldown_seconds)
        route_key, _ = self._route_identity(model)
        with self._lock, closing(self._connect()) as connection, connection:
            self._row(connection, model, now)
            connection.execute(
                """UPDATE quota_usage SET last_429=CASE WHEN ? THEN ? ELSE last_429 END,
                   cooldown_until=CASE WHEN ? THEN MAX(COALESCE(cooldown_until, 0), ?) ELSE cooldown_until END, last_error=? WHERE route_key=?""",
                (int(is_limit), now, int(cooldown_seconds > 0), now + cooldown_seconds, error, route_key),
            )
            limits = self.limits_for(model.provider, model.model_id)
            scope = str(failure.quota_scope or "").upper() if isinstance(failure, NormalizedFailure) else ""
            # MODEL_PROJECT is model-specific. Only explicit account/project
            # failures, or exhausted shared free-credit pools, block siblings.
            shared_failure = is_limit and (scope in {"ACCOUNT", "PROVIDER", "PROJECT"} or (
                limits.get("quota_class") == "shared_account" and scope not in {"MODEL", "PROJECT_MODEL", "MODEL_PROJECT", "CREDENTIAL"}
                and (failure.quota_exhausted if isinstance(failure, NormalizedFailure) else error.upper() in {"FREE_QUOTA_EXHAUSTED", "QUOTA_EXHAUSTED"})
            ))
            auth_failure = (failure.auth_error or failure.failure_class == FailureClass.AUTHENTICATION) if isinstance(failure, NormalizedFailure) else error == "AUTH_FAILED"
            provider_outage = capacity_failure and scope in {"ACCOUNT", "PROVIDER", "PROJECT"}
            credential_failure = auth_failure or (is_limit and scope == "CREDENTIAL")
            if credential_failure:
                connection.execute(
                    "INSERT INTO credential_quota_blocks VALUES (?, ?, ?) ON CONFLICT(credential_key) "
                    "DO UPDATE SET cooldown_until=MAX(cooldown_until, excluded.cooldown_until), last_error=excluded.last_error",
                    (self._credential_identity(model), now + max(60, cooldown_seconds), error),
                )
            project_disabled = isinstance(failure, NormalizedFailure) and failure.reason_code in {"PROJECT_DISABLED", "API_DISABLED"} and scope == "PROJECT"
            if shared_failure or provider_outage or project_disabled:
                provider_key, _ = self._provider_identity(model)
                self._provider_row(connection, model, now)
                connection.execute(
                    "UPDATE provider_quota_usage SET cooldown_until=MAX(COALESCE(cooldown_until, 0), ?), last_error=? WHERE provider_key=?",
                    (now + max(60, cooldown_seconds), error, provider_key),
                )
        self.release(model)

    def order_candidates(self, request: AIRequest, candidates: Iterable[Any]) -> tuple[Any, ...]:
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for index, candidate in enumerate(candidates):
            assessment = self.assess(candidate.model, request.estimated_input_tokens or 0, priority=request.execution_priority)
            if assessment.allowed:
                pressure_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(assessment.pressure, 3)
                logical_key = (candidate.model.provider, candidate.model.model_id)
                group = groups.setdefault(logical_key, {
                    "fallback_stage": candidate.fallback_stage,
                    "quality_band": round(candidate.score / 10),
                    "pressure_rank": pressure_rank,
                    "index": index,
                    "candidates": [],
                })
                group["pressure_rank"] = min(group["pressure_rank"], pressure_rank)
                group["candidates"].append((candidate, pressure_rank))
        ordered_groups = sorted(
            groups.values(),
            key=lambda item: (item["fallback_stage"], -item["quality_band"], item["pressure_rank"], item["index"]),
        )
        # Candidate order inside a logical model is the structural credential priority among ELIGIBLE credentials:
        # the user's own key first, then managed keys by priority. Blocked routes were already removed by assess();
        # a route under HIGH quota pressure yields to healthy ones without permanently changing priority.
        # USER-FIRST across models (user decision 2026-10-07): every eligible route on the current user's own key, in
        # model order, precedes any developer credential on any model.
        result = [candidate for group in ordered_groups for candidate, _ in group["candidates"]
                  if candidate.model.credential_scope.value.startswith('USER_')]
        for group in ordered_groups:
            credentials = sorted((item for item in group["candidates"]
                                  if not item[0].model.credential_scope.value.startswith('USER_')), key=lambda item: (
                1 if item[1] >= 2 else 0,
                item[0].model.metadata.get("credential_priority", 99),
            ))
            result.extend(candidate for candidate, _ in credentials)
        # Model regrouping must not reset the user's canonical account order.
        from config.credential_policy import order_managed_google_candidates
        return order_managed_google_candidates(result)

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM quota_usage ORDER BY provider, model_id, credential_scope").fetchall()
        result = []
        for row in rows:
            model = ProviderModel(
                provider=row["provider"], model_id=row["model_id"], display_name=row["model_id"],
                credential_scope=CredentialScope(row["credential_scope"]), credential_owner_id=row["credential_owner_ref"],
            )
            # Preserve the stored anonymized USER route identity for display-only assessment.
            limits = self.limits_for(row["provider"], row["model_id"])
            # Stored quota_ref is already normalized (and may be a project).
            # Re-hashing it as an owner would lose the corresponding pool.
            raw_pool = f"{row['provider']}\0{row['credential_scope']}\0{row['credential_owner_ref']}"
            provider_key = hashlib.sha256(raw_pool.encode("utf-8")).hexdigest()
            route_key = str(row["route_key"])
            with self._lock, closing(self._connect()) as provider_connection:
                provider_row = provider_connection.execute(
                    "SELECT * FROM provider_quota_usage WHERE provider_key=?", (provider_key,)
                ).fetchone()
                official = provider_connection.execute(
                    "SELECT * FROM provider_rate_limits WHERE route_key=?", (route_key,)
                ).fetchone()
            max_concurrent = limits.get("max_concurrent") if isinstance(limits.get("max_concurrent"), int) else None
            active_requests = int(provider_row["active_requests"] or 0) if provider_row else 0
            minute_bucket, day_bucket = self._buckets(now, row["provider"])
            day_current = self._day_current(row, now, row["provider"])
            minute_requests = int(row["minute_requests"] or 0) if int(row["minute_bucket"]) == minute_bucket else 0
            day_requests = int(row["day_requests"] or 0) if day_current else 0
            minute_tokens = int(row["minute_tokens"] or 0) if int(row["minute_bucket"]) == minute_bucket else 0
            rpm, rpd, tpm, tpd = (limits.get(name) if isinstance(limits.get(name), int) else None for name in ("rpm", "rpd", "tpm", "tpd"))
            remaining = {
                "rpm": max(0, rpm - minute_requests) if rpm is not None else None,
                "rpd": max(0, rpd - day_requests) if rpd is not None else None,
                "tpm": max(0, tpm - minute_tokens) if tpm is not None else None,
                "tpd": max(0, tpd - (int(row["day_tokens"]) if day_current else 0)) if tpd is not None else None,
            }
            if row["provider"].casefold() == "groq":
                observed = self._observed_remaining(official, "request", now)
                if observed is not None:
                    remaining["rpd"] = observed if remaining["rpd"] is None else min(remaining["rpd"], observed)
                observed = self._observed_remaining(official, "token", now)
                if observed is not None:
                    remaining["tpm"] = observed if remaining["tpm"] is None else min(remaining["tpm"], observed)
            ratios = [remaining[name] / limit for name, limit in (("rpm", rpm), ("rpd", rpd), ("tpm", tpm), ("tpd", tpd)) if limit]
            cooldown = float(row["cooldown_until"] or 0)
            pool_current = bool(provider_row and self._day_current(provider_row, now, row["provider"]))
            pool_requests = int(provider_row["day_requests"]) if pool_current else 0
            pool_units = float(provider_row["day_units"]) if pool_current else 0
            pool_rpd = limits.get("shared_daily_requests")
            pool_budget = limits.get("shared_daily_units")
            if isinstance(pool_rpd, int) and pool_rpd:
                ratios.append(max(0, pool_rpd - pool_requests) / pool_rpd)
            if isinstance(pool_budget, (int, float)) and pool_budget:
                ratios.append(max(0, pool_budget - pool_units) / pool_budget)
            if provider_row:
                cooldown = max(cooldown, float(provider_row["cooldown_until"] or 0))
            pressure = "EXHAUSTED" if cooldown > now or any(remaining[name] == 0 for name in ("rpm", "rpd") if remaining[name] is not None) else "HIGH" if ratios and min(ratios) <= 0.15 else "MEDIUM" if ratios and min(ratios) <= 0.35 else "LOW"
            if ratios and min(ratios) <= 0:
                pressure = "EXHAUSTED"
            result.append({
                "provider": row["provider"], "model_id": row["model_id"],
                "credential_scope": row["credential_scope"], "credential_owner": "user" if row["credential_scope"].startswith('USER_') else "shared",
                "pressure": pressure, "limits": {"rpm": rpm, "rpd": rpd, "tpm": tpm, "tpd": tpd}, "remaining": remaining,
                "last_success": row["last_success"], "last_429": row["last_429"],
                "cooldown_until": cooldown or None, "last_error": row["last_error"],
                "source": limits.get("source", "unknown-limit"),
                "quota_class": limits.get("quota_class", "unknown"),
                "shared_pool": {
                    "scope": "provider-account", "unit": limits.get("shared_unit", "requests"),
                    "daily_request_limit": pool_rpd, "daily_unit_limit": pool_budget,
                    "day_requests": pool_requests, "day_units": pool_units,
                    "actual_units": float(provider_row["actual_units"]) if pool_current else 0,
                    "estimated_calls": int(provider_row["estimated_calls"]) if pool_current else 0,
                    "accounting": "actual-when-reported-otherwise-conservative-estimates",
                    "reserve_fraction": limits.get("reserve_fraction", 0),
                    "shared_across_models": limits.get("quota_class") in {"shared_account", "mixed"},
                },
                "concurrency": {
                    "max": max_concurrent,
                    "active": active_requests,
                    "remaining": max(0, max_concurrent - active_requests) if max_concurrent else None,
                },
                "official_headers": ({
                    "request_limit": official["request_limit"],
                    "token_limit": official["token_limit"],
                    "request_remaining": official["request_remaining"],
                    "token_remaining": official["token_remaining"],
                    "request_reset": official["request_reset"],
                    "token_reset": official["token_reset"],
                    "retry_after": official["retry_after"],
                    "observed_at": official["observed_at"],
                } if official else None),
            })
        return result


def quota_governor() -> QuotaGovernor:
    data_root = Path(os.getenv("JUPITER_DATA_DIR", "").strip() or ROOT / "data")
    path = str((data_root / "quota_governor.db").resolve())
    with _INSTANCES_LOCK:
        instance = _INSTANCES.get(path)
        if instance is None:
            instance = QuotaGovernor(path)
            _INSTANCES[path] = instance
        return instance
