"""Restart-safe durable outbox claiming; no worker starts on import."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from services.architecture_store import ArchitectureStore


class DurableOutboxWorker:
    def __init__(self, worker_id: str, store: ArchitectureStore | None = None):
        self.worker_id = worker_id
        self.store = store or ArchitectureStore()

    def claim(self, limit: int = 10) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self.store.transaction() as connection:
            rows = connection.execute(
                """
                WITH candidates AS (
                    SELECT outbox_id FROM durable_outbox
                    WHERE status='PENDING' AND available_at <= now()
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE durable_outbox AS item
                SET status='RUNNING', locked_at=now(), locked_by=%s,
                    attempts=item.attempts+1
                FROM candidates
                WHERE item.outbox_id=candidates.outbox_id
                RETURNING item.*
                """,
                (safe_limit, self.worker_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def complete(self, outbox_id: str) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE durable_outbox SET status='COMPLETED', completed_at=now(),
                    locked_at=NULL, locked_by=NULL
                WHERE outbox_id=%s AND status='RUNNING' AND locked_by=%s
                """,
                (outbox_id, self.worker_id),
            )

    def retry(self, outbox_id: str, error_class: str, delay_seconds: int) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE durable_outbox SET status='PENDING',
                    available_at=now()+(%s * interval '1 second'),
                    last_error_class=%s, locked_at=NULL, locked_by=NULL
                WHERE outbox_id=%s AND status='RUNNING' AND locked_by=%s
                """,
                (max(1, delay_seconds), error_class[:120], outbox_id, self.worker_id),
            )

    def fail(self, outbox_id: str, error_class: str) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE durable_outbox SET status='FAILED', last_error_class=%s,
                    locked_at=NULL, locked_by=NULL
                WHERE outbox_id=%s AND status='RUNNING' AND locked_by=%s
                """,
                (error_class[:120], outbox_id, self.worker_id),
            )

    def recover_abandoned(self, lease_seconds: int = 300) -> int:
        """Return abandoned work to pending after a backend/worker restart."""

        with self.store.transaction() as connection:
            result = connection.execute(
                """
                UPDATE durable_outbox SET status='PENDING', locked_at=NULL,
                    locked_by=NULL, available_at=now(), last_error_class='WORKER_LEASE_EXPIRED'
                WHERE status='RUNNING'
                  AND locked_at < now()-(%s * interval '1 second')
                """,
                (max(30, lease_seconds),),
            )
        return result.rowcount
