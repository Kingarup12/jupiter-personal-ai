-- Marker 159: the canonical JobStatus (services/continuity_engine.py) gains FAILED, CANCELLED and AWAITING_WORKER
-- (a tracked task nothing has started yet) for task-completion notifications. Purely additive: every existing value stays valid and no row is rewritten.
--
-- NOT applied by this change: the active Jupiter runtime has no DATABASE_URL (checked 2026-10-07) and keeps jobs and
-- task notifications in the per-runtime SQLite store (services/task_notifications.py). This file keeps the PostgreSQL
-- constraint in sync with the contract for the day that store is activated. Never run against production without a
-- backup; it is safe to run twice.
BEGIN;
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN (
    'PLANNING','WAITING_OPTIONAL_INPUT','WAITING_REQUIRED_INPUT','RUNNING','REVIEWING','WAITING_CONFIRMATION',
    'EXECUTING','COMPLETED','FAILED_ALL_FREE_ROUTES','FAILED','CANCELLED','AWAITING_WORKER'));
COMMIT;

-- Rollback (only if no row uses the new values; check first):
--   SELECT count(*) FROM jobs WHERE status IN ('FAILED','CANCELLED','AWAITING_WORKER');   -- must be 0
--   BEGIN;
--   ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
--   ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN (
--       'PLANNING','WAITING_OPTIONAL_INPUT','WAITING_REQUIRED_INPUT','RUNNING','REVIEWING','WAITING_CONFIRMATION',
--       'EXECUTING','COMPLETED','FAILED_ALL_FREE_ROUTES'));
--   COMMIT;
