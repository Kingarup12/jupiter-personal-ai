"""Backend-only OAuth client loading for Jupiter Gmail integrations."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class OAuthConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class OAuthClientCredentials:
    client_id: str
    client_secret: str
    client_type: str


def _safe_credential_path(environment_name: str) -> Path | None:
    configured = os.getenv(environment_name, "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser().resolve()
    project_root = Path(__file__).resolve().parent.parent
    public_roots = (
        project_root / "windows_app",
        project_root / "mobile_web",
        project_root / "android_app" / "app" / "src" / "main" / "assets",
        project_root / "downloads",
    )
    if any(root.resolve() == path or root.resolve() in path.parents for root in public_roots):
        raise OAuthConfigurationError("OAuth credentials must remain outside public application assets.")
    return path


def _from_json(environment_name: str, section: str, client_type: str) -> OAuthClientCredentials | None:
    path = _safe_credential_path(environment_name)
    if path is None:
        return None
    if not path.exists():
        # File was configured but no longer exists on disk; fall through to
        # environment-variable fallback instead of blocking the entire flow.
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        source = payload.get(section) if isinstance(payload, dict) else None
        if not isinstance(source, dict):
            raise OAuthConfigurationError(f"The configured OAuth file is not a {client_type} client.")
        client_id = str((source or {}).get("client_id") or "").strip()
        client_secret = str((source or {}).get("client_secret") or "").strip()
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise OAuthConfigurationError("The configured OAuth credential file is unavailable or invalid.") from error
    if not client_id or not client_secret:
        raise OAuthConfigurationError(f"The configured OAuth file is not a {client_type} client.")
    return OAuthClientCredentials(client_id, client_secret, client_type)


def _environment_credentials(normalized: str) -> tuple[str, str]:
    """Read backend-only OAuth values for one platform, newest names first.

    Desktop (loopback redirect) and web (hosted callback) need different Google
    clients, so a platform-specific pair wins over the shared one.
    """
    platform_names = (
        ("GMAIL_DESKTOP_OAUTH_CLIENT_ID", "GMAIL_DESKTOP_OAUTH_CLIENT_SECRET")
        if normalized == "desktop"
        else ("GMAIL_WEB_OAUTH_CLIENT_ID", "GMAIL_WEB_OAUTH_CLIENT_SECRET")
    )
    # A pair is indivisible: never combine one client's ID with another secret.
    pair = tuple(os.getenv(name, "").strip() for name in platform_names)
    if any(pair) or normalized == "desktop":
        return pair
    # Legacy shared variables designate the hosted Web flow, never Desktop.
    for names in (("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET"),
                  ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")):
        pair = tuple(os.getenv(name, "").strip() for name in names)
        if any(pair):
            return pair
    return "", ""


def gmail_oauth_credentials(client_type: str = "web") -> OAuthClientCredentials:
    normalized = "desktop" if str(client_type).casefold() == "desktop" else "web"
    if normalized == "desktop":
        loaded = _from_json("GMAIL_DESKTOP_OAUTH_JSON_PATH", "installed", "desktop")
    else:
        loaded = _from_json("GMAIL_WEB_OAUTH_JSON_PATH", "web", "web")
    if loaded is not None:
        return loaded

    # Stable backend configuration: no developer-local credential file needed.
    # These values are read only by the backend and never leave it.
    client_id, client_secret = _environment_credentials(normalized)
    if client_id and client_secret:
        return OAuthClientCredentials(client_id, client_secret, normalized)
    raise OAuthConfigurationError("Gmail OAuth is not configured for this Jupiter platform.")


def gmail_oauth_configured(client_type: str = "web") -> bool:
    try:
        gmail_oauth_credentials(client_type)
        return True
    except OAuthConfigurationError:
        return False


def gmail_oauth_is_testing() -> bool:
    return os.getenv("GMAIL_OAUTH_PUBLISHING_STATUS", "testing").strip().casefold() != "production"


def gmail_platform_configuration() -> dict[str, bool]:
    """Current clients: WT loopback Desktop; hosted Windows/Android/Web use Web.

    Android's current WebView has no native server-code authorization bridge.
    Its unused native/debug IDs must neither gate the hosted flow nor substitute
    for the Web client. No user grant or successful consent is asserted here.
    """
    web = gmail_oauth_configured("web")
    return {"desktop": gmail_oauth_configured("desktop"), "web": web, "android": web}
