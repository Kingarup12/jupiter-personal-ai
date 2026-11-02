"""Durable, provider-reported monthly token accounting for Jupiter users."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config.settings import DATA_DIR


DEFAULT_ACCOUNTING_TIMEZONE = "Asia/Calcutta"


def _data_root() -> Path:
    configured = os.getenv("JUPITER_DATA_DIR", "").strip()
    return Path(configured) if configured else Path(DATA_DIR)


def _accounting_zone():
    name = os.getenv("JUPITER_ACCOUNTING_TIMEZONE", DEFAULT_ACCOUNTING_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def current_month_key(moment: datetime | None = None) -> str:
    now = moment or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(_accounting_zone()).strftime("%Y-%m")


class MonthlyTokenUsageStore:
    """Idempotent monthly buckets; prior months remain available as history."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or (_data_root() / "monthly_token_usage.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS monthly_token_usage (
                    user_id TEXT NOT NULL,
                    month_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, month_key, provider, model_id)
                );

                CREATE TABLE IF NOT EXISTS monthly_token_usage_events (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    month_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    source_class TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_monthly_token_usage_month_user
                    ON monthly_token_usage(month_key, user_id);
                CREATE INDEX IF NOT EXISTS idx_monthly_token_events_month_user
                    ON monthly_token_usage_events(month_key, user_id);
                """
            )

    def record(
        self,
        *,
        event_id: str,
        user_id: str,
        provider: str,
        model_id: str,
        input_tokens: int | None,
        output_tokens: int | None,
        source_class: str = "interactive",
        recorded_at: datetime | None = None,
    ) -> bool:
        """Record one successful provider usage event exactly once.

        Missing provider usage is not estimated. The event key protects stream
        finalization/retry paths from double-counting the same successful call.
        """

        if input_tokens is None and output_tokens is None:
            return False
        input_count = max(0, int(input_tokens or 0))
        output_count = max(0, int(output_tokens or 0))
        total = input_count + output_count
        timestamp = recorded_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        month_key = current_month_key(timestamp)
        normalized_user = str(user_id or "system:maintenance").strip().casefold()
        normalized_provider = str(provider or "unknown").strip().casefold()
        normalized_model = str(model_id or "unknown").strip()
        normalized_event = str(event_id or "").strip()
        if not normalized_event:
            return False
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO monthly_token_usage_events (
                    event_id, user_id, month_key, provider, model_id,
                    input_tokens, output_tokens, total_tokens, source_class, recorded_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    normalized_event,
                    normalized_user,
                    month_key,
                    normalized_provider,
                    normalized_model,
                    input_count,
                    output_count,
                    total,
                    str(source_class or "interactive")[:40],
                    timestamp.astimezone(timezone.utc).isoformat(),
                ),
            ).rowcount
            if not inserted:
                return False
            connection.execute(
                """
                INSERT INTO monthly_token_usage (
                    user_id, month_key, provider, model_id,
                    input_tokens, output_tokens, total_tokens, request_count, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id, month_key, provider, model_id) DO UPDATE SET
                    input_tokens=input_tokens+excluded.input_tokens,
                    output_tokens=output_tokens+excluded.output_tokens,
                    total_tokens=total_tokens+excluded.total_tokens,
                    request_count=request_count+1,
                    updated_at=excluded.updated_at
                """,
                (
                    normalized_user,
                    month_key,
                    normalized_provider,
                    normalized_model,
                    input_count,
                    output_count,
                    total,
                    1,
                    timestamp.astimezone(timezone.utc).isoformat(),
                ),
            )
        return True

    def totals_for_month(self, month_key: str | None = None) -> dict[str, int]:
        selected_month = str(month_key or current_month_key())
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM monthly_token_usage
                WHERE month_key=?
                GROUP BY user_id
                """,
                (selected_month,),
            ).fetchall()
        return {str(row["user_id"]): int(row["total_tokens"] or 0) for row in rows}

    def snapshot(self, month_key: str | None = None) -> dict:
        selected_month = str(month_key or current_month_key())
        return {
            "month_key": selected_month,
            "timezone": getattr(_accounting_zone(), "key", "UTC"),
            "totals": self.totals_for_month(selected_month),
        }
