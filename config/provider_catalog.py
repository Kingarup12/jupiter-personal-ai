"""Provider catalog: provider -> accounts -> credentials, by environment-variable NAME only.

The catalog (config/provider_catalog.json) never holds a secret. This module resolves a credential value from the
process environment for one provider scope, honouring account/credential priority and an optional explicit pin
(a model row's credential_owner_id). Google keeps its own richer policy in config/credential_policy.py.
Diagnostics expose labels and presence flags only.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("provider_catalog.json")
PROVIDER_KINDS = frozenset({"model_provider", "gateway", "specialist_api", "router"})
PROVIDER_STATUSES = frozenset({"integrated", "planned"})
# Default product policy: a continuing/replenishing free allowance, no card, no paid plan, no one-time trial or
# promotional credit. Every other classification is retained in the catalog but never routed automatically.
FREE_POLICIES = frozenset({
    "recurring", "recurring_noncommercial", "recurring_evaluation", "recurring_development", "conditional_recurring",
    "conditional_development", "promotional_limited_time", "signup_finite", "paid", "mixed", "unverified",
    "not_applicable",
})
DEFAULT_ROUTABLE_FREE_POLICIES = frozenset({"recurring"})
MODEL_POLICIES = frozenset({"excluded", "no_user_content", "free_plan_listed"})
# Requests that may carry the user's own words, files, memories or images. Only PUBLIC requests may use routes whose
# terms forbid personal/confidential data or whose upstream is undetermined.
_USER_CONTENT_PRIVACY = frozenset({"NORMAL_USER_DATA", "PRIVATE", "SENSITIVE", "BIOMETRIC", "SECRET"})
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,80}$")


class CatalogError(ValueError):
    pass


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    providers = payload.get("providers")
    if payload.get("schema_version") != 1 or not isinstance(providers, list):
        raise CatalogError("provider catalog: unsupported schema")
    seen: set[str] = set()
    scopes: set[str] = set()
    for row in providers:
        provider_id = str(row.get("id") or "")
        if not provider_id or provider_id in seen:
            raise CatalogError(f"provider catalog: missing or duplicate id {provider_id!r}")
        seen.add(provider_id)
        if row.get("kind") not in PROVIDER_KINDS or row.get("status") not in PROVIDER_STATUSES:
            raise CatalogError(f"provider catalog: invalid kind/status for {provider_id}")
        if row.get("free_policy", "unverified") not in FREE_POLICIES:
            raise CatalogError(f"provider catalog: invalid free_policy for {provider_id}")
        if row.get("free_policy") in DEFAULT_ROUTABLE_FREE_POLICIES and not (row.get("sources") and row.get("checked_at")):
            raise CatalogError(f"provider catalog: a routable free policy needs an official source and check date ({provider_id})")
        if row.get("kind") == "router" and row.get("credential_scope"):
            raise CatalogError(f"provider catalog: router {provider_id} cannot own an inference credential scope")
        scope = row.get("credential_scope")
        if scope:
            if scope in scopes:
                raise CatalogError(f"provider catalog: scope {scope} claimed twice")
            scopes.add(scope)
        credential_ids: set[str] = set()
        for account in row.get("accounts") or []:
            for name in account.get("account_env") or []:
                if not _ENV_NAME.match(str(name)):
                    raise CatalogError(f"provider catalog: invalid env name in {provider_id}")
            for credential in account.get("credentials") or []:
                credential_id = str(credential.get("id") or "")
                if not credential_id or credential_id in credential_ids:
                    raise CatalogError(f"provider catalog: missing or duplicate credential id in {provider_id}")
                credential_ids.add(credential_id)
                names = list(credential.get("env") or []) + list(credential.get("secret_env") or [])
                if not credential.get("env") or not all(_ENV_NAME.match(str(name)) for name in names):
                    raise CatalogError(f"provider catalog: credential {credential_id} must name env variables only")
    for rule in payload.get("model_policies") or []:
        if rule.get("policy") not in MODEL_POLICIES or rule.get("provider") not in seen:
            raise CatalogError("provider catalog: invalid model policy")
        if not (rule.get("model_ids") or rule.get("model_prefix")) or not (rule.get("sources") and rule.get("checked_at")):
            raise CatalogError("provider catalog: a model policy needs model ids/prefix, a source and a check date")
    return payload


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    return _validate(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))


def providers(kind: str | None = None) -> list[dict[str, Any]]:
    return [row for row in catalog()["providers"] if kind is None or row["kind"] == kind]


def provider(provider_id: str) -> dict[str, Any] | None:
    return next((row for row in catalog()["providers"] if row["id"] == provider_id), None)


def provider_for_scope(scope: str) -> dict[str, Any] | None:
    return next((row for row in catalog()["providers"] if row.get("credential_scope") == str(scope or "")), None)


def credential_slots(scope: str) -> list[dict[str, Any]]:
    """All credentials of one provider scope across its accounts, in priority order (stable within a priority)."""
    row = provider_for_scope(scope)
    slots = []
    for account_index, account in enumerate((row or {}).get("accounts") or []):
        for credential_index, credential in enumerate(account.get("credentials") or []):
            slots.append({**credential, "account_id": account.get("id", ""),
                          "_order": (int(credential.get("priority", 100)), account_index, credential_index)})
    return [{k: v for k, v in slot.items() if k != "_order"} for slot in sorted(slots, key=lambda s: s["_order"])]


def _first_env(names) -> str:
    for name in names or []:
        value = os.getenv(str(name), "").strip()
        if value:
            return value
    return ""


def credential_secret(scope: str, credential_owner_id: str = "") -> str | None:
    """Resolve one secret for a scope. A pinned credential id is honoured exactly (never silently replaced by
    another account's key); otherwise the first configured credential by priority is used."""
    pinned = str(credential_owner_id or "").strip()
    slots = credential_slots(scope)
    if pinned and pinned != "*":
        slot = next((s for s in slots if s.get("id") == pinned), None)
        return (_first_env(slot.get("env")) or None) if slot else None
    for slot in slots:
        value = _first_env(slot.get("env"))
        if value:
            return value
    return None


def _model_rule(provider_id: str, model_id: str) -> dict[str, Any] | None:
    name = str(model_id or "").strip()
    for rule in catalog().get("model_policies") or []:
        if rule.get("provider") != provider_id:
            continue
        if name in (rule.get("model_ids") or []) or (rule.get("model_prefix") and name.startswith(rule["model_prefix"])):
            return rule
    return None


def routing_gate(provider_id: str, model_id: str, privacy_class: str = "NORMAL_USER_DATA") -> tuple[bool, str]:
    """Default free-only eligibility of one provider route for one request, BEFORE credentials or quality ranking.

    Fails closed: an uncatalogued provider, any non-recurring/unknown free policy, an excluded model or a route whose
    terms forbid user content (for a request that carries user content) is not eligible. Returns (allowed, reason).
    """
    row = provider(str(provider_id or ""))
    if row is None:
        return False, "provider_not_catalogued"
    policy = row.get("free_policy", "unverified")
    if policy not in DEFAULT_ROUTABLE_FREE_POLICIES:
        return False, f"free_policy_{policy}"
    rule = _model_rule(row["id"], model_id)
    if rule and rule["policy"] == "excluded":
        return False, f"model_excluded_{rule.get('reason', 'policy')}"
    if rule and rule["policy"] == "no_user_content" and str(privacy_class or "").upper() in _USER_CONTENT_PRIVACY:
        return False, f"terms_forbid_user_content_{rule.get('reason', 'policy')}"
    return True, "recurring_free_policy"


def documented_io(provider_id: str, model_id: str) -> dict[str, Any] | None:
    """Officially documented input/output types for one model (catalog ``model_io``, with source and date). The
    Gemini models list does not report input types, so discovery alone records only text."""
    for row in catalog().get("model_io", []) or []:
        if row.get("provider") == provider_id and row.get("model") == model_id and row.get("source") and row.get("checked_at"):
            return row
    return None


def free_plan_listed(provider_id: str, model_id: str) -> bool:
    """The provider's official FREE-plan table lists this exact model, even though its public catalog shows the
    paid-tier per-token price (e.g. Groq). Only an explicit, sourced, dated catalog rule can say so."""
    rule = _model_rule(str(provider_id or ""), model_id)
    return bool(rule and rule["policy"] == "free_plan_listed")


def free_search_grounding(provider_id: str, model_id: str) -> bool:
    """True only where the official free tier documents Google Search grounding for this exact model."""
    if provider_id != "google":
        return False
    tool = (catalog().get("tool_policies") or {}).get("google_search_grounding") or {}
    return str(model_id or "").strip().removeprefix("models/") in set(tool.get("free_models") or ())


def safe_catalog_status() -> list[dict[str, Any]]:
    """Labels and presence flags only — never values, hashes or partial secrets."""
    rows = []
    for row in providers():
        slots = credential_slots(row["credential_scope"]) if row.get("credential_scope") else []
        rows.append({
            "id": row["id"], "name": row["name"], "kind": row["kind"], "status": row["status"],
            "accounts": len(row.get("accounts") or []) if not row.get("accounts_source") else None,
            "accounts_source": row.get("accounts_source"),
            "credentials": len(slots),
            "configured_credentials": sum(1 for slot in slots if _first_env(slot.get("env"))),
            "free_policy": row.get("free_policy", "unverified"),
            "default_routing": row.get("free_policy") in DEFAULT_ROUTABLE_FREE_POLICIES and row["status"] == "integrated",
            "checked_at": row.get("checked_at"),
        })
    return rows
