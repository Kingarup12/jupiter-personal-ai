"""Pluggable LLM backend abstraction for Jupiter.

Switch between local (Ollama, llama.cpp) and cloud (OpenAI, Gemini) providers
by setting JUPITER_LLM_BACKEND in your environment or config/settings.py.
The rest of Jupiter never imports a provider directly.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

from config.settings import TEMPERATURE
from core.logger import Logger


class ChatResult(str):
    """Assistant text with optional, provider-reported model token counts."""

    def __new__(cls, text, token_usage=None, model="", provider=""):
        value = super().__new__(cls, str(text or "").strip())
        value.token_usage = token_usage
        value.model = model
        value.provider = provider
        return value


@dataclass(frozen=True)
class ChatStreamChunk:
    """One native provider delta plus any metadata available at that point."""

    text: str = ""
    event_type: str = "delta"
    model: str = ""
    token_usage: dict | None = None


def _usage_number(source, *names):
    for name in names:
        value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
        try:
            if value is not None and int(value) >= 0:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def model_token_usage(source):
    """Normalize real provider usage metadata; return None when unavailable."""
    if source is None:
        return None
    prompt = _usage_number(source, "prompt_tokens", "prompt_token_count", "prompt_eval_count")
    output = _usage_number(source, "completion_tokens", "candidates_token_count", "eval_count")
    total = _usage_number(source, "total_tokens", "total_token_count")
    if total is None and (prompt is not None or output is not None):
        total = (prompt or 0) + (output or 0)
    if total is None:
        return None
    return {"input": prompt, "output": output, "total": total}


def _provider_status_code(error) -> int | None:
    """Extract an HTTP-like status without exposing provider response bodies."""

    for candidate in (
        getattr(error, "status_code", None),
        getattr(error, "code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        if isinstance(candidate, int):
            return candidate
    detail = str(error or "")
    return next((code for code in (400, 401, 403, 404, 410, 429, 500, 502, 503, 504) if str(code) in detail), None)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class LLMBackend(ABC):
    """Every backend must implement chat and embed."""

    @abstractmethod
    def chat(
        self,
        model: str | list[str],
        messages: list[dict],
        temperature: float = TEMPERATURE,
    ) -> str:
        """Send a chat-completion request and return the assistant text."""

    def chat_stream(
        self,
        model: str | list[str],
        messages: list[dict],
        temperature: float = TEMPERATURE,
    ):
        """Yield native provider deltas without accumulating the full reply."""
        raise NotImplementedError(f"{type(self).__name__} does not support streaming")

    @abstractmethod
    def embed(self, model: str, texts: str | list[str]) -> list[list[float]]:
        """Return embedding vectors for one or more texts."""


# ---------------------------------------------------------------------------
# Ollama (default local backend)
# ---------------------------------------------------------------------------

class OllamaBackend(LLMBackend):
    """Talks to a locally running Ollama server."""

    def chat(self, model, messages, temperature=TEMPERATURE):
        from ollama import chat as ollama_chat
        
        # If passed a list, just take the first one (fallback not fully implemented for local yet)
        if isinstance(model, list):
            model = model[0]


        response = ollama_chat(
            model=model,
            messages=messages,
            options={"temperature": temperature},
        )
        return ChatResult(response["message"]["content"], model_token_usage(response))

    def chat_stream(self, model, messages, temperature=TEMPERATURE):
        from ollama import chat as ollama_chat

        if isinstance(model, list):
            model = model[0]
        stream = ollama_chat(
            model=model,
            messages=messages,
            options={"temperature": temperature},
            stream=True,
        )
        for chunk in stream:
            text = (chunk.get("message") or {}).get("content", "")
            usage = model_token_usage(chunk) if chunk.get("done") else None
            if text or usage:
                yield ChatStreamChunk(text=text, model=model, token_usage=usage)

    def embed(self, model, texts):
        from ollama import embed as ollama_embed

        if isinstance(texts, str):
            texts = [texts]
        response = ollama_embed(model=model, input=texts)
        return response["embeddings"]


# ---------------------------------------------------------------------------
# OpenAI-compatible (works with OpenAI, Together, Groq, local vLLM, etc.)
# ---------------------------------------------------------------------------

class OpenAIBackend(LLMBackend):
    """Any provider that speaks the OpenAI chat-completions API."""

    def __init__(self):
        self._api_key = os.getenv("OPENAI_API_KEY", "")
        self._base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    def chat(self, model, messages, temperature=TEMPERATURE):
        from openai import OpenAI
        
        if isinstance(model, list):
            model = model[0]

        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
        )
        return ChatResult(response.choices[0].message.content, model_token_usage(getattr(response, "usage", None)))

    def chat_stream(self, model, messages, temperature=TEMPERATURE):
        from openai import OpenAI

        if isinstance(model, list):
            model = model[0]
        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            stream=True,
        )
        for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            text = getattr(choices[0].delta, "content", "") if choices else ""
            usage = model_token_usage(getattr(chunk, "usage", None))
            if text or usage:
                yield ChatStreamChunk(text=text or "", model=model, token_usage=usage)

    def embed(self, model, texts):
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        if isinstance(texts, str):
            texts = [texts]
        response = client.embeddings.create(model=model, input=texts)
        return [item.embedding for item in response.data]


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------

class GeminiBackend(LLMBackend):
    """Google Generative AI (Gemini) via the official SDK."""

    def __init__(self):
        # Secrets are resolved per model through the central credential
        # policy. Keeping a process-global generic shared key here would let
        # legacy fallback bypass per-credential restrictions.
        self._shared_api_key = None

    def _request_api_keys(self, model: str, capability: str = "text"):
        from config.credential_policy import managed_google_policies
        from core.request_context import current_request_context, resolve_request_credential, request_google_quota_group

        attempts = []
        context = current_request_context()
        # Entitlement admits the special managed fallback, never replacing the
        # requesting account's own saved key (including Owner/Admin/Testing).
        uses_jupiter_user_google = bool(context.managed_google_credentials)
        user_key = resolve_request_credential("USER_GOOGLE")
        if user_key:
            attempts.append((user_key, request_google_quota_group()))
        seen = {user_key} if user_key else set()
        for policy in managed_google_policies(model, capability, include_jupiter_user_google=uses_jupiter_user_google):
            secret = policy.secret()
            if secret and secret not in seen:
                seen.add(secret)
                attempts.append((secret, policy.quota_group_key()))
        if not attempts:
            raise RuntimeError("No eligible Google credential is configured for this model.")
        return attempts

    def chat(self, model, messages, temperature=TEMPERATURE):
        from google import genai
        from google.genai import types
        import time
        from core.model_manager import ModelManager

        manager = ModelManager()
        
        system_instruction = None
        contents = []
        for msg in messages:
            if msg["role"] == "system":
                if system_instruction is None:
                    system_instruction = msg["content"]
                else:
                    system_instruction += "\n\n" + msg["content"]
            elif msg["role"] == "user":
                content = msg["content"]
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if part.get("type") == "text":
                            parts.append(types.Part.from_text(text=str(part.get("text") or "")))
                        elif part.get("type") == "binary":
                            parts.append(types.Part.from_bytes(
                                data=part["data"],
                                mime_type=part["mime_type"],
                            ))
                    contents.append({"role": "user", "parts": parts})
                else:
                    contents.append({"role": "user", "parts": [{"text": content}]})
            elif msg["role"] == "assistant":
                contents.append({"role": "model", "parts": [{"text": msg["content"]}]})

        config = {"temperature": temperature}
        if system_instruction:
            config["system_instruction"] = system_instruction
            
        models_to_try = []
        if isinstance(model, list):
            models_to_try.extend(model)
        else:
            models_to_try.append(model)
            
        last_exception = None
        for m in models_to_try:
            exhausted_groups = set()
            for api_key, quota_group in self._request_api_keys(m):
                if quota_group in exhausted_groups:
                    continue
                start_time = time.time()
                client = genai.Client(api_key=api_key)
                try:
                    response = client.models.generate_content(
                        model=m,
                        contents=contents,
                        config=config,
                    )
                    latency = time.time() - start_time
                    manager.report_success(m, latency)
                    actual_model = getattr(response, "model_version", None) or m
                    return ChatResult(
                        response.text,
                        model_token_usage(getattr(response, "usage_metadata", None)),
                        model=actual_model,
                        provider="google",
                    )
                except Exception as e:
                    detail = str(e).casefold()
                    is_429 = "429" in detail or "resource_exhausted" in detail
                    manager.report_failure(m, is_429)
                    last_exception = e
                    if is_429:
                        exhausted_groups.add(quota_group)
                        continue
                    if any(marker in detail for marker in ("401", "unauthenticated", "invalid api key")):
                        continue
                    # Broken request/model/modality is not repaired by keys.
                    break
                finally:
                    client.close()
        raise last_exception

    def chat_stream(self, model, messages, temperature=TEMPERATURE):
        from google import genai
        from google.genai import types
        import time
        from core.model_manager import ModelManager

        manager = ModelManager()
        system_instruction = None
        contents = []
        for msg in messages:
            if msg["role"] == "system":
                system_instruction = (
                    msg["content"] if system_instruction is None
                    else system_instruction + "\n\n" + msg["content"]
                )
            elif msg["role"] == "user":
                content = msg["content"]
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if part.get("type") == "text":
                            parts.append(types.Part.from_text(text=str(part.get("text") or "")))
                        elif part.get("type") == "binary":
                            parts.append(types.Part.from_bytes(
                                data=part["data"],
                                mime_type=part["mime_type"],
                            ))
                    contents.append({"role": "user", "parts": parts})
                else:
                    contents.append({"role": "user", "parts": [{"text": content}]})
            elif msg["role"] == "assistant":
                contents.append({"role": "model", "parts": [{"text": msg["content"]}]})

        config = {"temperature": temperature}
        if system_instruction:
            config["system_instruction"] = system_instruction
        models_to_try = list(model) if isinstance(model, list) else [model]
        last_exception = None
        partial_output = ""
        for model_index, selected_model in enumerate(models_to_try):
            exhausted_groups = set()
            for api_key, quota_group in self._request_api_keys(selected_model):
                if quota_group in exhausted_groups:
                    continue
                start_time = time.time()
                emitted_text = False
                client = genai.Client(api_key=api_key)
                try:
                    selected_system_instruction = system_instruction
                    if partial_output:
                        continuation = (
                            "A previous verified-free model produced the partial assistant reply below "
                            "before its stream was interrupted. Continue from its exact end without "
                            "repeating any completed text:\n\n" + partial_output[-4000:]
                        )
                        selected_system_instruction = (
                            selected_system_instruction + "\n\n" + continuation
                            if selected_system_instruction else continuation
                        )
                    selected_config = {"temperature": temperature}
                    if selected_system_instruction:
                        selected_config["system_instruction"] = selected_system_instruction
                    stream = client.models.generate_content_stream(
                        model=selected_model,
                        contents=contents,
                        config=selected_config,
                    )
                    final_usage = None
                    for chunk in stream:
                        text = getattr(chunk, "text", "") or ""
                        usage = model_token_usage(getattr(chunk, "usage_metadata", None))
                        if usage:
                            final_usage = usage
                        if text:
                            emitted_text = True
                            partial_output += text
                            yield ChatStreamChunk(text=text, model=selected_model)
                    manager.report_success(selected_model, time.time() - start_time)
                    if final_usage:
                        yield ChatStreamChunk(model=selected_model, token_usage=final_usage)
                    return
                except Exception as error:
                    detail = str(error).casefold()
                    is_429 = "429" in detail or "resource_exhausted" in detail
                    manager.report_failure(selected_model, is_429)
                    last_exception = error
                    status_code = _provider_status_code(error)
                    has_fallback = model_index + 1 < len(models_to_try)
                    fallback_action = "continue_next_free_model" if has_fallback else (
                        "preserve_partial_response" if partial_output else "fail_all_models"
                    )
                    Logger.warning(
                        f"Normal chat provider stream stage=provider_stream provider=google "
                        f"model={selected_model} exception={type(error).__name__} "
                        f"status={status_code or 'none'} emitted={emitted_text} "
                        f"fallback_action={fallback_action}"
                    )
                    if partial_output:
                        return
                    if is_429:
                        exhausted_groups.add(quota_group)
                        continue
                    if any(marker in detail for marker in ("401", "unauthenticated", "invalid api key")):
                        continue
                    break
                finally:
                    client.close()
        raise last_exception

    def embed(self, model, texts):
        from google import genai

        if isinstance(texts, str):
            texts = [texts]
        last_exception = None
        for api_key, _ in self._request_api_keys(model, "embeddings"):
            client = genai.Client(api_key=api_key)
            try:
                result = client.models.embed_content(model=model, contents=texts)
                return [e.values for e in result.embeddings]
            except Exception as error:
                last_exception = error
            finally:
                client.close()
        raise last_exception or RuntimeError("No eligible Google embedding credential is available.")


# ---------------------------------------------------------------------------
# llama.cpp (via llama-cpp-python HTTP server)
# ---------------------------------------------------------------------------

class LlamaCppBackend(LLMBackend):
    """llama.cpp server running at a local HTTP endpoint (OpenAI-compatible)."""

    def __init__(self):
        self._base_url = os.getenv("LLAMACPP_BASE_URL", "http://localhost:8080/v1")

    def chat(self, model, messages, temperature=TEMPERATURE):
        from openai import OpenAI
        
        if isinstance(model, list):
            model = model[0]

        client = OpenAI(api_key="not-needed", base_url=self._base_url)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
        )
        return ChatResult(response.choices[0].message.content, model_token_usage(getattr(response, "usage", None)))

    def chat_stream(self, model, messages, temperature=TEMPERATURE):
        from openai import OpenAI

        if isinstance(model, list):
            model = model[0]
        client = OpenAI(api_key="not-needed", base_url=self._base_url)
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            stream=True,
        )
        for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            text = getattr(choices[0].delta, "content", "") if choices else ""
            usage = model_token_usage(getattr(chunk, "usage", None))
            if text or usage:
                yield ChatStreamChunk(text=text or "", model=model, token_usage=usage)

    def embed(self, model, texts):
        from openai import OpenAI

        client = OpenAI(api_key="not-needed", base_url=self._base_url)
        if isinstance(texts, str):
            texts = [texts]
        response = client.embeddings.create(model=model, input=texts)
        return [item.embedding for item in response.data]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type[LLMBackend]] = {
    "ollama": OllamaBackend,
    "openai": OpenAIBackend,
    "gemini": GeminiBackend,
    "llama.cpp": LlamaCppBackend,
    "llamacpp": LlamaCppBackend,
}

_instance: LLMBackend | None = None


def get_backend() -> LLMBackend:
    """Return the singleton LLM backend based on config.settings.LLM_BACKEND."""
    global _instance
    if _instance is not None:
        return _instance

    from config.settings import LLM_BACKEND
    name = LLM_BACKEND.lower().strip()
    cls = _BACKENDS.get(name)
    if cls is None:
        Logger.warning(
            f"Unknown LLM backend '{name}', falling back to Ollama."
        )
        cls = OllamaBackend
    _instance = cls()
    Logger.info(f"LLM backend: {cls.__name__}")
    return _instance
