"""Backup/recovery policy metadata; this module performs no backup operations."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BackupPolicy:
    enabled: bool
    encryption_required: bool = True
    wal_point_in_time_recovery_required: bool = True
    checksum_required: bool = True
    second_location_required: bool = True
    retention_days: int = 30
    destination_reference: str | None = None
    second_destination_reference: str | None = None


def configured_backup_policy() -> BackupPolicy:
    """Return references/status only; credentials belong to external tooling."""

    enabled = os.getenv("JUPITER_BACKUP_ENABLED", "false").strip().lower() == "true"
    try:
        retention = max(1, int(os.getenv("JUPITER_BACKUP_RETENTION_DAYS", "30")))
    except ValueError:
        retention = 30
    return BackupPolicy(
        enabled=enabled,
        retention_days=retention,
        destination_reference=os.getenv("JUPITER_BACKUP_DESTINATION_REF") or None,
        second_destination_reference=os.getenv("JUPITER_BACKUP_SECOND_DESTINATION_REF") or None,
    )
