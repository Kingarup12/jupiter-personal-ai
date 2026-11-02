"""Genuine streaming dictation preview (marker 159 follow-up): Gemini Live transcription, backend-only.

The composer's provisional words come from a real streaming recogniser: ``gemini-3.5-transcribe-live`` over the Gemini
Live API returns interim hypotheses while the user speaks and a finalized segment after each pause (official guide
https://ai.google.dev/gemini-api/docs/live-api/live-transcribe; free of charge on the free tier per
https://ai.google.dev/gemini-api/docs/pricing, both checked 2026-10-08).

The documented raw Live WebSocket protocol is used directly (setup -> setupComplete, realtimeInput audio,
audioStreamEnd; serverContent.interimInputTranscription / inputTranscription). The runtime's pinned google-genai
(1.75.0) predates the interim field, and upgrading the shared environment would change production on its next
restart, so this adapter does not depend on the SDK version. The key travels in the ``x-goog-api-key`` header,
never in a URL.

Scope, deliberately narrow:
* PREVIEW ONLY. The take is still committed by the user's ✓ through the existing accuracy-first ``/api/voice/transcribe``
  pass; capture, VAD, endpointing, mute and microphone ownership are unchanged. If this stream is unavailable the
  client keeps today's periodic draft preview.
* The Google credential never leaves the backend. The browser connects with a one-use, short-lived ticket that the
  authenticated user obtained; the socket carries raw 16 kHz mono PCM16 and receives only ``interim``/``final`` text.
* Credentials follow the Live order: the user's own Google key, then the managed keys in policy order. Only a
  failure BEFORE the stream is ready moves to the next credential (never mid-take, never to evade a quota).
* Nothing is persisted or logged except bounded, content-free diagnostics (no audio, no transcript text).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable

from core.logger import Logger

STREAMING_STT_MODEL = "gemini-3.5-transcribe-live"
LIVE_ENDPOINT = ("wss://generativelanguage.googleapis.com/ws/"
                 "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
INPUT_MIME = "audio/pcm;rate=16000"
SETUP_TIMEOUT_S = 10.0
MAX_SESSION_S = 9 * 60 + 30             # provider cap is 10 minutes per Live transcription session
MAX_CHUNK_BYTES = 64 * 1024             # ~2 s of 16 kHz PCM16 per message; real chunks are ~100 ms (3.2 KB)
MAX_TEXT = 4000
MAX_VOCABULARY = 24


class StreamingTranscriptionUnavailable(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def streaming_route_allowed() -> tuple[bool, str]:
    """The free-routing policy gate for this adapter (recurring-free provider, model not excluded, PRIVATE data)."""
    from config.provider_catalog import routing_gate
    return routing_gate("google", STREAMING_STT_MODEL, "PRIVATE")


def _vocabulary(terms: Any) -> list[str]:
    """User-owned spelling hints (names the user typed), bounded; the provider's official vocabulary biasing."""
    clean: list[str] = []
    for term in list(terms or [])[:48]:
        value = " ".join(str(term or "").split())[:40]
        if value and value.casefold() not in {item.casefold() for item in clean}:
            clean.append(value)
        if len(clean) >= MAX_VOCABULARY:
            break
    return clean


def setup_message(model_id: str, vocabulary: list[str]) -> dict[str, Any]:
    transcription: dict[str, Any] = {"languageCodes": []}      # automatic language detection (code-switching)
    if vocabulary:
        transcription["customVocabulary"] = vocabulary
    return {"setup": {"model": f"models/{model_id}", "generationConfig": {"responseModalities": ["TEXT"]},
                      "inputAudioTranscription": transcription}}


def transcription_events(message: dict[str, Any]) -> list[tuple[str, str]]:
    """(kind, text) pairs from one provider message: interim hypotheses and finalized segments."""
    content = message.get("serverContent") or {}
    events = []
    interim = (content.get("interimInputTranscription") or {}).get("text")
    if interim:
        events.append(("interim", str(interim)[:MAX_TEXT]))
    final = (content.get("inputTranscription") or {}).get("text")
    if final:
        events.append(("final", str(final)[:MAX_TEXT]))
    return events


def _category(error: BaseException) -> str:
    received = getattr(error, "rcvd", None)
    text = f"{getattr(received, 'code', '') or ''} {getattr(received, 'reason', '') or ''} {error}".casefold()
    if "429" in text or "resource_exhausted" in text or "quota" in text or "rate limit" in text:
        return "quota"
    if any(token in text for token in ("401", "403", "permission", "api key", "api_key", "unauthenticated")):
        return "auth"
    if any(token in text for token in ("404", "not found", "not supported", "unsupported", "invalid model")):
        return "model"
    if isinstance(error, asyncio.TimeoutError):
        return "setup_timeout"
    return type(error).__name__[:40]


def _decode(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _diagnostic(**fields: Any) -> None:
    Logger.info("streaming_stt " + " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, "")))


async def _default_connect(api_key: str):
    from websockets.asyncio.client import connect
    return await connect(LIVE_ENDPOINT, additional_headers={"x-goog-api-key": api_key}, max_size=2 ** 22,
                         open_timeout=SETUP_TIMEOUT_S)


class StreamingTranscriptionBridge:
    """One browser dictation take <-> one Gemini Live transcription session."""

    def __init__(self, *, model_id: str = STREAMING_STT_MODEL, clock=time.monotonic,
                 connect: Callable[[str], Awaitable[Any]] | None = None):
        self.model_id = model_id
        self._clock = clock
        self._connect = connect or _default_connect

    @staticmethod
    def credential_attempts(model_id: str) -> list[tuple[Any, str, str, str]]:
        from services.gemini_live_transport import GeminiLiveTransport
        return GeminiLiveTransport._credential_attempts(model_id)

    async def _open(self, vocabulary: list[str]):
        """Connect with the first credential whose session completes setup. Returns (provider_socket, label)."""
        attempts = self.credential_attempts(self.model_id)
        if not attempts:
            raise StreamingTranscriptionUnavailable("no_credential")
        last = "unavailable"
        for _scope, api_key, credential_id, _quota_group in attempts:
            label = "user-google" if credential_id == "user-google" else credential_id
            provider = None
            try:
                provider = await asyncio.wait_for(self._connect(api_key), SETUP_TIMEOUT_S)
                await provider.send(json.dumps(setup_message(self.model_id, vocabulary)))
                deadline = self._clock() + SETUP_TIMEOUT_S
                while True:
                    reply = _decode(await asyncio.wait_for(provider.recv(), max(0.1, deadline - self._clock())))
                    if "setupComplete" in reply:
                        return provider, label
                    if reply.get("error"):
                        raise RuntimeError(str(reply.get("error"))[:200])
            except Exception as error:                              # auth / quota / model: next credential, pre-ready
                last = _category(error)
                if provider is not None:
                    with suppress(Exception):
                        await provider.close()
            _diagnostic(event="credential_skipped", credential=label, category=last)
        raise StreamingTranscriptionUnavailable(last)

    async def bridge(self, websocket, *, user_id: str, vocabulary: Any = ()) -> dict[str, Any]:
        """Relay one take. Client -> server: binary PCM16 chunks, then {"type": "end"}. Server -> client:
        {"type": "ready"}, {"type": "interim", "text"}, {"type": "final", "text"}, {"type": "closed"} or
        {"type": "unavailable", "reason"}. Returns content-free stats."""
        started = self._clock()
        stats: dict[str, Any] = {"chunks": 0, "bytes": 0, "interims": 0, "finals": 0, "first_interim_ms": None,
                                 "credential": "", "setup_ms": None}
        allowed, reason = streaming_route_allowed()
        if not allowed:
            await websocket.send_json({"type": "unavailable", "reason": "not_free_eligible"})
            _diagnostic(event="refused", reason=reason)
            return stats
        try:
            provider, stats["credential"] = await self._open(_vocabulary(vocabulary))
        except StreamingTranscriptionUnavailable as error:
            await websocket.send_json({"type": "unavailable", "reason": error.category})
            _diagnostic(event="unavailable", reason=error.category)
            return stats
        stats["setup_ms"] = round((self._clock() - started) * 1000)
        await websocket.send_json({"type": "ready", "model": self.model_id})
        first_audio_at: list[float] = []

        async def upstream() -> None:
            while True:
                remaining = MAX_SESSION_S - (self._clock() - started)
                if remaining <= 0:
                    break
                message = await asyncio.wait_for(websocket.receive(), remaining)
                if message.get("type") == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data:
                    if len(data) > MAX_CHUNK_BYTES or len(data) % 2:
                        continue                                      # malformed chunk: dropped, never forwarded
                    if not first_audio_at:
                        first_audio_at.append(self._clock())
                    stats["chunks"] += 1
                    stats["bytes"] += len(data)
                    await provider.send(json.dumps({"realtimeInput": {"audio": {
                        "data": base64.b64encode(data).decode("ascii"), "mimeType": INPUT_MIME}}}))
                elif '"end"' in str(message.get("text") or ""):
                    break
            with suppress(Exception):
                await provider.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

        async def downstream() -> None:
            async for raw in provider:
                for kind, text in transcription_events(_decode(raw)):
                    if kind == "interim":
                        stats["interims"] += 1
                        if stats["first_interim_ms"] is None and first_audio_at:
                            stats["first_interim_ms"] = round((self._clock() - first_audio_at[0]) * 1000)
                    else:
                        stats["finals"] += 1
                    await websocket.send_json({"type": kind, "text": text})

        sender = asyncio.create_task(upstream())
        receiver = asyncio.create_task(downstream())
        try:
            done, _ = await asyncio.wait({sender, receiver}, timeout=MAX_SESSION_S, return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                await asyncio.wait({receiver}, timeout=2.5)            # let the last segment finalize
            elif receiver in done and not receiver.cancelled() and receiver.exception() is not None:
                stats["error"] = _category(receiver.exception())
                with suppress(Exception):
                    await websocket.send_json({"type": "unavailable", "reason": "stream_error"})
        finally:
            for task in (sender, receiver):
                task.cancel()
                with suppress(BaseException):
                    await task
            with suppress(Exception):
                await provider.close()
        with suppress(Exception):
            await websocket.send_json({"type": "closed"})
        _diagnostic(event="closed", credential=stats["credential"], setup_ms=stats["setup_ms"], chunks=stats["chunks"],
                    interims=stats["interims"], finals=stats["finals"], first_interim_ms=stats["first_interim_ms"],
                    error=stats.get("error"), seconds=round(self._clock() - started, 1))
        return stats
