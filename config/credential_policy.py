"""Backend-only credential policy for Google Gemini routes.

This module is the single source of truth for managed credential priority,
per-credential model restrictions, project quota grouping, and paid-model
admission.  It intentionally returns secret values only to backend execution
paths; all diagnostic helpers expose labels and presence flags instead.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Iterable

from core.provider_contracts import CredentialScope


GOOGLE_RESTRICTED_MANAGED_MODELS = frozenset({
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.5-flash",
})


def paid_model_usage_enabled() -> bool:
    """Paid routes remain opt-in even when a provider account has billing."""

    return os.getenv("JUPITER_PAID_MODEL_USAGE", "false").strip().casefold() == "true"


UNCONFIRMED_GOOGLE_QUOTA_GROUP = "google-project-unconfirmed"
GOOGLE_QUOTA_GROUPS_PATH = Path(__file__).with_name("google_quota_groups.json")


def confirmed_google_project(credential_id: str) -> str | None:
    """Owner-confirmed project for a managed credential (from AI Studio/Cloud project metadata), or None.

    The file holds credential labels and project identifiers only, never keys. Unreadable/malformed -> None, which
    falls back to the conservative shared pool (never to extra capacity).
    """

    path = Path(os.getenv("JUPITER_GOOGLE_QUOTA_GROUPS", "") or GOOGLE_QUOTA_GROUPS_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mappings = payload.get("mappings") if isinstance(payload, dict) else None
    entry = mappings.get(credential_id) if isinstance(mappings, dict) else None
    if not isinstance(entry, dict) or not entry.get("confirmed_by") or not entry.get("source"):
        return None
    project = str(entry.get("project") or "").strip()
    return project or None


@dataclass(frozen=True)
class GoogleCredentialPolicy:
    credential_id: str
    label: str
    priority: int
    credential_class: str
    secret_environment: tuple[str, ...]
    project_environment: tuple[str, ...]
    denied_models: frozenset[str] = frozenset()
    allowed_capabilities: frozenset[str] = frozenset({"*"})
    paid_usage_allowed: bool = False

    @property
    def scope(self) -> CredentialScope:
        return CredentialScope.JUPITER_SHARED_GOOGLE

    def secret(self) -> str | None:
        for name in self.secret_environment:
            value = os.getenv(name, "").strip()
            if value:
                return value
        return None

    @property
    def enabled(self) -> bool:
        return bool(self.secret())

    def project_reference(self) -> str | None:
        for name in self.project_environment:
            value = os.getenv(name, "").strip()
            if value:
                return value
        return confirmed_google_project(self.credential_id)

    def quota_group_key(self) -> str:
        """Return an opaque stable project group without exposing project IDs.

        A key whose project is not confirmed must not add supposedly independent quota capacity, so every unconfirmed
        managed key shares ONE conservative pool (one set of per-model counters and cooldowns). Auth health stays per
        credential, so one invalid key still cannot disable the others.
        """

        project = self.project_reference()
        # An unconfirmed key may belong to ANY confirmed project (e.g. the maintainer), so while one configured managed key is
        # unconfirmed, a confirmed project's separate counters could double its real allowance. Until every
        # configured key is mapped, all managed keys share the one conservative per-model pool.
        if not project or unconfirmed_managed_google_keys_present():
            return UNCONFIRMED_GOOGLE_QUOTA_GROUP
        digest = hashlib.sha256(project.casefold().encode("utf-8")).hexdigest()[:16]
        return f"google-project:{digest}"

    def quota_group_label(self) -> str:
        key = self.quota_group_key()
        if key == UNCONFIRMED_GOOGLE_QUOTA_GROUP:
            return (
                "Project confirmed; pooled until every key is mapped (shared conservative pool)"
                if self.project_reference()
                else "Project not confirmed (shared conservative pool)"
            )
        return f"Shared quota group {key.split(':', 1)[-1][:8]}"

    def allows(self, model_id: str, capability: str | None = None, *, paid_only: bool = False) -> bool:
        normalized_model = str(model_id or "").strip().removeprefix("models/")
        if normalized_model in self.denied_models:
            return False
        if paid_only and not (self.paid_usage_allowed and paid_model_usage_enabled()):
            return False
        normalized_capability = str(capability or "").strip().casefold()
        return (
            "*" in self.allowed_capabilities
            or not normalized_capability
            or normalized_capability in self.allowed_capabilities
        )

    def safe_status(self) -> dict[str, object]:
        return {
            "credential_id": self.credential_id,
            "label": self.label,
            "priority": self.priority,
            "credential_class": self.credential_class,
            "configured": self.enabled,
            "quota_group": self.quota_group_label(),
            "project_group_mapped": self.project_reference() is not None,
            "paid_usage_allowed": bool(self.paid_usage_allowed and paid_model_usage_enabled()),
            "denied_models": sorted(self.denied_models),
            "denied_model_count": len(self.denied_models),
            "allowed_capabilities": sorted(self.allowed_capabilities),
        }


# The legacy numbered environment aliases are normalized into one stable
# identity each.  "provider alias" is accepted only as an alias for provider seven; it never
# creates a second logical credential.
#
# JUPITER APPLICATION priority (user decision 2026-10-08; supersedes 2026-10-07), applied among ELIGIBLE credentials
# only: 1 the current user's own key (that user only, never another user's) · 2 provider five · 3 provider three · 4 provider four ·
# 5 a friend · 6 provider six ("PHD") · 7 provider seven · 11 provider eleven · 12 the maintainer (final fallback).
# Jupiter Tester / legacy Jupiter-user keys belong only to the designated Testing account's personal slot,
# never to the shared fallback list. The Owner's own configured the maintainer key also runs first for that Owner only.
# a friend and Jupiter Tester have
# NEW optional slots (GEMINI_ARPITA_* / GEMINI_JUPITER_TESTER_*); absent keys are skipped. Identities are matched by
# these explicit labels only — legacy numbered/alias variables keep the identity they already had (provider alias -> provider seven).
# Auth/quota/cooldown/model restrictions still exclude a credential; priority is never permanently rewritten by a
# temporary failure. One Google project shares quota across its keys: CredentialRouter/AIGateway block the whole
# project quota group after exhaustion instead of cycling keys.
MANAGED_GOOGLE_CREDENTIALS: tuple[GoogleCredentialPolicy, ...] = (
    GoogleCredentialPolicy(
        credential_id="jupiter-user-google",
        label="Jupiter user Google",
        priority=10,
        credential_class="JUPITER_USER_GOOGLE",
        secret_environment=("GEMINI_JUPITER_USER_API_KEY",),
        project_environment=("GEMINI_JUPITER_USER_PROJECT_ID",),
    ),
    GoogleCredentialPolicy(
        credential_id="provider-4-managed",
        label="provider four managed",
        priority=4,
        credential_class="RESTRICTED_MANAGED_GOOGLE",
        secret_environment=("GEMINI_TECHNOLOGY_BRAIN_API_KEY",),
        project_environment=("GEMINI_TECHNOLOGY_BRAIN_PROJECT_ID",),
        denied_models=GOOGLE_RESTRICTED_MANAGED_MODELS,
    ),
    GoogleCredentialPolicy(
        credential_id="provider-3-managed",
        label="provider three managed",
        priority=3,
        credential_class="RESTRICTED_MANAGED_GOOGLE",
        secret_environment=("GEMINI_MOHINI_API_KEY",),
        project_environment=("GEMINI_MOHINI_PROJECT_ID",),
        denied_models=GOOGLE_RESTRICTED_MANAGED_MODELS,
    ),
    GoogleCredentialPolicy(
        credential_id="provider-5-managed",
        label="provider five managed",
        priority=2,
        credential_class="RESTRICTED_MANAGED_GOOGLE",
        secret_environment=("GEMINI_RESHMA_API_KEY",),
        project_environment=("GEMINI_RESHMA_PROJECT_ID",),
        denied_models=GOOGLE_RESTRICTED_MANAGED_MODELS,
    ),
    GoogleCredentialPolicy(
        credential_id="provider-6-managed",
        label="provider six managed",
        priority=6,
        credential_class="RESTRICTED_MANAGED_GOOGLE",
        secret_environment=("GEMINI_PHD_API_KEY", "JUPITER_GOOGLE_API_KEY_3"),
        project_environment=("GEMINI_PHD_PROJECT_ID", "JUPITER_GOOGLE_PROJECT_ID_3"),
        denied_models=GOOGLE_RESTRICTED_MANAGED_MODELS,
    ),
    GoogleCredentialPolicy(
        credential_id="provider-11-managed",
        label="provider eleven managed",
        priority=11,
        credential_class="RESTRICTED_MANAGED_GOOGLE",
        secret_environment=("GEMINI_PURNIMA_API_KEY",),
        project_environment=("GEMINI_PURNIMA_PROJECT_ID",),
        denied_models=GOOGLE_RESTRICTED_MANAGED_MODELS,
    ),
    GoogleCredentialPolicy(
        credential_id="provider-7-managed",
        label="provider seven managed",
        priority=7,
        credential_class="MANAGED_GOOGLE",
        secret_environment=("GEMINI_AMAL_API_KEY", "GEMINI_AMAN_API_KEY", "JUPITER_GOOGLE_API_KEY_2"),
        project_environment=("GEMINI_AMAL_PROJECT_ID", "GEMINI_AMAN_PROJECT_ID", "JUPITER_GOOGLE_PROJECT_ID_2"),
    ),
    GoogleCredentialPolicy(
        credential_id="maintainer-managed",
        label="the maintainer managed",
        priority=12,   # absolute final managed slot (user decision 2026-10-08)
        credential_class="OWNER_MANAGED_GOOGLE",
        secret_environment=(
            "GEMINI_ARUP_API_KEY",
            "GEMINI_OWNER_API_KEY",
            "JUPITER_GOOGLE_API_KEY",
            "GEMINI_API_KEY",
        ),
        project_environment=(
            "GEMINI_OWNER_PROJECT_ID",
            "JUPITER_GOOGLE_PROJECT_ID",
            "GOOGLE_CLOUD_PROJECT",
        ),
    ),
    GoogleCredentialPolicy(
        credential_id="provider-2-managed",
        label="a friend managed",
        priority=5,
        credential_class="RESTRICTED_MANAGED_GOOGLE",
        secret_environment=("GEMINI_ARPITA_API_KEY",),
        project_environment=("GEMINI_ARPITA_PROJECT_ID",),
        denied_models=GOOGLE_RESTRICTED_MANAGED_MODELS,
    ),
    GoogleCredentialPolicy(
        credential_id="test-provider-managed",
        label="Jupiter Tester managed",
        priority=8,
        credential_class="RESTRICTED_MANAGED_GOOGLE",
        secret_environment=("GEMINI_JUPITER_TESTER_API_KEY",),
        project_environment=("GEMINI_JUPITER_TESTER_PROJECT_ID",),
        denied_models=GOOGLE_RESTRICTED_MANAGED_MODELS,
    ),
)


ACCOUNT_ONLY_GOOGLE_CREDENTIAL_IDS = frozenset({"test-provider-managed", "jupiter-user-google"})


def order_managed_google_candidates(candidates):
    """Keep non-Google/user positions; order Google fallbacks by canonical
    account priority across models, with the maintainer absolutely final. No admission,
    health or model eligibility decision is changed here."""
    rows = list(candidates)

    def shared_google(candidate):
        return (candidate.model.provider == "google"
                and not candidate.model.credential_scope.value.startswith("USER_"))

    priorities = {policy.credential_id: policy.priority for policy in MANAGED_GOOGLE_CREDENTIALS}
    google = iter(sorted((c for c in rows if shared_google(c)), key=lambda c: priorities.get(
        c.model.credential_owner_id, c.model.metadata.get("credential_priority", 99))))
    rows = [next(google) if shared_google(c) else c for c in rows]

    def final_arup(candidate):
        return shared_google(candidate) and candidate.model.credential_owner_id == "maintainer-managed"

    return tuple([c for c in rows if not final_arup(c)] + [c for c in rows if final_arup(c)])


def managed_google_policies(
    model_id: str,
    capability: str | None = None,
    *,
    paid_only: bool = False,
    configured_only: bool = True,
    include_jupiter_user_google: bool = False,
) -> tuple[GoogleCredentialPolicy, ...]:
    return tuple(
        policy
        for policy in sorted(MANAGED_GOOGLE_CREDENTIALS, key=lambda item: item.priority)
        if (policy.enabled or not configured_only)
        # Legacy flag retained for callers, but neither Testing key is shared
        # fallback. Resolve it only for its designated authenticated account.
        and policy.credential_id not in ACCOUNT_ONLY_GOOGLE_CREDENTIAL_IDS
        and policy.allows(model_id, capability, paid_only=paid_only)
    )


def managed_google_policy(credential_id: str) -> GoogleCredentialPolicy | None:
    normalized = str(credential_id or "").strip().casefold()
    return next(
        (policy for policy in MANAGED_GOOGLE_CREDENTIALS if policy.credential_id.casefold() == normalized),
        None,
    )


def managed_google_secret(credential_id: str, model_id: str = "", capability: str = "") -> str | None:
    policy = managed_google_policy(credential_id)
    if policy is None or not policy.allows(model_id, capability):
        return None
    return policy.secret()


def model_is_denied(credential_id: str, model_id: str) -> bool:
    policy = managed_google_policy(credential_id)
    return bool(policy and not policy.allows(model_id))


def unconfirmed_managed_google_keys_present() -> bool:
    """True while any CONFIGURED managed Google key has no confirmed project (absent keys never count)."""

    return any(policy.enabled and not policy.project_reference() for policy in MANAGED_GOOGLE_CREDENTIALS)


def safe_google_credential_statuses() -> list[dict[str, object]]:
    return [
        {
            "credential_id": "user-google",
            "label": "User credential",
            "priority": 1,
            "credential_class": "USER_GOOGLE",
            "configured": None,
            "quota_group": "User credential project",
            "project_group_mapped": False,
            "paid_usage_allowed": False,
            "denied_models": [],
            "denied_model_count": 0,
            "allowed_capabilities": ["*"],
        },
        *(policy.safe_status() for policy in MANAGED_GOOGLE_CREDENTIALS),
    ]


def policy_for_environment(name: str) -> GoogleCredentialPolicy | None:
    normalized = str(name or "").strip()
    return next(
        (policy for policy in MANAGED_GOOGLE_CREDENTIALS if normalized in policy.secret_environment),
        None,
    )


def configured_managed_credential_ids() -> tuple[str, ...]:
    return tuple(policy.credential_id for policy in MANAGED_GOOGLE_CREDENTIALS if policy.enabled)


def denied_for_any_policy(model_id: str, credential_ids: Iterable[str]) -> bool:
    return any(model_is_denied(credential_id, model_id) for credential_id in credential_ids)
