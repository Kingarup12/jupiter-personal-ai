"""Server-side configuration for Jupiter's provider control plane.

This module deliberately exposes presence/status, never secret values.  Client
bundles must not import it; provider adapters run in the backend process only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


SECRET_ENV_NAMES = (
    "JUPITER_GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GEMINI_OWNER_API_KEY",
    "GEMINI_AMAL_API_KEY",
    "GEMINI_AMAN_API_KEY",
    "GEMINI_PHD_API_KEY",
    "GEMINI_PURNIMA_API_KEY",
    "GEMINI_RESHMA_API_KEY",
    "GEMINI_ARPITA_API_KEY",
    "GEMINI_JUPITER_TESTER_API_KEY",
    "JUPITER_GOOGLE_API_KEY_2",
    "JUPITER_GOOGLE_API_KEY_3",
    "GROQ_API_KEY",
    "NVIDIA_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "OPENROUTER_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "BYTEZ_API_KEY",
    "FACEPP_API_KEY",
    "FACEPP_API_SECRET",
    "OPENWEATHER_API_KEY",
    "TAVILY_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "JUPITER_CREDENTIAL_ENCRYPTION_KEY",
    "DATABASE_URL",
)


@dataclass(frozen=True)
class ProviderConfiguration:
    provider: str
    credential_scope: str
    required_environment: tuple[str, ...]
    optional_environment: tuple[str, ...] = ()

    def _is_configured(self, name: str) -> bool:
        return bool(os.getenv(name, "").strip())

    @property
    def enabled(self) -> bool:
        if self.provider == "google_cloud_vision":
            return vision_configuration_state()[0]
        return all(self._is_configured(name) for name in self.required_environment)

    def status(self) -> dict[str, object]:
        """Return safe configuration status without returning credential data."""

        return {
            "provider": self.provider,
            "credential_scope": self.credential_scope,
            "enabled": self.enabled,
            "missing": [
                name for name in self.required_environment
                if not self._is_configured(name)
            ],
        }


PROVIDER_CONFIGURATIONS = {
    "JUPITER_SHARED_GOOGLE": ProviderConfiguration(
        "google", "JUPITER_SHARED_GOOGLE", ("JUPITER_GOOGLE_API_KEY",)
    ),
    "JUPITER_SHARED_GROQ": ProviderConfiguration(
        "groq", "JUPITER_SHARED_GROQ", ("GROQ_API_KEY",)
    ),
    "JUPITER_SHARED_NVIDIA": ProviderConfiguration(
        "nvidia", "JUPITER_SHARED_NVIDIA", ("NVIDIA_API_KEY",)
    ),
    "JUPITER_SHARED_CLOUDFLARE": ProviderConfiguration(
        "cloudflare",
        "JUPITER_SHARED_CLOUDFLARE",
        ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"),
    ),
    "JUPITER_SHARED_OPENCODE": ProviderConfiguration(
        "opencode", "JUPITER_SHARED_OPENCODE", ("OPENCODE_ZEN_API_KEY",)
    ),
    "JUPITER_SHARED_OPENROUTER": ProviderConfiguration(
        "openrouter", "JUPITER_SHARED_OPENROUTER", ("OPENROUTER_API_KEY",)
    ),
    "JUPITER_SHARED_BYTEZ": ProviderConfiguration(
        "bytez", "JUPITER_SHARED_BYTEZ", ("BYTEZ_API_KEY",)
    ),
    "JUPITER_FACEPLUSPLUS": ProviderConfiguration(
        "faceplusplus", "JUPITER_FACEPLUSPLUS", ("FACEPP_API_KEY", "FACEPP_API_SECRET")
    ),
    "JUPITER_OPENWEATHER": ProviderConfiguration(
        "openweather", "JUPITER_OPENWEATHER", ("OPENWEATHER_API_KEY",)
    ),
    "JUPITER_GOOGLE_CLOUD_VISION": ProviderConfiguration(
        "google_cloud_vision",
        "JUPITER_GOOGLE_CLOUD_VISION",
        ("GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"),
    ),
}


def database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def multi_provider_enabled() -> bool:
    """Use explicit configuration, or the verified local registry in auto mode."""

    configured = os.getenv("JUPITER_MULTI_PROVIDER_ENABLED", "auto").strip().lower()
    if configured == "false":
        return False
    if configured == "true":
        return True
    try:
        from services.local_model_registry import LocalModelRegistry
        return LocalModelRegistry().is_ready()
    except Exception:
        return False


def service_account_path() -> Path | None:
    """Return only the configured backend path; never read or expose the JSON."""

    configured = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    return Path(configured).expanduser() if configured else None


def service_account_path_is_backend_only() -> bool:
    path = service_account_path()
    if path is None:
        return False
    resolved = path.resolve()
    project_root = Path(__file__).resolve().parent.parent
    public_roots = (
        project_root / "windows_app",
        project_root / "mobile_web",
        project_root / "android_app" / "app" / "src" / "main" / "assets",
        project_root / "downloads",
    )
    return not any(root.resolve() == resolved or root.resolve() in resolved.parents for root in public_roots)


class VisionConfigurationError(RuntimeError):
    def __init__(self, health: str, message: str):
        super().__init__(message)
        self.health = health


def load_vision_credentials():
    """Validate the existing backend file without token exchange or image calls."""
    path = service_account_path()
    if path is None or not path.is_file():
        raise VisionConfigurationError("CONFIG_MISSING", "Cloud Vision service-account file is unavailable.")
    if not service_account_path_is_backend_only():
        raise VisionConfigurationError("CONFIG_INVALID", "Cloud Vision credentials must remain outside public assets.")
    try:
        from google.oauth2 import service_account
        credentials = service_account.Credentials.from_service_account_file(str(path))
    except ImportError:
        raise VisionConfigurationError("CONFIG_INVALID", "Cloud Vision authentication library is unavailable.") from None
    except Exception:
        raise VisionConfigurationError("CONFIG_INVALID", "Cloud Vision service-account file is invalid.") from None
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip() or credentials.project_id
    if not project or project != credentials.project_id:
        raise VisionConfigurationError("CONFIG_INVALID", "Cloud Vision service account does not match the configured project.")
    return credentials


def vision_configuration_state() -> tuple[bool, str, str]:
    try:
        load_vision_credentials()
    except VisionConfigurationError as error:
        return False, error.health, str(error)
    return True, "CONFIGURED", "Backend service-account credentials loaded; image analysis not verified."


def configuration_status() -> list[dict[str, object]]:
    return [configuration.status() for configuration in PROVIDER_CONFIGURATIONS.values()]
