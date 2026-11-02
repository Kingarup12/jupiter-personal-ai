"""Semantic embedding generation via the pluggable backend."""

from config.settings import EMBEDDING_MODEL


class EmbeddingGenerator:
    """Generates semantic embeddings using the configured LLM backend."""

    def __init__(self):
        self.model = EMBEDDING_MODEL

    def _eligible_models(self) -> list[str]:
        # This index has one configured vector-space contract. Equal dimension
        # does NOT make two models' vectors interchangeable. A catalog refresh
        # must not change the embedding model for either writes or queries;
        # changing this setting requires an explicit full-index migration.
        return [self.model]

    def generate(self, text: str) -> list[float]:
        if not text.strip():
            return []

        from core.backends import get_backend

        backend = get_backend()
        last_error = None
        for model in self._eligible_models():
            quota_model, governor = self._quota_route(model)
            admission = governor.reserve(quota_model, max(1, len(text) // 4))
            if not admission.allowed:
                continue
            try:
                embeddings = backend.embed(model=model, texts=text)
                governor.success(quota_model, input_tokens=max(1, len(text) // 4), admission=admission)
                return embeddings[0]
            except Exception as error:
                governor.failure(quota_model, self._quota_failure(error))
                last_error = error
        if last_error:
            raise last_error
        return []

    def generate_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        from core.backends import get_backend

        backend = get_backend()
        last_error = None
        for model in self._eligible_models():
            estimated = max(1, sum(len(str(text)) for text in texts) // 4)
            quota_model, governor = self._quota_route(model)
            admission = governor.reserve(quota_model, estimated)
            if not admission.allowed:
                continue
            try:
                result = backend.embed(model=model, texts=texts)
                governor.success(quota_model, input_tokens=estimated, admission=admission)
                return result
            except Exception as error:
                governor.failure(quota_model, self._quota_failure(error))
                last_error = error
        if last_error:
            raise last_error
        return []

    @staticmethod
    def _quota_failure(error: Exception) -> str:
        detail = str(error).casefold()
        if "429" in detail or "rate limit" in detail:
            return "RATE_LIMIT"
        if "quota" in detail or "resource_exhausted" in detail:
            return "QUOTA_EXHAUSTED"
        return type(error).__name__.upper()[:80]

    @staticmethod
    def _quota_route(model_id: str):
        from core.provider_contracts import CredentialScope, ProviderModel
        from core.request_context import current_request_context, resolve_request_credential
        from services.quota_governor import quota_governor

        context = current_request_context()
        user_key = resolve_request_credential(CredentialScope.USER_GOOGLE.value)
        scope = (
            CredentialScope.USER_GOOGLE
            if user_key and not context.managed_google_credentials
            else CredentialScope.JUPITER_SHARED_GOOGLE
        )
        return ProviderModel(
            provider="google",
            model_id=model_id,
            display_name=model_id,
            credential_scope=scope,
            credential_owner_id=context.user_id or "*" if scope == CredentialScope.USER_GOOGLE else "*",
        ), quota_governor()
