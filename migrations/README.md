# Jupiter architecture migrations

These migrations are reviewable preparation only. They are never executed by
application import or startup. Apply them manually, in order, to the PostgreSQL
database configured by `DATABASE_URL` after taking and verifying a backup.

The initial migration is additive: it creates missing tables, columns, indexes,
and constraints. It contains no `DROP`, `TRUNCATE`, bulk delete, or data reset.
Row-level-security policies are deliberately not enabled automatically because
the current authentication/database integration must be migrated first.

The second migration adds proactive settings/events, offline directed-message
delivery, and explicit shared-people facts. It stores no provider credentials
and does not make private per-user memory globally readable.

Recommended production sequence (future work, not performed in this pass):

1. Back up the existing authoritative stores.
2. Review table-name compatibility and identity mapping.
3. Apply the migration in a transaction on staging.
4. Backfill existing accounts, conversations, memory, profile, and files with
   stable idempotency keys; do not remove the legacy copy.
5. Reconcile counts/checksums and only then enable the control plane.
6. Configure WAL/PITR and encrypted off-site backup hooks.
