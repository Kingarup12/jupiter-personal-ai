"""Central account capabilities used by authorization and credential routing.

The defaults preserve Jupiter's existing accounts while allowing deployments to
override them from one backend-only JSON environment value.  No client may
promote itself by supplying an email or a view-mode header.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AccountPolicy:
    role: str = "user"
    testing_account: bool = False
    managed_google_credentials: bool = False
    personal_google_credential_ids: tuple[str, ...] = ()


DEFAULT_ACCOUNT_POLICIES = {
    "owner@example.invalid": AccountPolicy(
        role="owner", managed_google_credentials=True,
        personal_google_credential_ids=("maintainer-managed",)
    ),
    "admin-one@example.invalid": AccountPolicy(role="admin"),
    "admin-two@example.invalid": AccountPolicy(role="admin"),
    "tester@example.invalid": AccountPolicy(
        role="owner", testing_account=True, managed_google_credentials=True,
        personal_google_credential_ids=("test-provider-managed", "jupiter-user-google")
    ),
}

# Owner-managed role changes are deliberately limited to ordinary accounts.
# Protected Owner and designated Testing accounts are represented read-only and
# can never be assigned through this policy surface.
MANAGEABLE_ACCOUNT_ROLES = frozenset({"user", "admin"})
_SYSTEM_ROLE_PROTECTED_EMAILS = frozenset({
    email for email, policy in DEFAULT_ACCOUNT_POLICIES.items()
    if policy.role == "owner" or policy.testing_account
})


def _normalized_email(value: str) -> str:
    return str(value or "").strip().casefold()


def _configured_policies() -> dict[str, AccountPolicy]:
    configured = os.getenv("JUPITER_ACCOUNT_POLICIES", "").strip()
    if not configured:
        return DEFAULT_ACCOUNT_POLICIES
    try:
        payload = json.loads(configured)
    except (TypeError, ValueError):
        return DEFAULT_ACCOUNT_POLICIES
    policies = dict(DEFAULT_ACCOUNT_POLICIES)
    if not isinstance(payload, dict):
        return policies
    for email, raw in payload.items():
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "user").strip().casefold()
        if role not in {"owner", "admin", "user", "testing"}:
            continue
        policies[_normalized_email(email)] = AccountPolicy(
            role=role,
            testing_account=bool(raw.get("testing_account", False)),
            # A role alone never grants the special managed user credential.
            # Deployments must opt an account in explicitly, as the built-in
            # owner/testing policies already do.
            managed_google_credentials=bool(raw.get("managed_google_credentials", False)),
            # These existing account-bound keys are not granted by a role or
            # by the general managed-key entitlement flag.
            personal_google_credential_ids=DEFAULT_ACCOUNT_POLICIES.get(
                _normalized_email(email), AccountPolicy()
            ).personal_google_credential_ids,
        )
    return policies


def account_policy(email: str) -> AccountPolicy:
    email_key = _normalized_email(email)
    configured = _configured_policies()
    base = configured.get(email_key, AccountPolicy())
    # Built-in owner/testing accounts are system exceptions and never become
    # mutable through the user-management UI.
    if email_key in _SYSTEM_ROLE_PROTECTED_EMAILS:
        return base
    override = _stored_role_override(email_key)
    return AccountPolicy(role=override) if override else base


def _stored_role_override(email: str) -> str | None:
    """Read the existing account-access store without instantiating it.

    This makes an Owner's change immediately authoritative for every backend
    consumer of account_policy while keeping role data alongside the account,
    not in a second policy database.
    """

    data_dir = Path(
        os.getenv("JUPITER_DATA_DIR", "").strip()
        or Path(__file__).resolve().parents[1] / "data"
    )
    database = data_dir / "account_access.sqlite3"
    if not database.is_file():
        return None
    connection = None
    try:
        connection = sqlite3.connect(database)
        row = connection.execute(
            "SELECT account_role FROM account_access WHERE email=? COLLATE NOCASE",
            (email,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        if connection is not None:
            connection.close()
    role = str(row[0] or "").strip().casefold() if row else ""
    return role if role in MANAGEABLE_ACCOUNT_ROLES else None


def system_role_protected(email: str) -> bool:
    return _normalized_email(email) in _SYSTEM_ROLE_PROTECTED_EMAILS


def authorized_role(email: str) -> str:
    return account_policy(email).role


def is_testing_account(email: str) -> bool:
    return account_policy(email).testing_account


def uses_managed_google_credentials(email: str) -> bool:
    return account_policy(email).managed_google_credentials


TEST_ACCOUNT_EMAIL = next(
    email for email, policy in DEFAULT_ACCOUNT_POLICIES.items() if policy.testing_account
)


# An account-level support exception, intentionally separate from Admin role.
MODEL_DEBUG_ACCOUNT_EMAILS = frozenset({"maintainer@example.invalid"})


def can_view_model_debug(email: str) -> bool:
    """Whether an authenticated account may view its safe model labels."""
    email_key = _normalized_email(email)
    policy = account_policy(email_key)
    return policy.role == "owner" or policy.testing_account or email_key in MODEL_DEBUG_ACCOUNT_EMAILS
