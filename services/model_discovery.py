"""Metadata-only discovery into the existing local registry. Never inference.

Visibility is scoped to the exact credential used for models.list. Catalog
visibility does not assert successful generation, free quota, or tool support.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from config.model_audit import capability_evidence
from core.provider_adapters import _credential, provider_adapters
from core.provider_contracts import CredentialScope, ProviderFailure


def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def credential_reference(scope, secret):
    return hashlib.sha256((scope + "\0" + secret).encode()).hexdigest()


def normalized_metadata(model):
    return {
        "display_name": model.display_name,
        "available": model.available,
        "input_modalities": sorted(model.input_modalities),
        "output_modalities": sorted(model.output_modalities),
        "capabilities": sorted(model.capabilities),
        "context_window": model.context_window,
        "max_output": model.max_output,
        "lifecycle_status": model.status,
        "supported_generation_methods": model.metadata.get("supported_generation_methods", []),
        "modalities_explicit": bool(model.metadata.get("catalog_modalities_explicit")),
        "thinking": model.metadata.get("thinking"),
        "catalog_free_status": model.metadata.get("catalog_free_status"),
    }


def collect_catalogs(adapters=None):
    results = []
    for adapter in (adapters if adapters is not None else provider_adapters().values()):
        try:
            secret = _credential(adapter.credential_scope)
        except ProviderFailure:
            # No credential means no provider request, not a provider outage.
            continue
        reference = credential_reference(adapter.credential_scope.value, secret)
        item = {"provider": adapter.name, "scope": adapter.credential_scope.value,
                "reference": reference, "checked_at": stamp()}
        try:
            models = adapter.list_models()
            item["models"] = {model.model_id: normalized_metadata(model) for model in models}
            item["status"] = "completed"
        except ProviderFailure as error:
            item.update(status="failed", error=error.failure.failure_class.value)
        except Exception:
            item.update(status="failed", error="discovery-unavailable")
        results.append(item)
    return results


def merge_catalogs(payload, results, *, job_id):
    rows = {(str(row.get("provider")), str(row.get("model_id"))): dict(row)
            for row in payload.get("models", [])}
    access = dict(payload.get("discovery_access") or {})
    counts = {"added": 0, "updated": 0, "unavailable": 0}
    changed = set()
    statuses = []
    for result in results:
        provider, reference = result["provider"], result["reference"]
        statuses.append({"provider": provider, "scope": result["scope"],
                         "status": result["status"], "count": len(result.get("models", {})),
                         "error": result.get("error")})
        if result["status"] != "completed":
            continue  # Failure never removes last-known catalog or health.
        models = result["models"]
        previous_access = access.get(reference, {})
        previous_models = previous_access.get("models") or {}
        if not isinstance(previous_models, dict):
            previous_models = {}
        counts["unavailable"] += len(set(previous_access.get("models", [])) - set(models))
        access[reference] = {"provider": provider, "scope": result["scope"],
                             "checked_at": result["checked_at"], "models": models}
        for model_id, metadata in models.items():
            key = provider, model_id
            old = rows.get(key)
            if old is None:
                counts["added"] += 1
                old = {"provider": provider, "model_id": model_id,
                       "requested_model_id": metadata["display_name"], "status": "candidate",
                       "output_valid": False, "free_status_confidence": "catalog-does-not-prove-free-quota",
                       "roles": [], "capability": "unknown"}
            elif previous_models.get(model_id) != metadata and key not in changed:
                counts["updated"] += 1
            changed.add(key)
            rows[key] = {**old, "discovery": metadata, "last_discovered_at": result["checked_at"]}
    # Logical eligibility is the union of observed scopes, not whichever key
    # happened to refresh last. Execution still checks the exact key below.
    for key in changed:
        variants = [entry["models"][key[1]] for entry in access.values()
                    if entry.get("provider") == key[0]
                    and isinstance(entry.get("models"), dict)
                    and key[1] in entry["models"]]
        merged = dict(rows[key]["discovery"])
        for field in ("input_modalities", "output_modalities", "capabilities", "supported_generation_methods"):
            merged[field] = sorted({value for variant in variants for value in variant.get(field, [])})
        merged["modalities_explicit"] = all(v.get("modalities_explicit") for v in variants)
        merged["context_window"] = max((v.get("context_window") or 0 for v in variants), default=0) or None
        if any(v.get("catalog_free_status") == "paid" for v in variants):
            merged["catalog_free_status"] = "paid"
        rows[key]["discovery"] = merged
    failures = sum(item["status"] == "failed" for item in results)
    successes = len(results) - failures
    refresh = {"job_id": job_id, "state": "partial" if failures and successes else
               "completed" if successes else "failed", "completed_at": stamp(), **counts,
               "providers": statuses, "providers_checked": len(results),
               "providers_succeeded": successes, "providers_failed": failures,
               "started_at": (payload.get("last_refresh") or {}).get("started_at")}
    return {**payload, "models": list(rows.values()), "discovery_access": access,
            "last_refresh": refresh, "last_discovery_at": refresh["completed_at"]}


def routing_metadata(row):
    """Existing explicit audit evidence supplements missing catalog fields."""
    discovery = row.get("discovery") or {}
    label = str(row.get("capability") or "")
    aliases = {"tts": "text-to-speech", "embeddings": "text embeddings",
               "audio_transcription": "automatic speech recognition",
               "vision": "image-to-text", "image_generation": "text-to-image"}
    evidence = capability_evidence(aliases.get(label, label))
    # Older audited local files use the label 'text', not 'text/chat'.
    if row.get("capability") == "text" or not row.get("capability"):
        evidence = {"input_modalities": {"text"}, "output_modalities": {"text"}, "capabilities": set()}
    explicit = bool(discovery.get("modalities_explicit"))
    capabilities = set(evidence["capabilities"]) | set(discovery.get("capabilities") or ())
    # Exact existing specialist contract: Google Gemini text models support
    # Google Search Grounding via google_search tool configuration.
    if row.get("provider") == "google" and label not in {"tts", "embeddings", "image_generation"}:
        capabilities.add("web_search")
    if row.get("provider") == "google" and "memory_extraction" in (row.get("roles") or []):
        capabilities.add("structured_output")
    return {
        "input_modalities": frozenset(discovery.get("input_modalities") or ())
            if explicit else frozenset(evidence["input_modalities"] | set(discovery.get("input_modalities") or ())),
        "output_modalities": frozenset(discovery.get("output_modalities") or ())
            if explicit else frozenset(evidence["output_modalities"] | set(discovery.get("output_modalities") or ())),
        "capabilities": frozenset(capabilities),
        "context_window": discovery.get("context_window"),
    }


def credential_catalog_allows(model, request=None):
    access = model.metadata.get("discovery_access") or {}
    if not access:
        return True
    try:
        reference = credential_reference(model.credential_scope.value, _credential(model.credential_scope, model))
    except ProviderFailure:
        return False
    catalog = access.get(reference)
    # Unchecked credentials retain their pre-existing eligibility/health policy.
    # A successful catalog for A must never be treated as a catalog for B.
    if catalog is None:
        return True
    models = catalog.get("models", {})
    if model.model_id not in models:
        return False
    metadata = models.get(model.model_id) if isinstance(models, dict) else None
    if not metadata:
        return True
    if metadata.get("available") is False:
        return False
    if metadata.get("catalog_free_status") == "paid":
        return False
    if request is not None:
        if metadata.get("context_window") and (request.estimated_input_tokens or 0) > metadata["context_window"]:
            return False
        if metadata.get("modalities_explicit"):
            capabilities = set(metadata.get("capabilities", []))
            if model.provider == "google" and not any(term in model.model_id for term in ("tts", "embedding")):
                capabilities.add("web_search")
            return (request.required_modalities.issubset(metadata.get("input_modalities", []))
                    and request.required_capabilities.issubset(capabilities))
    return True
