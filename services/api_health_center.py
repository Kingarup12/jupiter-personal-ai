"""Local, redacted API health telemetry for Jupiter administrators."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.security import redact, redact_mapping


ROOT = Path(__file__).resolve().parents[1]
DETAIL_RETENTION_SECONDS = 30 * 24 * 60 * 60
DUPLICATE_WINDOW_SECONDS = 10 * 60


def _default_path() -> Path:
    data_root = Path(os.getenv("JUPITER_DATA_DIR", "").strip() or ROOT / "data")
    return data_root / "api_health.db"


class ApiHealthCenter:
    """Stores no prompts, response bodies, credentials, tokens, or raw user IDs."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else _default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_health_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    user_ref TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    actual_model TEXT,
                    task_type TEXT NOT NULL,
                    latency_ms INTEGER,
                    success INTEGER NOT NULL,
                    status_code INTEGER,
                    error_class TEXT,
                    fallback_number INTEGER NOT NULL DEFAULT 0,
                    occurred_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_api_health_attempts_recent
                    ON api_health_attempts(occurred_at DESC, provider, requested_model);

                CREATE TABLE IF NOT EXISTS api_health_events (
                    event_id TEXT PRIMARY KEY,
                    user_ref TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    error_class TEXT NOT NULL,
                    status_code INTEGER,
                    reason_code TEXT,
                    safe_message TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    resolved_at REAL,
                    resolved_by_ref TEXT,
                    retest_requested_at REAL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS ix_api_health_events_duplicate
                    ON api_health_events(
                        user_ref, provider, model_id, task_type, error_class,
                        status_code, reason_code, last_seen DESC
                    );

                CREATE TABLE IF NOT EXISTS api_health_daily (
                    day TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    error_class TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    total_latency_ms INTEGER NOT NULL DEFAULT 0,
                    latency_samples INTEGER NOT NULL DEFAULT 0,
                    fallback_count INTEGER NOT NULL DEFAULT 0,
                    occurrence_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day, provider, model_id, error_class)
                );
                CREATE TABLE IF NOT EXISTS model_route_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at REAL NOT NULL,
                    user_ref TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    generation_mode TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    api_family TEXT NOT NULL,
                    exact_model_id TEXT NOT NULL,
                    credential_label TEXT NOT NULL,
                    route_role TEXT NOT NULL,
                    fallback_reason TEXT,
                    native_live INTEGER NOT NULL DEFAULT 0,
                    camera_live INTEGER NOT NULL DEFAULT 0,
                    skip_reason TEXT,
                    success INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_model_route_events_recent
                    ON model_route_events(occurred_at DESC, task_type, generation_mode);
                CREATE TABLE IF NOT EXISTS live_turn_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at REAL NOT NULL,
                    user_ref TEXT NOT NULL,
                    connection_ref TEXT NOT NULL,
                    provider_turn INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT,
                    exact_model_id TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_live_turn_events_recent ON live_turn_events(occurred_at DESC);
                CREATE TABLE IF NOT EXISTS live_capture_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at REAL NOT NULL,
                    user_ref TEXT NOT NULL,
                    session_ref TEXT NOT NULL,
                    client_at REAL,
                    kind TEXT NOT NULL,
                    state TEXT,
                    fields_json TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_live_capture_events_recent ON live_capture_events(occurred_at DESC);
                """
            )

    @staticmethod
    def _safe_identifier(value: Any, fallback: str = "unknown") -> str:
        cleaned = redact(value, 160).strip()
        return cleaned or fallback

    @staticmethod
    def _user_ref(user_id: str) -> str:
        key = os.getenv("JUPITER_CREDENTIAL_ENCRYPTION_KEY", "").encode("utf-8")
        if not key:
            key = b"jupiter-api-health-local-user-isolation"
        return hmac.new(key, str(user_id).strip().casefold().encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _normalized_error_category(
        error_class: str | None,
        reason_code: str | None,
        status_code: int | None,
    ) -> str:
        raw = str(error_class or "UNKNOWN").strip().upper()
        reason = str(reason_code or "").strip().upper()
        if reason in {"FREE_QUOTA_EXHAUSTED", "QUOTA_EXHAUSTED"}:
            return "QUOTA_EXHAUSTED"
        if reason in {"MODEL_NOT_FREE", "BILLING_REQUIRED", "CREDITS_REQUIRED"}:
            return "PAID_PLAN_REQUIRED"
        if reason in {"AUTH_REVOKED", "TOKEN_REVOKED"}:
            return "AUTH_REVOKED"
        if reason in {"MODEL_DEPRECATED", "DEPRECATED_MODEL"}:
            return "MODEL_DEPRECATED"
        if reason in {"CONTENT_POLICY", "SAFETY_BLOCK"}:
            return "CONTENT_POLICY"
        mapping = {
            "QUOTA_EXHAUSTED": "QUOTA_EXHAUSTED",
            "RATE_LIMIT": "RATE_LIMITED",
            "RATE_LIMITED": "RATE_LIMITED",
            "AUTHENTICATION": "AUTH_INVALID",
            "AUTH_FAILED": "AUTH_INVALID",
            "MODEL_REMOVED": "MODEL_NOT_FOUND",
            "MODEL_NOT_FOUND": "MODEL_NOT_FOUND",
            "UNSUPPORTED": "UNSUPPORTED_MODALITY",
            "BAD_REQUEST": "BAD_REQUEST",
            "TIMEOUT": "TIMEOUT",
            "NETWORK": "NETWORK",
            "SERVER": "PROVIDER_CAPACITY" if status_code in {502, 503, 504} else "NETWORK",
            "PERMISSION": "AUTH_REVOKED" if status_code in {401, 403} else "UNKNOWN",
        }
        return mapping.get(raw, "UNKNOWN")

    def record_attempt(
        self,
        *,
        user_id: str,
        provider: str,
        requested_model: str,
        actual_model: str | None,
        task_type: str,
        latency_ms: int | None,
        success: bool,
        status_code: int | None,
        error_class: str | None,
        fallback_number: int,
        reason_code: str | None = None,
        safe_message: str | None = None,
        details: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        timestamp = float(now if now is not None else time.time())
        user_ref = self._user_ref(user_id)
        safe_provider = self._safe_identifier(provider)
        safe_requested = self._safe_identifier(requested_model)
        safe_actual = self._safe_identifier(actual_model, "") or None
        safe_task = self._safe_identifier(task_type)
        safe_reason = self._safe_identifier(reason_code, "") or None
        safe_error = (
            self._normalized_error_category(error_class, safe_reason, status_code)
            if not success else None
        )
        message = redact(safe_message or safe_error or "Provider request failed.", 240)
        safe_details = redact_mapping(details or {})
        with self._lock, closing(self._connect()) as connection, connection:
            self._aggregate_and_prune(connection, timestamp)
            connection.execute(
                """
                INSERT INTO api_health_attempts (
                    attempt_id, user_ref, provider, requested_model, actual_model,
                    task_type, latency_ms, success, status_code, error_class,
                    fallback_number, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), user_ref, safe_provider, safe_requested, safe_actual,
                    safe_task, latency_ms, int(bool(success)), status_code, safe_error,
                    max(0, int(fallback_number or 0)), timestamp,
                ),
            )
            if success:
                return
            duplicate = connection.execute(
                """
                SELECT event_id FROM api_health_events
                WHERE user_ref=? AND provider=? AND model_id=? AND task_type=?
                  AND error_class=? AND COALESCE(status_code,-1)=COALESCE(?,-1)
                  AND COALESCE(reason_code,'')=COALESCE(?,'')
                  AND resolved_at IS NULL AND last_seen>=?
                ORDER BY last_seen DESC LIMIT 1
                """,
                (
                    user_ref, safe_provider, safe_requested, safe_task, safe_error,
                    status_code, safe_reason, timestamp - DUPLICATE_WINDOW_SECONDS,
                ),
            ).fetchone()
            if duplicate:
                connection.execute(
                    """
                    UPDATE api_health_events
                    SET occurrence_count=occurrence_count+1, last_seen=?, safe_message=?, details_json=?
                    WHERE event_id=?
                    """,
                    (timestamp, message, json.dumps(safe_details, separators=(",", ":")), duplicate["event_id"]),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO api_health_events (
                        event_id, user_ref, provider, model_id, task_type, error_class,
                        status_code, reason_code, safe_message, first_seen, last_seen, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()), user_ref, safe_provider, safe_requested, safe_task,
                        safe_error, status_code, safe_reason, message, timestamp, timestamp,
                        json.dumps(safe_details, separators=(",", ":")),
                    ),
                )

    # Non-secret route observability: which exact model handled which task, in which generation mode.
    # Only labels are accepted (credential ids such as "provider-7-managed" / "user-google", never key material);
    # user identity is stored as the same keyed one-way reference used by the rest of this store.
    GENERATION_MODES = frozenset({"text_chat", "live_native", "live_degraded", "background", "specialist", "unknown"})
    ROUTE_ROLES = frozenset({"primary", "fallback", "skipped"})

    @staticmethod
    def api_family_for(provider: str, generation_mode: str = "") -> str:
        provider = str(provider or "").casefold()
        if provider == "google":
            return "gemini-live-bidi" if generation_mode == "live_native" else "gemini-generate-content"
        if provider in {"groq", "nvidia", "openrouter", "opencode", "bytez"}:
            return "openai-compatible-chat"
        if provider == "cloudflare":
            return "workers-ai"
        return provider or "unknown"

    def record_route_event(
        self,
        *,
        user_id: str,
        task_type: str,
        generation_mode: str,
        provider: str,
        exact_model_id: str,
        credential_label: str,
        route_role: str,
        success: bool,
        fallback_reason: str | None = None,
        native_live: bool = False,
        camera_live: bool = False,
        skip_reason: str | None = None,
        api_family: str | None = None,
        now: float | None = None,
    ) -> None:
        if os.getenv("PYTEST_CURRENT_TEST") and self.path == _default_path():
            return                      # tests use an explicit temporary store; never pollute production telemetry
        mode = generation_mode if generation_mode in self.GENERATION_MODES else "unknown"
        role = route_role if route_role in self.ROUTE_ROLES else "fallback"
        # A text/TTS route can never be recorded as native Live: only the Live transport passes the bidi family
        # explicitly; a derived default is never bidi.
        family = api_family or self.api_family_for(provider, "text")
        if mode == "live_native" and family != "gemini-live-bidi":
            mode = "unknown"
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO model_route_events (
                    event_id, occurred_at, user_ref, task_type, generation_mode, provider, api_family,
                    exact_model_id, credential_label, route_role, fallback_reason, native_live, camera_live,
                    skip_reason, success
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), float(now if now is not None else time.time()), self._user_ref(user_id),
                    self._safe_identifier(task_type), mode, self._safe_identifier(provider),
                    self._safe_identifier(family), self._safe_identifier(exact_model_id),
                    self._safe_identifier(credential_label), role,
                    self._safe_identifier(fallback_reason, "") or None,
                    int(bool(native_live and mode == "live_native")), int(bool(camera_live)),
                    self._safe_identifier(skip_reason, "") or None, int(bool(success)),
                ),
            )

    LIVE_TURN_EVENTS = frozenset({"activity_submitted", "activity_rejected", "first_output", "waiting_nudged",
                                  "turn_complete", "interrupted", "empty_response", "continuation_started",
                                  # CONNECTING stages (labels/durations only): context assembly → candidate → setup
                                  # sent → setup accepted/rejected → live_ready; recovery transitions.
                                  "context_ready", "provider_candidate_selected", "provider_setup_sent",
                                  "provider_setup_accepted", "provider_setup_rejected", "live_ready",
                                  "startup_timeout", "fallback_started", "transport_recovered",
                                  # Marker 183: events the transport already emitted but this allowlist dropped, so
                                  # a silent-provider recovery or an app-owned turn left no server trace; plus the
                                  # measured provider-client stage of startup.
                                  "provider_client_ready", "response_timeout", "activity_submit_stalled",
                                  "app_turn_started", "app_turn_result", "text_turn_submitted",
                                  # Marker 206: why an app-owned / silenced quiet-mode turn ended early.
                                  "app_turn_cancelled", "silent_absorb_expired",
                                  # Marker 197: which device actions the Windows app announced for this session.
                                  "device_capabilities",
                                  # Marker 213: overheard / said-to-Jupiter verdicts (labels only, never the words).
                                  "addressee",
                                  # Marker 217: honest device turns (a retracted draft, a re-sent result note, a
                                  # corrected false claim) - labels only.
                                  "app_turn_draft_retracted", "app_note_resent", "claim_corrected",
                                  # Marker 218: cooperative computer control (labels only).
                                  "cu_mouse_locked", "cu_mouse_released", "cu_user_override_paused",
                                  "cu_idle_resumed", "cu_esc_aborted",
                                  # Marker 220: a Jupiter call's opening line (was emitted but dropped here).
                                  "call_opening_check", "call_opening_sent", "call_opening_failed",
                                  # Marker 221: a real memory save confirmed in Live (count only, never the content).
                                  "memory_saved"})

    def record_live_turn_event(self, *, user_id: str, connection_id: str, provider_turn: int, event: str,
                               detail: str | None = None, exact_model_id: str | None = None) -> None:
        """Native Live turn lifecycle (labels only; never audio, frames, transcripts or keys)."""
        if event not in self.LIVE_TURN_EVENTS:
            return
        if os.getenv("PYTEST_CURRENT_TEST") and self.path == _default_path():
            return
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO live_turn_events (event_id, occurred_at, user_ref, connection_ref, provider_turn, event, detail, exact_model_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), time.time(), self._user_ref(user_id),
                 hashlib.sha256(str(connection_id or "").encode("utf-8")).hexdigest()[:16], int(provider_turn or 0),
                 event, self._safe_identifier(detail, "") or None, self._safe_identifier(exact_model_id, "") or None),
            )

    # Client capture-health trace (WT). Labels/numbers only: never audio, frames, transcripts, identities or keys.
    LIVE_CAPTURE_FIELDS = frozenset({
        "from", "to", "reason", "turn", "attempt", "attemptType", "transportState", "playbackGeneration",
        "receiveLoop", "terminalReason", "watchdogReason", "fallbackReason", "captureGeneration", "captureOwner",
        "micTrackState", "audioProcessorActive", "vadState", "speechActive", "endpointPending",
        "endpointCancelledReason", "activityStartSent", "activityEndSent", "continuation", "transportUsable",
        "ownerValid", "refusal", "sequence", "queue", "gapMs", "postSpeechListeningMs", "endpointDecisionMs",
        "endpointCandidateMs", "providerAcceptedMs", "providerFirstOutputMs", "ackPlayed", "holdState",
        "durationMs", "remainingMs", "ackSource", "transcriptFinal", "transcriptLanguage", "transcriptWords",
        "segmentIndex", "voicedMs", "terminalReason", "visualGroundingRequested", "visualGroundingProvider",
        "visualGroundingSuccess", "visualGroundingFailure", "visualGroundingLatencyMs", "visualGroundingAction",
        # Camera lifecycle + startup phases (labels only): the camera "blink" report had no camera evidence stored.
        "action", "cameraHealth", "cameraWanted", "trackState", "overlayRendered", "overlayErrorReason", "voicedMs",
        "orbTransition", "preRollMs", "onsetToStartMs", "firstVoicedMs", "contextState", "outputLatencyMs",
        "scheduleLeadMs", "receiveToScheduleMs", "idleMs", "keepAlive", "mediaStreamReadyMs",
        "contextRunningMs", "processorReadyMs", "firstPcmMs", "firstFrameMs"})
    LIVE_CAPTURE_MAX_ROWS = 20000

    @staticmethod
    def _capture_label(value: Any) -> Any:
        import re as _re
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(float(value), 1)
        text = str(value or "")
        return text if _re.fullmatch(r"[A-Za-z0-9_.:\-]{0,64}", text) else None

    def record_live_capture_events(self, *, user_id: str, session_id: str, events: list) -> int:
        """Persist a bounded batch of allowlisted Live capture-health trace events."""
        import re as _re
        if os.getenv("PYTEST_CURRENT_TEST") and self.path == _default_path():
            return 0
        rows = []
        now = time.time()
        session_ref = hashlib.sha256(str(session_id or "").encode("utf-8")).hexdigest()[:16]
        user_ref = self._user_ref(user_id)
        for item in list(events or [])[:256]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            if not _re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", kind):
                continue
            state = self._capture_label(item.get("state"))
            fields = {}
            for key in self.LIVE_CAPTURE_FIELDS:
                if key in item:
                    label = self._capture_label(item[key])
                    if label is not None:
                        fields[key] = label
            client_at = item.get("at")
            rows.append((str(uuid4()), now, user_ref, session_ref,
                         float(client_at) if isinstance(client_at, (int, float)) and not isinstance(client_at, bool) else None,
                         kind, state if isinstance(state, str) else None, json.dumps(fields, sort_keys=True)))
        if not rows:
            return 0
        with self._lock, closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT INTO live_capture_events (event_id, occurred_at, user_ref, session_ref, client_at, kind, state, fields_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            connection.execute(
                "DELETE FROM live_capture_events WHERE event_id IN (SELECT event_id FROM live_capture_events "
                "ORDER BY occurred_at DESC, rowid DESC LIMIT -1 OFFSET ?)", (self.LIVE_CAPTURE_MAX_ROWS,))
        return len(rows)

    def live_capture_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT occurred_at, session_ref, client_at, kind, state, fields_json FROM live_capture_events "
                "ORDER BY occurred_at DESC, rowid DESC LIMIT ?", (max(1, min(int(limit), 5000)),)).fetchall()
        return [dict(row) for row in rows]

    def live_turn_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT occurred_at, connection_ref, provider_turn, event, detail, exact_model_id FROM live_turn_events "
                "ORDER BY occurred_at DESC LIMIT ?", (max(1, min(int(limit), 1000)),)).fetchall()
        return [dict(row) for row in rows]

    def route_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT occurred_at, task_type, generation_mode, provider, api_family, exact_model_id,
                       credential_label, route_role, fallback_reason, native_live, camera_live, skip_reason, success
                FROM model_route_events ORDER BY occurred_at DESC LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _aggregate_and_prune(self, connection: sqlite3.Connection, now: float) -> None:
        cutoff = now - DETAIL_RETENTION_SECONDS
        attempts = connection.execute(
            """
            SELECT strftime('%Y-%m-%d', occurred_at, 'unixepoch') AS day,
                   provider, requested_model AS model_id, COALESCE(error_class,'') AS error_class,
                   COUNT(*) AS attempt_count, SUM(success) AS success_count,
                   SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failure_count,
                   SUM(COALESCE(latency_ms,0)) AS total_latency_ms,
                   SUM(CASE WHEN latency_ms IS NULL THEN 0 ELSE 1 END) AS latency_samples,
                   SUM(CASE WHEN fallback_number>0 THEN 1 ELSE 0 END) AS fallback_count
            FROM api_health_attempts WHERE occurred_at<?
            GROUP BY day, provider, requested_model, COALESCE(error_class,'')
            """,
            (cutoff,),
        ).fetchall()
        for row in attempts:
            connection.execute(
                """
                INSERT INTO api_health_daily (
                    day, provider, model_id, error_class, attempt_count, success_count,
                    failure_count, total_latency_ms, latency_samples, fallback_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day, provider, model_id, error_class) DO UPDATE SET
                    attempt_count=attempt_count+excluded.attempt_count,
                    success_count=success_count+excluded.success_count,
                    failure_count=failure_count+excluded.failure_count,
                    total_latency_ms=total_latency_ms+excluded.total_latency_ms,
                    latency_samples=latency_samples+excluded.latency_samples,
                    fallback_count=fallback_count+excluded.fallback_count
                """,
                tuple(row),
            )
        events = connection.execute(
            """
            SELECT strftime('%Y-%m-%d', last_seen, 'unixepoch') AS day,
                   provider, model_id, error_class, SUM(occurrence_count) AS occurrence_count
            FROM api_health_events WHERE last_seen<?
            GROUP BY day, provider, model_id, error_class
            """,
            (cutoff,),
        ).fetchall()
        for row in events:
            connection.execute(
                """
                INSERT INTO api_health_daily (day, provider, model_id, error_class, occurrence_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(day, provider, model_id, error_class) DO UPDATE SET
                    occurrence_count=occurrence_count+excluded.occurrence_count
                """,
                tuple(row),
            )
        connection.execute("DELETE FROM api_health_attempts WHERE occurred_at<?", (cutoff,))
        connection.execute("DELETE FROM api_health_events WHERE last_seen<?", (cutoff,))
        connection.execute("DELETE FROM model_route_events WHERE occurred_at<?", (cutoff,))
        connection.execute("DELETE FROM live_turn_events WHERE occurred_at<?", (cutoff,))

    def summary(self, *, limit: int = 100) -> dict[str, Any]:
        now = time.time()
        cutoff = now - DETAIL_RETENTION_SECONDS
        with self._lock, closing(self._connect()) as connection, connection:
            self._aggregate_and_prune(connection, now)
            provider_rows = connection.execute(
                """
                SELECT provider, requested_model AS model_id, COUNT(*) AS attempts,
                       SUM(success) AS successes,
                       ROUND(AVG(latency_ms), 1) AS average_latency_ms,
                       SUM(CASE WHEN fallback_number>0 THEN 1 ELSE 0 END) AS fallback_count,
                       MAX(occurred_at) AS last_seen
                FROM api_health_attempts WHERE occurred_at>=?
                GROUP BY provider, requested_model
                ORDER BY provider, requested_model
                """,
                (cutoff,),
            ).fetchall()
            events = connection.execute(
                """
                SELECT event_id, provider, model_id, task_type, error_class, status_code,
                       reason_code, safe_message, occurrence_count, first_seen, last_seen,
                       resolved_at, retest_requested_at, substr(user_ref,1,12) AS user_ref
                FROM api_health_events
                ORDER BY resolved_at IS NOT NULL, last_seen DESC LIMIT ?
                """,
                (max(1, min(int(limit), 250)),),
            ).fetchall()
            daily = connection.execute(
                """
                SELECT day, provider, model_id, error_class, attempt_count, success_count,
                       failure_count, total_latency_ms, latency_samples, fallback_count,
                       occurrence_count
                FROM api_health_daily ORDER BY day DESC LIMIT 200
                """
            ).fetchall()
        providers = []
        for row in provider_rows:
            item = dict(row)
            attempts_count = int(item["attempts"] or 0)
            successes = int(item["successes"] or 0)
            item["success_rate"] = round(successes / attempts_count, 4) if attempts_count else None
            providers.append(item)
        total_attempts = sum(int(row["attempts"] or 0) for row in providers)
        total_fallbacks = sum(int(row["fallback_count"] or 0) for row in providers)
        return {
            "retention_days": 30,
            "duplicate_window_minutes": 10,
            "totals": {
                "attempts": total_attempts,
                "fallbacks": total_fallbacks,
                "fallback_rate": round(total_fallbacks / total_attempts, 4) if total_attempts else 0.0,
                "open_events": sum(1 for row in events if row["resolved_at"] is None),
            },
            "providers": providers,
            "events": [dict(row) for row in events],
            "daily_aggregates": [dict(row) for row in daily],
        }

    def resolve(self, event_id: str, actor_user_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE api_health_events SET resolved_at=?, resolved_by_ref=?
                WHERE event_id=? AND resolved_at IS NULL
                """,
                (time.time(), self._user_ref(actor_user_id), event_id),
            )
            return cursor.rowcount == 1

    def request_retest(self, event_id: str) -> dict[str, str] | None:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT provider, model_id FROM api_health_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE api_health_events SET retest_requested_at=? WHERE event_id=?",
                (time.time(), event_id),
            )
            return {"provider": row["provider"], "model_id": row["model_id"]}

    def has_recent_success(
        self,
        provider: str,
        model_id: str,
        task_type: str,
        *,
        max_age_seconds: int = 7 * 24 * 60 * 60,
    ) -> bool:
        """Read previous live health evidence without treating catalog presence as success."""
        cutoff = time.time() - max(60, int(max_age_seconds))
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM api_health_attempts
                WHERE provider=? AND requested_model=? AND task_type=?
                  AND success=1 AND occurred_at>=? LIMIT 1
                """,
                (provider, model_id, task_type, cutoff),
            ).fetchone()
        return row is not None


_INSTANCES: dict[str, ApiHealthCenter] = {}
_INSTANCES_LOCK = threading.Lock()


def api_health_center() -> ApiHealthCenter:
    path = str(_default_path().resolve())
    with _INSTANCES_LOCK:
        instance = _INSTANCES.get(path)
        if instance is None:
            instance = ApiHealthCenter(path)
            _INSTANCES[path] = instance
        return instance
