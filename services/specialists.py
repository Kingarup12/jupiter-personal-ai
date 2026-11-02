"""Specialist APIs for biometrics, OCR/document vision, and weather facts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from core.security import redact
from core.provider_contracts import ProviderModel
from services.architecture_store import ArchitectureStore
from services.provider_status import record_provider_status
from config.provider_settings import service_account_path_is_backend_only


class SpecialistUnavailable(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason_code: str | None = None,
        attempt_diagnostics: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.reason_code = reason_code
        self.attempt_diagnostics = attempt_diagnostics or []


@dataclass(frozen=True)
class FaceIdentityResult:
    state: str
    confidence: float | None = None
    person_id: str | None = None
    biometric_profile_id: str | None = None
    provider_face_token: str | None = None

    def semantic_annotation(self, position: str) -> str:
        if self.state == "KNOWN" and self.person_id:
            return f"{position} = remembered person {self.person_id}"
        if self.state == "PROBABLE":
            return f"{position} = identity requires verification"
        return f"{position} = unknown person"


class FacePlusPlusSpecialist:
    """Face++ identity only. It never performs general scene interpretation."""

    detect_url = "https://api-us.faceplusplus.com/facepp/v3/detect"
    search_url = "https://api-us.faceplusplus.com/facepp/v3/search"
    faceset_create_url = "https://api-us.faceplusplus.com/facepp/v3/faceset/create"
    faceset_add_url = "https://api-us.faceplusplus.com/facepp/v3/faceset/addface"
    faceset_remove_url = "https://api-us.faceplusplus.com/facepp/v3/faceset/removeface"

    @staticmethod
    def _credentials() -> tuple[str, str]:
        key = os.getenv("FACEPP_API_KEY", "").strip()
        secret = os.getenv("FACEPP_API_SECRET", "").strip()
        if not key or not secret:
            raise SpecialistUnavailable("Face identity is not configured.")
        return key, secret

    def detect(self, image: bytes) -> list[dict[str, Any]]:
        key, secret = self._credentials()
        try:
            response = httpx.post(
                self.detect_url,
                data={"api_key": key, "api_secret": secret, "return_attributes": "quality"},
                files={"image_file": ("face.jpg", image, "image/jpeg")},
                timeout=25.0,
            )
            response.raise_for_status()
            payload = response.json()
            return payload.get("faces", []) if isinstance(payload, dict) else []
        except Exception as error:
            raise SpecialistUnavailable(redact(f"Face detection unavailable: {error}", 240)) from None

    def search(self, face_token: str, faceset_reference: str) -> FaceIdentityResult:
        key, secret = self._credentials()
        try:
            response = httpx.post(
                self.search_url,
                data={
                    "api_key": key,
                    "api_secret": secret,
                    "face_token": face_token,
                    "outer_id": faceset_reference,
                },
                timeout=25.0,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as error:
            raise SpecialistUnavailable(redact(f"Face search unavailable: {error}", 240)) from None
        results = payload.get("results", []) if isinstance(payload, dict) else []
        thresholds = payload.get("thresholds", {}) if isinstance(payload, dict) else {}
        if not results:
            return FaceIdentityResult("UNKNOWN")
        best = results[0]
        matched_token = str(best.get("face_token") or "") or None
        confidence = float(best.get("confidence") or 0)
        known_threshold = float(thresholds.get("1e-5") or thresholds.get("1e-4") or 90)
        probable_threshold = float(thresholds.get("1e-3") or known_threshold * 0.85)
        if confidence >= known_threshold:
            return FaceIdentityResult("KNOWN", confidence=confidence, provider_face_token=matched_token)
        if confidence >= probable_threshold:
            return FaceIdentityResult("PROBABLE", confidence=confidence, provider_face_token=matched_token)
        return FaceIdentityResult("UNKNOWN", confidence=confidence, provider_face_token=matched_token)

    def _faceset_post(self, url: str, **fields) -> dict[str, Any]:
        key, secret = self._credentials()
        try:
            response = httpx.post(url, data={"api_key": key, "api_secret": secret, **fields}, timeout=25.0)
            payload = response.json() if response.content else {}
            # create is idempotent at Jupiter level; Face++ reports an existing
            # outer_id as an error even though the required FaceSet is ready.
            if response.status_code >= 400 and str((payload or {}).get("error_message") or "").casefold() not in {"outer_id_exist"}:
                response.raise_for_status()
            return payload if isinstance(payload, dict) else {}
        except Exception as error:
            raise SpecialistUnavailable(redact(f"FaceSet operation unavailable: {error}", 240)) from None

    def ensure_faceset(self, outer_id: str) -> None:
        self._faceset_post(self.faceset_create_url, outer_id=outer_id, display_name="Jupiter private faces")

    def add_faces(self, outer_id: str, face_tokens: list[str]) -> None:
        if face_tokens:
            self._faceset_post(self.faceset_add_url, outer_id=outer_id, face_tokens=",".join(face_tokens))

    def remove_faces(self, outer_id: str, face_tokens: list[str]) -> None:
        if face_tokens:
            self._faceset_post(self.faceset_remove_url, outer_id=outer_id, face_tokens=",".join(face_tokens))

    @staticmethod
    def require_enrollment_consent(consent: bool, subject_is_account_owner: bool) -> None:
        if not consent:
            raise PermissionError("Explicit biometric enrollment consent is required.")
        if not subject_is_account_owner:
            raise PermissionError("Another person's biometric enrollment requires a separate consent flow.")


class BiometricIdentityStore:
    """Keep protected provider face references separate from ordinary memory."""

    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    @staticmethod
    def _protect(references: list[str]) -> bytes:
        from cryptography.fernet import Fernet

        key = os.getenv("JUPITER_CREDENTIAL_ENCRYPTION_KEY", "").strip()
        if not key:
            raise SpecialistUnavailable("Biometric reference encryption is not configured.")
        return Fernet(key.encode("ascii")).encrypt(
            json.dumps(references, separators=(",", ":")).encode("utf-8")
        )

    def enroll_reference(
        self,
        *,
        user_id: str,
        person_id: str,
        faceset_reference: str,
        provider_face_references: list[str],
        consent_record: dict[str, Any],
        idempotency_key: str,
    ) -> str:
        FacePlusPlusSpecialist.require_enrollment_consent(
            bool(consent_record.get("explicit_consent")),
            bool(consent_record.get("subject_is_account_owner")),
        )
        protected = self._protect(provider_face_references)
        profile_id = str(uuid4())
        with self.store.transaction() as connection:
            self.store.ensure_user(connection, user_id)
            existing = connection.execute(
                """
                SELECT biometric_profile_id FROM biometric_profiles
                WHERE user_id=%s AND person_id=%s AND provider='faceplusplus'
                  AND deleted_at IS NULL
                """,
                (user_id, person_id),
            ).fetchone()
            if existing:
                return str(existing["biometric_profile_id"])
            connection.execute(
                """
                INSERT INTO biometric_profiles (
                    biometric_profile_id, user_id, person_id, provider,
                    faceset_reference, provider_face_refs_protected, consent_record
                ) VALUES (%s,%s,%s,'faceplusplus',%s,%s,%s::jsonb)
                """,
                (
                    profile_id, user_id, person_id, faceset_reference,
                    protected, json.dumps({**consent_record, "idempotency_key": idempotency_key}),
                ),
            )
            connection.execute(
                """
                INSERT INTO biometric_person_links (
                    user_id, biometric_profile_id, person_id, confidence_threshold
                ) VALUES (%s,%s,%s,%s)
                """,
                (user_id, profile_id, person_id, 0.9),
            )
        return profile_id


class CloudVisionSpecialist:
    """Backend-only OCR and detection; never remembered-person identity."""

    @staticmethod
    def _client():
        from config.provider_settings import load_vision_credentials, VisionConfigurationError
        try:
            credentials = load_vision_credentials()
            from google.cloud import vision
            return vision.ImageAnnotatorClient(credentials=credentials), vision
        except VisionConfigurationError as error:
            raise SpecialistUnavailable(str(error), reason_code=error.health) from None
        except ImportError as error:
            raise SpecialistUnavailable("Cloud Vision library is not installed.") from error

    def document_text(self, content: bytes) -> dict[str, Any]:
        client, vision = self._client()
        try:
            response = client.document_text_detection(
                image=vision.Image(content=content), timeout=20.0
            )
            if response.error.message:
                raise SpecialistUnavailable("Cloud Vision document OCR is unavailable.")
            annotation = response.full_text_annotation
            pages = []
            for page_index, page in enumerate(annotation.pages):
                pages.append({
                    "page": page_index,
                    "width": page.width,
                    "height": page.height,
                    "blocks": len(page.blocks),
                })
            return {"text": annotation.text or "", "layout": {"pages": pages}}
        except SpecialistUnavailable:
            raise
        except Exception as error:
            raise SpecialistUnavailable(redact(f"Cloud Vision OCR unavailable: {error}", 240)) from None

    def localize_objects(self, content: bytes) -> list[dict[str, Any]]:
        """Object localization: real normalized boxes + scores (on-demand mark / locate only). Never identity."""
        client, vision = self._client()
        try:
            response = client.object_localization(image=vision.Image(content=content), timeout=10.0)
            if response.error.message:
                raise SpecialistUnavailable("Cloud Vision object localization is unavailable.")
            objects = []
            for annotation in response.localized_object_annotations:
                points = [(float(v.x or 0), float(v.y or 0)) for v in annotation.bounding_poly.normalized_vertices]
                if not points:
                    continue
                xs, ys = [p[0] for p in points], [p[1] for p in points]
                objects.append({"name": str(annotation.name or ""), "score": float(annotation.score or 0),
                                "box": {"x": min(xs), "y": min(ys), "w": max(xs) - min(xs), "h": max(ys) - min(ys)}})
            return objects
        except SpecialistUnavailable:
            raise
        except Exception as error:
            raise SpecialistUnavailable(redact(f"Cloud Vision object localization unavailable: {error}", 240)) from None

    def face_presence(self, content: bytes) -> bool:
        client, vision = self._client()
        try:
            response = client.face_detection(image=vision.Image(content=content), timeout=20.0)
            if response.error.message:
                return False
            return bool(response.face_annotations)
        except Exception:
            return False


@dataclass(frozen=True)
class PDFExtraction:
    file_hash: str
    text: str
    page_count: int
    requires_ocr: bool


class DocumentPipeline:
    """Use native PDF text before spending specialist OCR quota."""

    @staticmethod
    def inspect_pdf(content: bytes) -> PDFExtraction:
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
        except Exception as error:
            raise SpecialistUnavailable(redact(f"PDF extraction unavailable: {error}", 240)) from None
        text = "\n\n".join(page for page in pages if page)
        useful_text = len(text.split()) >= max(10, len(pages) * 3)
        return PDFExtraction(
            file_hash=hashlib.sha256(content).hexdigest(),
            text=text,
            page_count=len(pages),
            requires_ocr=not useful_text,
        )


class OpenWeatherSpecialist:
    """Weather facts are returned only from OpenWeather payloads."""

    current_url = "https://api.openweathermap.org/data/2.5/weather"
    forecast_url = "https://api.openweathermap.org/data/2.5/forecast"

    @staticmethod
    def _api_key() -> str:
        from core.request_context import resolve_request_credential
        key = resolve_request_credential('USER_OPENWEATHER') or os.getenv("OPENWEATHER_API_KEY", "").strip()
        if not key:
            raise SpecialistUnavailable("Weather data is currently unavailable.", reason_code="CONFIG_MISSING")
        return key

    def _request(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        # Configuration failures are not network failures, and never send a
        # request when the key is absent.
        key = self._api_key()
        from core.request_context import current_request_context, resolve_request_credential
        user_key = resolve_request_credential('USER_OPENWEATHER')
        def record_weather_status(provider, capability, health, classification, reason):
            if user_key:
                recorder = current_request_context().credential_status_recorder
                if recorder and health in {'HEALTHY', 'AUTH_INVALID'}:
                    recorder('USER_OPENWEATHER', 'ACTIVE' if health == 'HEALTHY' else 'AUTH_FAILED')
            else:
                record_provider_status(provider, capability, health, classification, reason)
        try:
            with httpx.Client(trust_env=False, timeout=15.0) as client:
                response = client.get(url, params={**params, "appid": self._api_key()})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or str(payload.get("cod", "200")) != "200":
                raise SpecialistUnavailable("Weather response is invalid.", reason_code="INVALID_RESPONSE")
            record_weather_status(
                "openweather", "weather", "HEALTHY", "SPECIALIST_ONLY",
                "A weather request completed successfully.",
            )
        except SpecialistUnavailable:
            raise
        except httpx.TimeoutException:
            record_weather_status(
                "openweather", "weather", "TIMEOUT", "SPECIALIST_ONLY",
                "The weather request timed out.",
            )
            raise SpecialistUnavailable(
                "Weather data is currently unavailable.", reason_code="TIMEOUT"
            ) from None
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            reason = (
                "AUTH_INVALID" if status == 401
                else "PERMISSION_DENIED" if status == 403
                else "RATE_LIMITED" if status == 429
                else "OTHER"
            )
            record_weather_status(
                "openweather",
                "weather",
                reason,
                "CONFIGURATION_MISSING_INVALID" if status == 401 else "SPECIALIST_ONLY",
                "OpenWeather rejected the authenticated request." if status == 401
                else "OpenWeather could not complete the request.",
            )
            raise SpecialistUnavailable(
                "Weather data is currently unavailable.",
                status_code=status,
                reason_code=reason,
            ) from None
        except Exception:
            record_weather_status(
                "openweather", "weather", "NETWORK", "SPECIALIST_ONLY",
                "The weather provider could not be reached.",
            )
            raise SpecialistUnavailable(
                "Weather data is currently unavailable.", reason_code="OTHER"
            ) from None
        if not isinstance(payload, dict):
            raise SpecialistUnavailable("Weather data is currently unavailable.")
        return payload

    def current_weather(
        self,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        city: str | None = None,
        units: str = "metric",
    ) -> dict[str, Any]:
        if city:
            location: dict[str, Any] = {"q": city}
        elif latitude is not None and longitude is not None:
            location = {"lat": latitude, "lon": longitude}
        else:
            raise ValueError("Weather requires a city or latitude/longitude pair.")
        payload = self._request(self.current_url, {**location, "units": units})
        return {
            "source": "OpenWeather",
            "latitude": (payload.get("coord") or {}).get("lat"),
            "longitude": (payload.get("coord") or {}).get("lon"),
            "timezone_offset_seconds": payload.get("timezone"),
            "location_name": payload.get("name"),
            "current": {
                "timestamp": payload.get("dt"),
                "temperature": (payload.get("main") or {}).get("temp"),
                "feels_like": (payload.get("main") or {}).get("feels_like"),
                "humidity": (payload.get("main") or {}).get("humidity"),
                "weather": payload.get("weather") or [],
                "wind": payload.get("wind") or {},
            },
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }

    def forecast(self, latitude: float, longitude: float, units: str = "metric") -> dict[str, Any]:
        payload = self._request(self.forecast_url, {
            "lat": latitude,
            "lon": longitude,
            "units": units,
        })
        city = payload.get("city") if isinstance(payload.get("city"), dict) else {}
        return {
            "source": "OpenWeather",
            "latitude": (city.get("coord") or {}).get("lat", latitude),
            "longitude": (city.get("coord") or {}).get("lon", longitude),
            "timezone_offset_seconds": city.get("timezone"),
            "location_name": city.get("name"),
            "three_hour_forecast": (payload.get("list") or [])[:40],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }


@dataclass(frozen=True)
class VisionAnalysisPlan:
    semantic_analysis_required: bool = True
    face_precheck_required: bool = False
    face_identity_allowed: bool = False
    cloud_ocr_required: bool = False


class VisionSpecialistPolicy:
    """Keep scene understanding, OCR, face presence, and identity separated."""

    @staticmethod
    def plan(*, mime_type: str, asks_about_text: bool, asks_about_people: bool) -> VisionAnalysisPlan:
        is_image = mime_type.startswith("image/")
        return VisionAnalysisPlan(
            semantic_analysis_required=is_image,
            face_precheck_required=is_image and asks_about_people,
            face_identity_allowed=is_image and asks_about_people,
            cloud_ocr_required=is_image and asks_about_text,
        )

    @staticmethod
    def identity_annotation(
        *, face_present: bool, identity_result: FaceIdentityResult | None, position: str
    ) -> str | None:
        if not face_present:
            return None
        if identity_result is None:
            return f"{position} = identity unavailable"
        return identity_result.semantic_annotation(position)


class CloudflareImageSpecialist:
    """Generate only through a registry record already verified as free."""

    @staticmethod
    def _image_mime(raw: bytes) -> str:
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if raw.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "image/webp"
        return ""

    @classmethod
    def _decode_json_artifact(cls, payload: Any) -> tuple[bytes, str]:
        encoded: Any = None
        if isinstance(payload, dict):
            result = payload.get("result")
            if isinstance(result, dict):
                encoded = result.get("image") or result.get("data")
            elif isinstance(result, str):
                encoded = result
            encoded = encoded or payload.get("image") or payload.get("data")
        if not isinstance(encoded, str) or not encoded.strip():
            raise SpecialistUnavailable(
                "Image provider returned no artifact.", reason_code="BAD_RESPONSE"
            )
        encoded = encoded.strip()
        declared_mime = ""
        if encoded.startswith("data:image/") and ";base64," in encoded:
            header, encoded = encoded.split(",", 1)
            declared_mime = header[5:].split(";", 1)[0].casefold()
        try:
            raw = base64.b64decode("".join(encoded.split()), validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise SpecialistUnavailable(
                "Image provider returned an invalid artifact.", reason_code="BAD_RESPONSE"
            ) from None
        detected_mime = cls._image_mime(raw)
        mime_type = detected_mime or declared_mime
        if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise SpecialistUnavailable(
                "Image provider returned an unsupported artifact.", reason_code="BAD_RESPONSE"
            )
        return raw, mime_type

    def generate(self, *, prompt: str, model: ProviderModel, usage: dict | None = None) -> tuple[bytes, str]:
        if model.provider != "cloudflare" or "image_generation" not in model.capabilities:
            raise ValueError("A Cloudflare image-generation model is required.")
        if not (model.free_eligible and model.free_verified and model.free_plan_available):
            raise PermissionError("Image model is not currently verified free.")
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
        if not account_id or not token:
            raise SpecialistUnavailable("Cloudflare image generation is not configured.")
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/"
            f"{model.model_id}"
        )
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"prompt": prompt},
                timeout=90.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            reason_code = (
                "FREE_QUOTA_EXHAUSTED" if status_code == 429
                else "AUTH_FAILED" if status_code in {401, 403}
                else "PROVIDER_5XX" if status_code >= 500
                else "IMAGE_PROVIDER_FAILED"
            )
            raise SpecialistUnavailable(
                f"Image provider returned HTTP {status_code}.",
                status_code=status_code,
                reason_code=reason_code,
            ) from None
        except httpx.TimeoutException:
            raise SpecialistUnavailable(
                "Image provider timed out.", reason_code="TIMEOUT"
            ) from None
        except Exception as error:
            raise SpecialistUnavailable(redact(f"Image generation unavailable: {error}", 240)) from None
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
        if mime_type.startswith("image/"):
            detected_mime = self._image_mime(response.content)
            if detected_mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise SpecialistUnavailable(
                    "Image provider returned an invalid artifact.", reason_code="BAD_RESPONSE"
                )
            return response.content, detected_mime
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            raise SpecialistUnavailable(
                "Image provider returned an invalid response.", reason_code="BAD_RESPONSE"
            ) from None
        if usage is not None:
            from services.quota_governor import QuotaGovernor
            usage["neurons"] = QuotaGovernor.actual_shared_units(payload)
        return self._decode_json_artifact(payload)

    def edit(
        self,
        *,
        prompt: str,
        image: bytes,
        model: ProviderModel,
        mask: bytes | None = None,
        strength: float = 0.72,
        usage: dict | None = None,
    ) -> tuple[bytes, str]:
        """Run SDXL image-to-image/masked editing through Workers AI."""

        if model.provider != "cloudflare" or model.model_id != "@cf/stabilityai/stable-diffusion-xl-base-1.0":
            raise ValueError("Cloudflare SDXL Base is required for image editing.")
        if not (model.free_eligible and model.free_verified and model.free_plan_available):
            raise PermissionError("Cloudflare SDXL is not currently verified free.")
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
        if not account_id or not token:
            raise SpecialistUnavailable("Cloudflare image editing is not configured.", reason_code="AUTH_FAILED")
        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model.model_id}"
        payload: dict[str, Any] = {
            "prompt": str(prompt)[:1800],
            "image": list(image),
            "strength": max(0.05, min(1.0, float(strength))),
        }
        if mask:
            payload["mask"] = list(mask)
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=120.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            reason = "FREE_QUOTA_EXHAUSTED" if status == 429 else "AUTH_FAILED" if status in {401, 403} else "UNSUPPORTED" if status in {400, 404} else "IMAGE_ROUTES_FAILED"
            raise SpecialistUnavailable("Cloudflare SDXL could not edit this image.", status_code=status, reason_code=reason) from None
        except httpx.TimeoutException:
            raise SpecialistUnavailable("Cloudflare SDXL timed out.", reason_code="TIMEOUT") from None
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
        if mime_type.startswith("image/"):
            detected = self._image_mime(response.content)
            if detected:
                return response.content, detected
        try:
            result = response.json()
            if usage is not None:
                from services.quota_governor import QuotaGovernor
                usage["neurons"] = QuotaGovernor.actual_shared_units(result)
            return self._decode_json_artifact(result)
        except (ValueError, json.JSONDecodeError):
            raise SpecialistUnavailable("Cloudflare SDXL returned no edited image.", reason_code="BAD_RESPONSE") from None
