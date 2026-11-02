"""Backend-only Gemini Live audio bridge for authenticated Jupiter sessions."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import hashlib
import secrets
import threading
import time
import re
from datetime import datetime, timezone
from contextlib import AsyncExitStack, suppress
from typing import Any

from core.provider_contracts import CredentialScope, ProviderModel
from core.request_context import current_request_context, resolve_request_credential, request_google_quota_group
from services.live_voice_router import LiveVoiceRouter
from services.quota_governor import quota_governor
from services.voice_pipeline import (VerifiedFreeTTSService, LIVE_CURRENT_TURN_GUIDANCE, LIVE_SPOKEN_CONVERSATION_GUIDANCE,
                                     LIVE_SESSION_CONTEXT_MAX_CHARS,
                                    live_verbatim_grounding)
from config.voice_settings import DEFAULT_JUPITER_VOICE, normalize_live_voice
from collections import OrderedDict

# Constructing google.genai.Client costs 0.8-1.5 s on the WT host (HTTP/SSL setup), measured 29-Sep-2026 — paid on
# every Live start AND every fallback reconnect. Live opens its own websocket per session, so one client per credential
# is reused (bounded LRU). Keyed by the Client class too, so a patched test client never leaks across tests.
_LIVE_CLIENT_LIMIT = 8
_LIVE_CLIENTS: "OrderedDict[tuple[int, str], Any]" = OrderedDict()
_LIVE_CLIENTS_LOCK = threading.Lock()


def _live_client(api_key: str):
    from google import genai

    key = (id(genai.Client), hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest())
    with _LIVE_CLIENTS_LOCK:
        cached = _LIVE_CLIENTS.get(key)
        if cached is not None:
            _LIVE_CLIENTS.move_to_end(key)
            return cached
    client = genai.Client(api_key=api_key)
    with _LIVE_CLIENTS_LOCK:
        cached = _LIVE_CLIENTS.get(key)
        if cached is not None:
            with suppress(Exception):
                client.close()
            return cached
        _LIVE_CLIENTS[key] = client
        while len(_LIVE_CLIENTS) > _LIVE_CLIENT_LIMIT:
            _, evicted = _LIVE_CLIENTS.popitem(last=False)
            with suppress(Exception):
                evicted.close()
    return client


_PREWARM_LOCK = threading.Lock()
_PREWARM_STARTED = False


def prewarm_live_client_library() -> bool:
    """Marker 183: measured startup (server live_turn_events, 9–14 Oct): on the FIRST Live connection after a backend
    start, "provider_candidate_selected -> provider_setup_sent" took 2.2–9.6 s (production 9.4 s and 9.6 s), while every
    later connection took 3–16 ms. That gap is only ``_live_client``: the one-time ``from google import genai`` import
    (1.8–2.7 s warm-cache, measured locally; far more on a cold disk) plus ``genai.Client`` construction (~0.6 s).
    The provider handshake itself (setup_sent -> accepted) is a normal 1.3–2.4 s and is not changed.

    This warms the library once, in a daemon thread, with a throwaway client built from a dummy key: no network
    request, no credential, no provider call. Real clients are still created per credential at connect time."""
    global _PREWARM_STARTED
    with _PREWARM_LOCK:
        if _PREWARM_STARTED:
            return False
        _PREWARM_STARTED = True

    def warm() -> None:
        from core.logger import Logger
        started = time.perf_counter()
        try:
            from google import genai
            from google.genai import types  # noqa: F401  (used by _config on every connect)
            api_key = os.getenv("GEMINI_API_KEY", "").strip()
            if not api_key:
                return
            client = genai.Client(api_key=api_key)
            with suppress(Exception):
                client.close()
            Logger.info(f"live_client_prewarm ms={int((time.perf_counter() - started) * 1000)}")
        except Exception as error:                                   # warming is optional; connect still works
            Logger.warning(f"live_client_prewarm failed error_class={type(error).__name__}")

    threading.Thread(target=warm, name="jupiter-live-client-prewarm", daemon=True).start()
    return True


class NativeLiveUnavailable(RuntimeError):
    def __init__(self, message: str, category: str = "live_model_unavailable"):
        super().__init__(message)
        self.category = category


class LiveMessageDiagnostics:
    """Short-lived, opaque turn receipts; durable model truth stays in messages.

    Clients supply only their stable chat/message IDs. Neither a model nor a
    credential can be selected through this channel. Receipts survive socket
    close long enough for the final local history commit, but are never history
    metadata and are scoped to the authenticated user and connected session.
    """
    _records: dict[str, dict[str, Any]] = {}
    _lock = threading.RLock()

    @staticmethod
    def target_available(user_id: str, chat_id: str, message_id: str) -> bool:
        from storage.sqlite_store import SQLiteStore
        store = SQLiteStore(user_id=user_id)
        try:
            return not store.is_chat_deleted(chat_id) and not store.connection.execute(
                "SELECT 1 FROM messages WHERE user_id=? AND chat_id=? AND message_id=?",
                (store.user_id, chat_id, message_id),
            ).fetchone()
        finally:
            store.close()

    @classmethod
    def issue(cls, user_id: str, session_id: str, turn_id: int, model_id: str,
              *, chat_id: str = "", message_id: str = "") -> str:
        receipt = secrets.token_urlsafe(32)
        now = time.time()
        with cls._lock:
            cls._records = {key: value for key, value in cls._records.items() if value["expires"] > now}
            if any(value["user"] == user_id and value["target"] == (chat_id, message_id)
                   for value in cls._records.values()):
                return ""  # No second session/turn may claim this sibling.
            if len(cls._records) >= 4096:
                cls._records.pop(next(iter(cls._records)))
            cls._records[hashlib.sha256(receipt.encode()).hexdigest()] = {
                "user": user_id, "session": session_id, "turn": turn_id,
                "model": model_id, "expires": now + 1800, "binding": None,
                "target": (chat_id, message_id),
            }
        return receipt

    @classmethod
    def bind(cls, user_id: str, receipt: str, chat_id: str, message_id: str) -> bool:
        if not all(re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value or "") for value in (chat_id, message_id)):
            return False
        key = hashlib.sha256(str(receipt).encode()).hexdigest()
        with cls._lock:
            cls._records = {key: value for key, value in cls._records.items() if value["expires"] > time.time()}
            record = cls._records.get(key)
            if not record or record["user"] != user_id or record["expires"] <= time.time():
                return False
            binding = (chat_id, message_id)
            if record["target"] != binding:
                return False
            if record["binding"] is not None and record["binding"] != binding:
                return False
            from storage.sqlite_store import SQLiteStore
            store = SQLiteStore(user_id=user_id)
            try:
                store.connection.execute("BEGIN IMMEDIATE")
                if store.is_chat_deleted(chat_id):
                    return False
                if not store.connection.execute(
                    "SELECT 1 FROM chats WHERE user_id=? AND chat_id=?", (store.user_id, chat_id),
                ).fetchone():
                    return False
                # Wait for canonical history sync. Never resurrect deleted
                # chats or manufacture empty assistant rows for a receipt.
                row = store.connection.execute(
                    "SELECT sender, capability_metadata_json, model_used FROM messages WHERE user_id=? AND chat_id=? AND message_id=?",
                    (store.user_id, chat_id, message_id),
                ).fetchone()
                if not row or row["sender"] not in {"ai", "assistant"}:
                    return False
                metadata = json.loads(row["capability_metadata_json"] or "{}")
                if not isinstance(metadata, dict) or metadata.get("generation_mode") != "live_native":
                    return False
                if row["model_used"] and row["model_used"] != record["model"]:
                    return False
                store.save_message_model_diagnostic(message_id, chat_id, "", datetime.now(timezone.utc).isoformat(), record["model"])
                record["binding"] = binding
                return True
            finally:
                store.close()


JPEG_MAGIC = bytes((0xFF, 0xD8, 0xFF))
PNG_MAGIC = bytes((0x89, 0x50, 0x4E, 0x47))


class GeminiLiveTransport:
    """Keep Google credentials and the persistent Live socket on the backend."""

    # A continued turn (speech resumed after activity_end) sometimes gets no reply until the first-output recovery:
    # Gemini finished the retired generation instead of answering the new activity. One nudge on the SAME session
    # (no new model request, no reconnect) after CONTINUATION_NUDGE_S; the 15 s recovery deadline is unchanged.
    CONTINUATION_NUDGE_S = 4
    # Marker 220 (owner: Live "stuck at listening" after a few turns): 15 s felt like a hang and users gave up first.
    # WT telemetry, 367 plain turns: first output p50 1.35 s, p97 5.4 s, 3 over 8 s. App-owned work (computer control,
    # web search, memory) never counts: its "suppressing" phase keeps resetting the clock below.
    FIRST_OUTPUT_TIMEOUT_S = 8       # after the provider accepted activity_end (activity_submitted)
    PROGRESS_TIMEOUT_S = 30          # between output chunks once the reply has started

    INPUT_MIME = "audio/pcm;rate=16000"

    @staticmethod
    def _catalog_allows(route, scope, api_key) -> bool:
        from services.model_discovery import credential_reference
        catalog = getattr(route, "discovery_access", {}).get(credential_reference(scope.value, api_key))
        if catalog is None:
            return True  # Audited contract can be tried; not proof of entitlement.
        entry = (catalog.get("models") or {}).get(route.model_id)
        if not isinstance(entry, dict) or entry.get("available") is False or entry.get("catalog_free_status") == "paid":
            return False
        methods = entry.get("supported_generation_methods") or []
        return bool((not methods or "bidiGenerateContent" in methods) and (
            not entry.get("modalities_explicit") or (
                "audio" in entry.get("input_modalities", []) and "audio" in entry.get("output_modalities", []))))

    @staticmethod
    def _credential_attempts(model_id: str) -> list[tuple[CredentialScope, str, str, str]]:
        from config.credential_policy import managed_google_policies

        attempts: list[tuple[CredentialScope, str, str, str]] = []
        context = current_request_context()
        # The special Jupiter credential is an account-policy entitlement, not
        # a role shortcut.  In particular, an Admin must still be able to use
        # an account-scoped key and must not inherit the owner/testing slot.
        uses_jupiter_user_google = bool(context.managed_google_credentials)
        # A managed-key entitlement adds fallback, not replacement of a personal key.
        user_key = resolve_request_credential(CredentialScope.USER_GOOGLE.value)
        user_id = context.user_id or "unknown"
        if user_key:
            attempts.append((
                CredentialScope.USER_GOOGLE,
                user_key,
                "user-google",
                request_google_quota_group(),
            ))
        seen = {user_key} if user_key else set()
        for policy in managed_google_policies(
            model_id,
            "live",
            include_jupiter_user_google=uses_jupiter_user_google,
        ):
            secret = policy.secret()
            if not secret or secret in seen:
                continue
            seen.add(secret)
            attempts.append((
                CredentialScope.JUPITER_SHARED_GOOGLE,
                secret,
                policy.credential_id,
                policy.quota_group_key(),
            ))
        return attempts

    # Models whose official Live contract includes all video in a turn while audio stays activity-bounded.
    ALL_VIDEO_TURN_COVERAGE_MODELS = frozenset({"gemini-3.8-live"})
    PROVIDER_SETUP_TIMEOUT_S = 10.0    # websocket open + setup -> setupComplete (real WT: ~2-4 s end to end)
    STARTUP_BUDGET_S = 40.0            # all candidates together; inside the client's 45 s startup deadline
    ACTIVITY_SUBMIT_TIMEOUT_S = 8.0    # a finished turn must reach the provider (activity_end) within this
    APP_TURN_TIMEOUT_S = 7.0           # one app-owned turn: grounding (<=3.5 s) + render confirm (<=2 s) + margin

    @staticmethod
    def _config(purpose: str, target_language: str = "", session_context: str = "", model_id: str = "",
                voice_name: str = ""):
        from google.genai import types

        learning = purpose == "language_learning"
        instruction = (
            "Live voice transport rules: maintain a natural, low-latency spoken conversation. "
            "Understand multilingual and code-switched speech without translating it unless asked. "
            "Do not repeat a Roman transliteration after speaking the same native phrase. "
            "For language teaching, speak each native phrase once, then its meaning and useful explanation; "
            "Roman transliteration is display-only unless the user explicitly asks to hear it. "
            "Be concise enough for real-time conversation and never claim a tool result you did not receive. "
            "When the user interrupts with only stop, wait, hold on, enough or an equivalent in any language, "
            "stop the previous answer; stay silent or acknowledge very briefly, never launch a long reply. "
            "If the interruption includes a new question or correction, answer that new request instead."
        )
        if learning:
            instruction += " The current session is LanguageLearningMode, not interpreter translation."
        instruction += "\n" + LIVE_CURRENT_TURN_GUIDANCE
        instruction += "\n" + LIVE_SPOKEN_CONVERSATION_GUIDANCE
        # Text only (no tool/function declaration: an unverified one broke gemini-3.8-live setup in marker 150).
        from services.live_visual_grounding import LIVE_SCREEN_MARKING_GUIDANCE
        instruction += "\n" + LIVE_SCREEN_MARKING_GUIDANCE
        from services.desktop_intents import LIVE_DEVICE_GUIDANCE
        instruction += "\n" + LIVE_DEVICE_GUIDANCE
        from services.live_web_search import LIVE_WEB_GUIDANCE      # marker 203: web search is an app-owned turn
        instruction += "\n" + LIVE_WEB_GUIDANCE
        if session_context:
            instruction += (
                "\n\nPrivate session context. Use it only for this authenticated conversation; "
                "do not repeat or expose it unless relevant:\n" + str(session_context)[:LIVE_SESSION_CONTEXT_MAX_CHARS]
            )
        kwargs: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            # (Marker 215: AudioTranscriptionConfig(language_codes=...) is rejected by the Live API - "language_codes
            # parameter is not supported" - so the transcriber still guesses; "¿Qué?" noise is filtered after it.)
            "input_audio_transcription": types.AudioTranscriptionConfig(),
            "output_audio_transcription": types.AudioTranscriptionConfig(),
            "system_instruction": instruction,
            "speech_config": types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=normalize_live_voice(voice_name)   # female only; default Leda
                    )
                )
            ),
            "realtime_input_config": types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True
                ),
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                # Audio stays bounded by the manual activity window on every model. On 3.8 Live the turn also
                # includes the camera frames sent since the previous turn, so "what am I showing you?" sees the
                # current (possibly deduplicated, unchanged) frame even if it arrived just before speech began.
                turn_coverage=(
                    types.TurnCoverage.TURN_INCLUDES_AUDIO_ACTIVITY_AND_ALL_VIDEO
                    if model_id in GeminiLiveTransport.ALL_VIDEO_TURN_COVERAGE_MODELS
                    else types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY
                ),
            ),
            "session_resumption": types.SessionResumptionConfig(),
            # Official limit: without compression audio+video sessions end after 2 minutes (audio-only 15).
            # A sliding window keeps camera sessions alive; it only trims the oldest context.
            "context_window_compression": types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
        }
        if purpose == "translation":
            if not target_language:
                raise NativeLiveUnavailable("Live translation needs an explicit target language.")
            kwargs["translation_config"] = types.TranslationConfig(
                target_language_code=target_language,
                echo_target_language=False,
            )
        return types.LiveConnectConfig(**kwargs)

    @staticmethod
    def _auxiliary(state: dict, operation) -> bool:
        """Bounded, best-effort work, never joined by the audio sender/receiver."""
        tasks = state.setdefault("auxiliary", set())
        if len(tasks) >= 8:
            operation.close()
            state["diagnostic_failure"] = True
            return False
        started = False
        async def run():
            nonlocal started
            started = True
            try:
                await asyncio.wait_for(operation, 5)
            except Exception:
                state["diagnostic_failure"] = True
        task = asyncio.create_task(run())
        tasks.add(task)
        def finished(done):
            tasks.discard(done)
            if not started:
                # Cancellation before run() starts never transfers ownership
                # to wait_for; close the unscheduled diagnostic coroutine.
                operation.close()
        task.add_done_callback(finished)
        return True

    @staticmethod
    async def _watch_response(state: dict, session=None) -> None:
        connection_id = state.get("connection_id")
        while True:
            await asyncio.sleep(0.5)
            if state.get("connection_id") != connection_id:
                return  # A retired watchdog cannot fail a replacement connection.
            end_requested = state.get("end_requested_at")
            if end_requested and time.monotonic() - end_requested >= GeminiLiveTransport.ACTIVITY_SUBMIT_TIMEOUT_S:
                GeminiLiveTransport._turn_event(state, "activity_submit_stalled", detail="transport_stalled")
                raise NativeLiveUnavailable("Live did not accept the finished turn", "transport_stalled")
            if not state.get("pending"):
                continue
            started = state.get("response_started", False)
            turn = state.get("input_turn")
            if (session is not None and not started and turn in state.get("continuation_priors", {})
                    and state.get("nudged_turn") != turn and not state.get("generation_active")
                    and not state.get("app_turn")
                    and time.monotonic() - state["progress_at"] >= GeminiLiveTransport.CONTINUATION_NUDGE_S):
                from google.genai import types
                state["nudged_turn"] = turn
                with suppress(Exception):
                    await session.send_client_content(
                        turns=types.Content(role="user", parts=[types.Part(text=GeminiLiveTransport.WAITING_NUDGE)]),
                        turn_complete=True)
                    GeminiLiveTransport._turn_event(state, "waiting_nudged", turn, "continuation")
            # Phase-aware (real WT: first output arrives 0.9–1.6 s after acceptance on gemini-3.8-live, which has no
            # thinking phase). A silent provider is recovered for the SAME turn instead of showing "Generating…"
            # for a minute: replay on the next native model if intact, else degraded fallback.
            if (state.get("app_turn") or {}).get("phase") == "suppressing":
                state["progress_at"] = time.monotonic()  # marker 217: the app is still working (its own time limit)
                continue
            deadline = GeminiLiveTransport.PROGRESS_TIMEOUT_S if started else GeminiLiveTransport.FIRST_OUTPUT_TIMEOUT_S
            if time.monotonic() - state["progress_at"] >= deadline:
                reason = "progress_timeout" if started else "first_output_timeout"
                GeminiLiveTransport._turn_event(state, "response_timeout", turn, reason)   # marker 183: was untraced
                raise NativeLiveUnavailable("Live response stopped progressing", reason)

    @staticmethod
    def _turn_event(state: dict | None, event: str, turn: int | None = None, detail: str | None = None) -> None:
        """Fire-and-forget lifecycle telemetry; never blocks or fails the Live socket."""
        if not state:
            return
        try:
            from services.api_health_center import api_health_center
            args = dict(user_id=str(state.get("user_id") or ""), connection_id=str(state.get("connection_id") or ""),
                        provider_turn=int(turn if turn is not None else state.get("input_turn", state.get("turn", 0)) or 0),
                        event=event, detail=detail, exact_model_id=state.get("model_id"))
            asyncio.get_running_loop().run_in_executor(None, lambda: api_health_center().record_live_turn_event(**args))
        except Exception:
            pass

    @staticmethod
    async def _reject_activity(websocket, state: dict, command: dict, reason: str) -> None:
        """A dropped activity_end must be explicit: the client recovers that turn at once instead of waiting."""
        GeminiLiveTransport._turn_event(state, "activity_rejected", detail=reason)
        with suppress(Exception):
            await websocket.send_json({"type": "activity_rejected", "reason": reason,
                                       "logical_turn_id": str(command.get("logical_turn_id") or "")[:120]})

    @staticmethod
    def _logical_input_turns(state: dict, turn) -> list:
        owners = state.get("logical_turns", {})
        owner = owners.get(turn, "")
        return sorted(t for t, o in owners.items() if o == owner) if owner else [turn]

    @staticmethod
    def _multi_party_note(state: dict) -> str:
        """Marker 179: once the conversation shows another person is present ("inse poochho…", "ask her why…", "my
        friend wants to talk"), every turn is reminded that the speaker may not be the account holder. The audio has
        no speaker labels; this only keeps attribution and perspective right, it never identifies anyone."""
        if not (state or {}).get("multi_party"):
            return ""
        return ("SEVERAL PEOPLE PRESENT: this speech may come from the account holder or another participant (no speaker "
                "labels). Decide from context who is speaking (who you just addressed, how they refer to others), reply "
                "TO that person in your own voice and energy suited to them, and never speak as them or relay their "
                "words in their first person. Do not mention this note.")

    @staticmethod
    def _turn_context_notes(state: dict, *, typed: bool = False) -> str:
        """Marker 179, per turn, deterministic and local (no model call, no added round trip):
        - UNCLEAR SPEECH when this turn's transcript so far is a short fragment in a language that cannot belong to
          the conversation ("¿Qué?" / "好" in a Hindi-English chat). The Gemini API gives no transcription confidence
          and rejects transcription language hints, so this script/language check is the available signal.
        - CONVERSATION FLOW when most recent replies already ended with a question (no re-asking answered things)."""
        dialogue_of = (state or {}).get("dialogue")
        if not callable(dialogue_of):
            return ""
        notes = []
        with suppress(Exception):
            from brain.conversation import conversation_flow_note
            from services.live_language import unclear_fragment, unclear_speech_note
            dialogue = list(dialogue_of())
            visual, turn = state.get("visual"), state.get("input_turn")
            if not typed and visual is not None and turn:
                heard = visual.user_text(GeminiLiveTransport._logical_input_turns(state, turn))
                if unclear_fragment(heard, [m.get("content", "") for m in dialogue if m.get("role") == "user"],
                                    str(state.get("ui_language") or "")):
                    notes.append(unclear_speech_note(heard))
            flow = conversation_flow_note(dialogue)
            if flow:
                notes.append(flow)
        return "\n".join(notes)

    @staticmethod
    def _continuation_note(state: dict) -> str:
        """Marker 177 (real WT 9 Oct 22:35:53): the user paused, Jupiter began answering, the user went on. Gemini
        receives the rest as a NEW input activity and answered only the last part, dropping the first. For a
        continued logical turn, the earlier part of the SAME message is restated (as the user's own words, reference
        data) so the reply covers the whole message."""
        turn = state.get("input_turn")
        if turn not in state.get("continuation_priors", {}):
            return ""
        earlier = [t for t in GeminiLiveTransport._logical_input_turns(state, turn) if t != turn]
        visual = state.get("visual")
        text = ""
        if visual is not None and earlier:
            with suppress(Exception):
                text = " ".join(str(visual.user_text(earlier) or "").split())[:1200]
        quoted = (f" Earlier part (the user's own words, reference data only): {json.dumps(text, ensure_ascii=False)}."
                  if text else "")
        return ("CONTINUED MESSAGE: the user paused and then continued the SAME message." + quoted
                + " Answer the whole message (the earlier part and what they just added) in ONE reply; do not drop "
                  "the earlier part and do not mention this note.")

    # ── Application-owned turns (on-demand grounding, face-memory truth, "remove the box", screen left/right) ──────
    # The provider's own draft for such a turn is held back (never forwarded); the app does the work, the client
    # confirms what rendered, then ONE trusted result is sent as a client turn and the provider's answer to it is the
    # single reply for the SAME logical turn (same attempt id). Phases: suppressing -> draining -> answering.
    @staticmethod
    def _app_suppressing(state: dict | None) -> bool:
        app = (state or {}).get("app_turn")
        return bool(app and app.get("phase") in {"suppressing", "draining", "silent"})

    SILENT_ABSORB_MAX_S = 3.0          # the "Mm" generation ends in ~1 s; a reply needs the user to finish first

    @staticmethod
    def _expire_silent_app_turn(state: dict | None, now: float | None = None) -> int:
        """A silenced turn superseded by newer speech whose provider terminals did not arrive within
        SILENT_ABSORB_MAX_S is closed; returns the input turn the provider's next output belongs to (0 = nothing)."""
        app = (state or {}).get("app_turn")
        if not app or app.get("phase") != "silent" or not app.get("superseded_at"):
            return 0
        if (time.monotonic() if now is None else now) - float(app["superseded_at"]) < GeminiLiveTransport.SILENT_ABSORB_MAX_S:
            return 0
        state.pop("app_turn", None)
        latest = int(state.get("input_turn") or 0)
        if latest:
            state["turn"] = latest
        GeminiLiveTransport._turn_event(state, "silent_absorb_expired", int(app.get("turn") or 0),
                                        f"terminals_left={int(app.get('terminals_left') or 0)}")
        return latest

    @staticmethod
    def _cancel_app_turn(state: dict, reason: str) -> None:
        app = state.pop("app_turn", None)
        if not app:
            return
        app["cancelled"] = True
        task = app.get("task")
        if task is not None and not task.done():
            task.cancel()
        GeminiLiveTransport._turn_event(state, "app_turn_cancelled", app.get("turn"), reason)

    @staticmethod
    async def _maybe_start_app_turn(websocket, session, state: dict, visual) -> None:
        """Called for each input transcript of the SUBMITTED current turn, until the turn is decided."""
        turn = state.get("input_turn")
        if (not turn or not state.get("pending") or state.get("collecting") or state.get("app_decided_turn") == turn
                or (state.get("app_turn") or {}).get("turn") == turn):
            return
        text = visual.user_text(GeminiLiveTransport._logical_input_turns(state, turn))
        typed = turn in state.get("typed_turns", set())
        try:                                            # marker 213: a typed message is said to Jupiter by definition
            intent = await asyncio.wait_for(visual.decide(text, gate=False) if typed else visual.decide(text), 1.0)
        except Exception:
            intent = None
        take_events = getattr(visual, "take_client_events", None)
        for event in (take_events() if callable(take_events) else []):   # marker 206: e.g. woken by name
            with suppress(Exception):
                await websocket.send_json(event)
        if intent is None or state.get("input_turn") != turn or not state.get("pending"):
            return                                      # ordinary so far; a later transcript chunk may still decide
        if getattr(intent, "kind", "") == "silent" and turn in state.get("typed_turns", set()):
            return                       # quiet mode silences overheard speech; a message TYPED to Jupiter is answered
        state["app_decided_turn"] = turn
        owner = state.get("logical_turns", {}).get(turn, "")
        connection_id = state.get("connection_id")
        if state.get("response_started"):
            # Rare: the draft started before the transcript. Retract what the client already has for this attempt;
            # the app result then produces the single answer on the same attempt.
            with suppress(Exception):
                await websocket.send_json({"type": "app_turn_retract", "logical_turn_id": owner,
                                           "provider_turn_id": turn,
                                           "attempt_id": f'{connection_id or "legacy"}:{turn}'})
            state["response_started"] = False
        app = {"turn": turn, "owner": owner, "phase": "suppressing", "kind": intent.kind}
        state["app_turn"] = app
        state["progress_at"] = time.monotonic()
        GeminiLiveTransport._turn_event(state, "app_turn_started", turn, intent.kind)
        with suppress(Exception):                    # marker 204: the buddy shows what Jupiter is doing (labels only)
            await websocket.send_json({"type": "app_activity", "kind": intent.kind, "logical_turn_id": owner})

        async def send(event):
            if state.get("connection_id") == connection_id:
                await websocket.send_json(event)

        async def run():
            from google.genai import types
            try:
                from services.live_addressee import JUDGE_TIMEOUT_S
                from services.live_visual_grounding import (APP_ADDRESSEE, APP_DEVICE, COMPUTER_TASK_RESULT_TIMEOUT_S,
                                                            DEVICE_RESULT_TIMEOUT_S)
                limit = (max(DEVICE_RESULT_TIMEOUT_S, COMPUTER_TASK_RESULT_TIMEOUT_S) + 1.0 if intent.kind == APP_DEVICE
                         else GeminiLiveTransport.APP_TURN_TIMEOUT_S)
                if intent.kind == "wait_more":
                    limit = 8.0                       # marker 217: a short wait for the rest of the sentence
                if intent.kind == APP_ADDRESSEE:
                    # Marker 217 (real WT 25 Oct): the side-talk check (<=6 s) THEN the request itself (a device
                    # action, <=15 s) ran under the 7 s screen-mark limit; it timed out and Live said "screen marking
                    # isn't available, but I've played your song".
                    limit = JUDGE_TIMEOUT_S + 3.0 + max(COMPUTER_TASK_RESULT_TIMEOUT_S + 1.0, limit)
                note, summary = await asyncio.wait_for(visual.resolve_app_turn(intent, owner, send), limit)
            except asyncio.CancelledError:
                raise
            except Exception:
                from services.visual_intent import grounding_context_text
                note = (grounding_context_text({"reason": "unavailable"}) if intent.kind in {"visual", "clear"}
                        else GeminiLiveTransport.APP_FAILED_NOTE)
                summary = {"kind": intent.kind, "error": 1}
            state.setdefault("app_results", {})[turn] = (str(summary.get("kind") or ""), bool(summary.get("ok")))
            if state.get("app_turn") is not app or app.get("cancelled") or state.get("connection_id") != connection_id:
                return
            if summary.get("kind") == "silent" and not summary.get("named"):
                # Quiet mode: no answer at all. The client closes the turn silently; the draft stays suppressed and
                # its provider terminal closes the turn. If that draft already finished, one tiny generation (also
                # suppressed) supplies the terminal so turn ids stay in step.
                app["phase"] = "silent"
                state["pending"] = False
                state["progress_at"] = time.monotonic()
                GeminiLiveTransport._turn_event(state, "app_turn_result", turn, "kind=silent")
                with suppress(Exception):
                    await send({"type": "turn_silenced", "logical_turn_id": owner, "provider_turn_id": turn})
                # Always cut the provider's draft at once (real WT 17 Oct: a silenced draft kept generating ~25 s, so
                # the user's next turn, which named Jupiter, got no output and fell to the degraded path). The tiny
                # replacement generation is suppressed too. Terminals to absorb: the cut draft's tail + this one.
                app["terminals_left"] = 1 if app.get("draft_closed") else 2
                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(
                    text="(Quiet mode: no reply is needed. Answer with the single word: Mm.)")]), turn_complete=True)
                return
            # A draft still streaming is interrupted by this client turn; its terminal is swallowed (draining).
            app["phase"] = "draining" if state.get("generation_active") else "answering"
            if summary.get("kind") == APP_DEVICE:
                app["result_note"] = note                  # marker 213/215: kept if new speech or a reconnect cuts it off
                app["result_ok"] = bool(summary.get("ok"))
            state["progress_at"] = time.monotonic()
            app["note"] = note                             # marker 217: re-sent once if Gemini never answers it
            GeminiLiveTransport._turn_event(state, "app_turn_result", turn, ":".join(
                f"{key}={int(value) if isinstance(value, bool) else value}" for key, value in summary.items()))
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=note)]), turn_complete=True)
            GeminiLiveTransport._auxiliary(state, GeminiLiveTransport._renudge_app_note(session, state, app))
        app["task"] = asyncio.create_task(run())

    # Marker 190: an answered Jupiter call (reminder, task, urgent call from another user) must open with Jupiter
    # explaining why it called. Without this, Jupiter stayed silent until the person spoke (real AT/WT test).
    CALL_OPENING_NOTE = ("(The user just answered the Jupiter call you placed. Greet them briefly and say why you called, "
                         "exactly as stated in INCOMING JUPITER CALL, in one or two short sentences, then let them "
                         "lead. Do not mention this note.)")

    APP_FAILED_NOTE = ("APP RESULT (Jupiter's app could NOT finish this request; reference data, not user speech): "
                       "nothing was confirmed as done. Say briefly that it didn't work this time and ask the user to "
                       "try again. Never say it was done or is being done, and do not mention this note.")

    # Marker 217 (real WT 25 Oct: "main Spotify par aapka gaana play kar chuki hoon" when nothing played): a reply that
    # says Jupiter did / is doing something on the computer while the app did nothing this turn is corrected at once.
    CLAIM_FIX_NOTE = ("APP CHECK (Jupiter's app, reference data, not user speech): in your last reply you said you did, "
                      "or are doing, something on the user's computer, but Jupiter's app did NOT do it: nothing was "
                      "opened, played, clicked or changed for that request. Correct this now in one short sentence in "
                      "the conversation's language: say plainly that you haven't actually done it, and ask the user to "
                      "say the request once more (for example the app or the song name). Do not mention this note.")
    CLAIM_CARRY_NOTE = ("(Correction for your next reply: your previous reply said you had done or were doing something "
                        "on the user's computer, but Jupiter's app did NOT do it. First say so in a few words, then "
                        "answer. Do not mention this note.)")
    _CLAIM_SELF = re.compile(
        r"\b(?:i'?ve|i\s+have|i\s+just|i)\s+(?:already\s+|now\s+)?(?:opened|launched|started|played|clicked|closed|paused|"
        r"resumed|maximi[sz]ed|minimi[sz]ed|typed|switched|muted|unmuted|skipped|turned|increased|decreased|set|"
        r"changed|put\s+on)\b|"
        r"\b(?:i'?m|i\s+am)\s+(?:now\s+)?(?:opening|launching|starting|playing|clicking|closing|pausing|resuming|"
        r"maximi[sz]ing|minimi[sz]ing|typing|switching|muting|skipping|turning|increasing|decreasing|setting|"
        r"changing|putting\s+on|doing\s+(?:it|that))\b|"
        r"^(?:(?:got\s+it|okay|ok|sure|alright|right|done)[\s,!.]+)?(?:boss[\s,!.]+)?(?:now\s+)?(?:opening|playing|"
        r"closing|launching|starting|maximi[sz]ing|minimi[sz]ing|pausing|resuming|typing|switching|muting|"
        r"turning)\b|"
        r"\blet\s+me\s+(?:just\s+)?(?:open|play|start|close|launch|maximi[sz]e|minimi[sz]e|pause|switch|turn)\b|"
        r"\b(?:is|are)\s+now\s+(?:back|open|opened|closed|minimi[sz]ed|maximi[sz]ed|restored|paused|muted|on|off)\b|"
        r"\b(?:has|have)\s+been\s+(?:opened|closed|minimi[sz]ed|maximi[sz]ed|restored|paused|muted|played)\b|"
        r"\b(?:i'?m|i\s+am)\s+(?:now\s+)?(?:attempting|trying)\s+to\s+(?:open|play|start|close|maximi[sz]e|minimi[sz]e|"
        r"restore|click|type|pause|switch|change|set|turn)\b|"
        r"\bdone[,!.]?\s+(?:i'?ve|it'?s|the|your|spotify|youtube)\b|\b(?:is|are)\s+now\s+playing\b|\bnow\s+playing\b|"
        r"\b(?:chala|baja|laga|khol|band\s+kar|play\s+kar|open\s+kar|kar|bhej|likh|type\s+kar)\s*"
        r"(?:rahi|raha|di|diya|chuki|chuka|deti|deta)\s*(?:hai|hoon|hu|hun|h)?\b", re.IGNORECASE)
    _CLAIM_OBJECT = re.compile(r"\b(?:spotify|youtube|song|songs|gaana|gaane|gana|music|track|playlist|video|tab|window|"
                               r"app|chrome|edge|browser|volume|awaaz|brightness|notepad|settings|file|folder|"
                               r"screen|calculator|whatsapp|it|that|pe|par)\b", re.IGNORECASE)
    _CLAIM_HEDGE = re.compile(r"\b(?:can'?t|cannot|couldn'?t|didn'?t|haven'?t|not|nahi|nahin|unable|shall\s+i|should\s+i|"
                              r"want\s+me\s+to|would\s+you\s+like|do\s+you\s+want|kya\s+main|bataiye|batao)\b|\?",
                              re.IGNORECASE)

    @staticmethod
    def claims_unperformed_action(text: str) -> bool:
        """A sentence of the reply says Jupiter did / is doing a computer action (not hedged, not a question)."""
        # each clause on its own: "marking isn't available, but I've played your song" still claims the song
        for sentence in re.split(r"(?<=[.!?।])\s+|,?\s+\b(?:but|lekin|magar|however|though)\b\s+",
                                 " ".join(str(text or "").split()), flags=re.IGNORECASE):
            if (GeminiLiveTransport._CLAIM_SELF.search(sentence) and GeminiLiveTransport._CLAIM_OBJECT.search(sentence)
                    and not GeminiLiveTransport._CLAIM_HEDGE.search(sentence)):
                return True
        return False

    @staticmethod
    async def _check_action_claim(session, state: dict | None, turn: int) -> None:
        """Marker 217: correct a reply that claimed a computer action the app never did in this turn."""
        if not state:
            return
        said = str(state.pop("said_text", "") or "")
        results = state.setdefault("app_results", {})
        kind, ok = results.pop(turn, ("", False))
        while len(results) > 16:
            results.pop(next(iter(results)))
        if state.pop("unchecked_turn", None) == turn:            # the answer to an APP UPDATE / earlier correction
            return
        if said and kind != "personal":
            # Marker 221: normal Live voice may not promise a reminder / call that was never set, nor a memory save
            # that has not happened (real 26 Oct: "call karke yaad dila doongi", "yaad rakh liya").
            from memory.turn_memory import false_memory_claim
            from services.reminder_claims import false_reminder_claim
            if false_reminder_claim(said):
                GeminiLiveTransport._turn_event(state, "claim_corrected", turn, "reminder")
                await GeminiLiveTransport._send_correction(session, state, turn, GeminiLiveTransport.REMINDER_FIX_NOTE)
                return
            if false_memory_claim(said):
                # its own task: it may wait ~20 s for this turn's memory step (the 5 s auxiliary limit would drop it)
                tasks = state.setdefault("memory_claim_tasks", set())
                task = asyncio.get_running_loop().create_task(
                    GeminiLiveTransport._check_memory_claim(session, state, turn))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                return
        visual = state.get("visual")
        if (visual is None or not getattr(visual, "device_capabilities", None) or not said
                or (kind and not (kind in {"device", "device_unrecognised", "addressee"} and not ok))):
            return                                                 # an app result (web, memory, done action) answered
        last = getattr(visual, "_last_device", None)
        if last is not None and visual._clock() - last[1] <= 45:  # talking about an action that really just happened
            return
        if not GeminiLiveTransport.claims_unperformed_action(said):
            return
        GeminiLiveTransport._turn_event(state, "claim_corrected", turn)
        from google.genai import types
        busy = bool(state.get("collecting") or state.get("pending") or state.get("app_turn"))
        with suppress(Exception):
            if busy:
                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(
                    text=GeminiLiveTransport.CLAIM_CARRY_NOTE)]), turn_complete=False)
            else:
                state["unchecked_turn"] = turn + 1
                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(
                    text=GeminiLiveTransport.CLAIM_FIX_NOTE)]), turn_complete=True)

    REMINDER_FIX_NOTE = ("APP CHECK (Jupiter's app, reference data, not user speech): in your last reply you promised a "
                         "reminder or a call, but NO reminder or call was set. Correct this now in one short sentence in "
                         "the conversation's language: say plainly that you haven't set it, and ask the user for the time "
                         "and what to remind them about (for example \"1 baje yaad dila dena ki station aa raha hai\"). "
                         "Do not mention this note.")
    MEMORY_FIX_NOTE = ("APP CHECK (Jupiter's app, reference data, not user speech): in your last reply you said you saved, "
                       "noted or will remember something, but Jupiter's memory system did NOT save it. Correct this now in "
                       "one short sentence in the conversation's language: say you haven't saved it, and that you can if "
                       "they tell you clearly what to remember. Do not mention this note.")

    @staticmethod
    def memory_saved_note(lines: list[str]) -> str:
        return ("(APP UPDATE from Jupiter's memory system, reference data, not user speech: it has now really saved "
                + "; ".join(str(line) for line in lines[:6])[:500]
                + ". Tell the user in one short sentence that it's saved, in the conversation's language; if you already "
                  "said so, just say \"Done\". Do not mention this note.)")

    @staticmethod
    async def _send_correction(session, state: dict, turn: int, note: str) -> None:
        from google.genai import types
        busy = bool(state.get("collecting") or state.get("pending") or state.get("app_turn"))
        with suppress(Exception):
            if busy:
                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(
                    text="(Correction for your next reply: " + note + ")")]), turn_complete=False)
            else:
                state["unchecked_turn"] = turn + 1
                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=note)]),
                                                  turn_complete=True)

    @staticmethod
    async def _check_memory_claim(session, state: dict, turn: int) -> None:
        """Marker 221: the reply said "saved"; wait for this turn's real memory step (it starts when the turn closes) and
        correct the reply only if nothing was saved. A real save is confirmed by its own app update instead."""
        job = None
        for _ in range(16):                                   # the turn closes right after this check
            job = (state.get("memory_jobs") or {}).get(turn)
            if job is not None:
                break
            await asyncio.sleep(0.25)
        receipt = None
        if job is not None:
            with suppress(Exception):
                receipt = await asyncio.wait_for(asyncio.shield(job), 15)
        if getattr(receipt, "saved", False):
            return
        GeminiLiveTransport._turn_event(state, "claim_corrected", turn, "memory")
        await GeminiLiveTransport._send_correction(session, state, turn, GeminiLiveTransport.MEMORY_FIX_NOTE)

    @staticmethod
    async def _renudge_app_note(session, state: dict, app: dict, delay_s: float = 3.0, limit_s: float = 12.0) -> None:
        """Marker 217 (real WT 25 Oct, "close the calculator window" / "play the song Chogada Tara"): Gemini sometimes
        never answers the app result note (its held-back draft just finished, or nothing came at all) and only the
        15 s watchdog's reconnect recovered the turn - ~20 s of silence, the next words merged into the old turn. The
        note is sent once more when no answer has started ``delay_s`` after the draft ended."""
        started = time.monotonic()
        answering_since = None
        while time.monotonic() - started < limit_s:
            await asyncio.sleep(0.25)
            if (state.get("app_turn") is not app or app.get("cancelled") or app.get("note_resent")
                    or state.get("response_started")):
                return
            if app.get("phase") != "answering" or state.get("generation_active"):
                answering_since = None                      # the held-back draft is still streaming
                continue
            answering_since = answering_since or time.monotonic()
            if time.monotonic() - answering_since >= delay_s:
                break
        else:
            return
        app["note_resent"] = True
        state["progress_at"] = time.monotonic()
        GeminiLiveTransport._turn_event(state, "app_note_resent", int(app.get("turn") or 0))
        from google.genai import types
        with suppress(Exception):
            await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(
                text=str(app.get("note") or ""))]), turn_complete=True)

    @staticmethod
    async def _speak_pending_update(session, state: dict | None) -> None:
        if not state or not state.get("pending_update"):
            return
        if state.get("collecting") or state.get("pending") or state.get("generation_active") or state.get("app_turn"):
            return                                                   # spoken after the current turn instead
        from google.genai import types
        text = str(state.pop("pending_update"))
        state["unchecked_turn"] = int(state.get("turn") or 0)
        with suppress(Exception):
            await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(
                text="APP UPDATE (Jupiter's task, reference data, not user speech): " + text
                + " Say this in one short sentence in the conversation's language. Do not mention this note.")]),
                turn_complete=True)

    DONE_CARRY_NOTE = ("(Your confirmation of the last action was cut off by new speech. The action WAS done; in your "
                       "next reply first say so in a few words, then answer. Result: ")

    WAITING_NUDGE = ("(The user has finished speaking for this turn. Respond now to what they just said, in the "
                     "conversation's current language. Do not mention this note.)")

    @staticmethod
    async def _resolve_waiting_for_input(session, state: dict, turn_id: int, emit) -> None:
        from google.genai import types
        if state.get("nudged_turn") == turn_id:
            raise NativeLiveUnavailable("Live kept waiting for more input after the turn ended", "empty_response")
        state["nudged_turn"] = turn_id
        state["progress_at"] = time.monotonic()             # a fresh, still bounded, first-output window
        try:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=GeminiLiveTransport.WAITING_NUDGE)]),
                turn_complete=True,
            )
        except Exception:
            raise NativeLiveUnavailable("Live could not be asked to respond", "empty_response") from None
        GeminiLiveTransport._turn_event(state, "waiting_nudged", turn_id)
        await emit({"type": "waiting_for_input", "action": "nudged"})

    # ── Typed turns inside the SAME Live session ────────────────────────────────────────────────────────────────
    # A message typed in the Live composer is one more user turn of this conversation: it gets the next provider turn
    # id, the same logical-turn/attempt identity and watchdogs as a spoken turn, and reaches the model as client
    # content (which the Live API appends to the conversation and which interrupts a generation in progress).
    TEXT_TURN_MAX_CHARS = 2000
    VISUAL_SOURCES = frozenset({"none", "camera", "screen"})
    SCREEN_CONTEXT_NOTE = ("(Visual context for this turn: the video frames in this conversation currently show the "
                           "user's SHARED SCREEN, not a camera. Refer to on-screen items by their visible labels and "
                           "positions; if text is too small or a screen looks blank or protected, say so instead of "
                           "guessing. Do not mention this note.)")

    @staticmethod
    def _visual_context_note(state: dict | None) -> str:
        return GeminiLiveTransport.SCREEN_CONTEXT_NOTE if (state or {}).get("visual_source") == "screen" else ""

    @staticmethod
    def _register_diagnostic_target(state: dict, command: dict, turn_id: int) -> None:
        """Bind the assistant message that will receive this turn's model diagnostic (validated off the hot path)."""
        if not (state and state.get("enabled")):
            return
        target = command.get("diagnostic_target")
        if not isinstance(target, dict):
            return
        chat_id = str(target.get("chatId") or "")
        message_id = str(target.get("messageId") or "")
        if (chat_id == state.get("chat_id")
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", message_id)
                and len(state["used_messages"]) < 4096
                and message_id not in state["used_messages"]
                and turn_id not in state["targets"]):
            state["used_messages"].add(message_id)
            target_record = {"target": (chat_id, message_id), "ready": asyncio.Event(), "available": False}
            state["targets"][turn_id] = target_record

            async def validate(record=target_record, chat=chat_id, msg=message_id):
                record["available"] = await asyncio.to_thread(
                    LiveMessageDiagnostics.target_available, state["user_id"], chat, msg,
                )
                record["ready"].set()
            GeminiLiveTransport._auxiliary(state, validate())

    @staticmethod
    async def _submit_text_turn(websocket, session, state: dict | None, command: dict) -> None:
        from google.genai import types

        text = " ".join(str(command.get("text") or "").split())[:GeminiLiveTransport.TEXT_TURN_MAX_CHARS]
        app_note = bool(command.get("app_note"))
        if app_note and text:
            # Marker 217: the outcome of a task Jupiter is doing (sent by Jupiter's own app after the reply), spoken
            # as an update; never the user's words and never a command.
            text = ("APP UPDATE (Jupiter's task, reference data, not user speech): " + text[:600]
                    + " Tell the user this now in one short sentence in the conversation's language. Do not mention "
                      "this note.")
        logical_id = str(command.get("logical_turn_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", logical_id):
            logical_id = ""
        if not text or not logical_id:
            await GeminiLiveTransport._reject_activity(websocket, state or {}, command, "empty_text")
            return
        if state is None:                                  # legacy bridge without turn accounting
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)
            return
        if state.get("collecting"):
            # A spoken turn is open: a typed message is never merged into it (and never sent through both channels).
            await GeminiLiveTransport._reject_activity(websocket, state, command, "voice_turn_open")
            return
        owners = state.setdefault("logical_turns", {})
        if logical_id in owners.values():
            return                                         # the same typed turn delivered twice
        if state.get("app_turn"):
            GeminiLiveTransport._cancel_app_turn(state, "new_text_turn")
        previous = int(state.get("input_turn", 0) or 0)
        superseded = bool(state.get("pending") and previous and not state.get("response_started"))
        input_turn = max(int(state["turn"]) + int(bool(state.get("pending"))), previous + 1)
        if superseded:
            # The earlier turn never produced output: its generation (if any) belongs to this typed turn from now on.
            state.setdefault("text_supersedes", {})[input_turn] = previous
            state.setdefault("retired_input_turns", set()).add(previous)
        if state.get("generation_active"):
            # A reply is streaming: this client turn interrupts it. Late packets of that reply are dropped until the
            # new generation's first audio (see _send_provider_events).
            state["text_boundary_turn"] = input_turn
        state.update(collecting=False, replay=[], replay_bytes=0, replayable=True, pending=False,
                     response_started=False, input_turn=input_turn, current_grounding="")
        owners[input_turn] = logical_id
        while len(owners) > 64:
            retired = next(iter(owners))
            owners.pop(retired)
            state.setdefault("audio_input_turns", set()).discard(retired)
            state.get("text_turns", {}).pop(retired, None)
        # Accepted input (typed): provider output for this turn is solicited, exactly like a spoken turn's.
        state.setdefault("audio_input_turns", set()).add(input_turn)
        state.setdefault("text_turns", {})[input_turn] = text
        attempt_id = f'{state.get("connection_id", "legacy")}:{input_turn}'
        await websocket.send_json({"type": "activity_started", "provider_turn_id": input_turn,
                                   "logical_turn_id": logical_id, "attempt_id": attempt_id, "input_source": "text"})
        GeminiLiveTransport._register_diagnostic_target(state, command, input_turn)
        note = "\n".join(part for part in (GeminiLiveTransport._visual_context_note(state),
                                           GeminiLiveTransport._multi_party_note(state),
                                           GeminiLiveTransport._turn_context_notes(state, typed=True)) if part)
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=f"{note}\n{text}" if note else text)]),
            turn_complete=True)
        state.update(pending=True, progress_at=time.monotonic())
        GeminiLiveTransport._turn_event(state, "text_turn_submitted", input_turn)
        await websocket.send_json({"type": "activity_submitted", "provider_turn_id": input_turn,
                                   "logical_turn_id": logical_id, "attempt_id": attempt_id, "input_source": "text"})
        # Marker 208 (real WT 23 Oct): a typed Live message never reached the app-owned turns (device actions, web
        # search, quiet mode), so "minimize Notepad" typed in Live got "Sorry, I can't" from the model. Typed words are
        # the turn's transcript; the same decision runs as for speech.
        visual = state.get("visual")
        if app_note:
            state["unchecked_turn"] = input_turn                 # the update itself is not a claim to be checked
            return
        if visual is not None:
            with suppress(Exception):
                visual.note_user(input_turn, text)
            state.setdefault("typed_turns", set()).add(input_turn)
            await GeminiLiveTransport._maybe_start_app_turn(websocket, session, state, visual)

    @staticmethod
    def _has_model_audio(content) -> bool:
        return any(getattr(getattr(part, "inline_data", None), "data", None)
                   for part in list(getattr(getattr(content, "model_turn", None), "parts", None) or []))

    @staticmethod
    async def _replay_turn(session, state: dict) -> None:
        from google.genai import types
        typed = state.get("text_turns", {}).get(state.get("input_turn"))
        carried = str(state.pop("carried_note", "") or "")       # marker 215: the action's real result, not a re-run
        if typed:
            # A typed turn is replayed as the same text on the next native route, never as audio.
            note = GeminiLiveTransport._visual_context_note(state)
            text = f"{note}\n{typed}" if note else typed
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=f"{text}\n\n{carried}" if carried else text)]),
                turn_complete=True)
            state["progress_at"] = time.monotonic()
            return
        await session.send_realtime_input(activity_start=types.ActivityStart())
        for frame in state.get("replay", []):
            await session.send_realtime_input(audio=types.Blob(data=frame, mime_type=GeminiLiveTransport.INPUT_MIME))
        if state.get("current_grounding"):
            await session.send_realtime_input(text=state["current_grounding"])
        if carried:
            await session.send_realtime_input(text=carried)
        await session.send_realtime_input(activity_end=types.ActivityEnd())
        state["progress_at"] = time.monotonic()

    VIDEO_MIME_TYPES = frozenset({"image/jpeg", "image/png"})
    VIDEO_MAX_BYTES = 256 * 1024
    VIDEO_MIN_INTERVAL_SECONDS = 0.95   # official Live guidance: video frames at most 1 per second

    @staticmethod
    def accept_video_frame(command: dict, video_state: dict, now: float) -> tuple[bytes, str] | None:
        """Validate one camera frame. Video is a separate input stream: it never starts, ends or extends a
        speech activity, never enters the audio replay buffer and is never persisted or logged."""
        mime = str(command.get("mime_type") or "")
        data = command.get("data")
        if mime not in GeminiLiveTransport.VIDEO_MIME_TYPES or not isinstance(data, str) or not data:
            return None
        if len(data) > GeminiLiveTransport.VIDEO_MAX_BYTES * 4 // 3 + 8:
            return None
        if now - float(video_state.get("last_at", -1e9)) < GeminiLiveTransport.VIDEO_MIN_INTERVAL_SECONDS:
            video_state["dropped"] = int(video_state.get("dropped", 0)) + 1
            return None
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            return None
        signature = raw[:4]
        if not raw or len(raw) > GeminiLiveTransport.VIDEO_MAX_BYTES or not (
                (mime == "image/jpeg" and signature[:3] == JPEG_MAGIC)
                or (mime == "image/png" and signature == PNG_MAGIC)):
            return None
        video_state["last_at"] = now
        video_state["sent"] = int(video_state.get("sent", 0)) + 1
        return raw, mime

    @staticmethod
    async def _send_client_audio(websocket, session, diagnostic_state: dict | None = None) -> None:
        from google.genai import types

        video_state: dict = {}
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            audio = message.get("bytes")
            if audio:
                # Manual activity owns input. Idle/late PCM is not a new turn;
                # digitally silent buffers provide no transcription evidence.
                if diagnostic_state is not None and not diagnostic_state.get("collecting"):
                    continue
                if not any(audio):
                    continue
                if diagnostic_state is not None and diagnostic_state.get("collecting"):
                    if not diagnostic_state["replay_bytes"]:
                        await session.send_realtime_input(activity_start=types.ActivityStart())
                    diagnostic_state.pop("empty_activity_previous", None)
                    diagnostic_state.setdefault("audio_input_turns", set()).add(diagnostic_state["input_turn"])
                    diagnostic_state["replay_bytes"] += len(audio)
                    if diagnostic_state["replay_bytes"] <= 8 * 1024 * 1024:
                        diagnostic_state["replay"].append(bytes(audio))
                    else:
                        # Never truncate/replay an incomplete long utterance.
                        diagnostic_state["replay"] = []
                        diagnostic_state["replayable"] = False
                await session.send_realtime_input(
                    audio=types.Blob(data=bytes(audio), mime_type=GeminiLiveTransport.INPUT_MIME)
                )
                continue
            raw = message.get("text")
            if not raw:
                continue
            try:
                command = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(command, dict):
                continue
            event_type = str(command.get("type") or "")
            if event_type == "video_frame":
                frame = GeminiLiveTransport.accept_video_frame(command, video_state, time.monotonic())
                if frame is not None:
                    await session.send_realtime_input(video=types.Blob(data=frame[0], mime_type=frame[1]))
                    if str(command.get("source") or "camera") == "screen":
                        # Shared-screen frames reach the model only: on-demand box drawing/face labels belong to the
                        # camera preview and never run on a user's screen.
                        continue
                    visual = diagnostic_state.get("visual") if diagnostic_state is not None else None
                    if visual is not None:
                        with suppress(Exception):              # optional feature: never affects the video stream
                            visual.accept_frame(frame[0], mirrored=bool(command.get("mirrored"))
                                                if "mirrored" in command else None)   # memory only
                    # Telemetry label only (never frame data). Kept in the VIDEO-local state: video never writes to
                    # the speech-turn diagnostic state.
                    if not video_state.get("camera_reported"):
                        video_state["camera_reported"] = True
                        callback = diagnostic_state.get("on_camera_live") if diagnostic_state is not None else None
                        if callable(callback):
                            with suppress(Exception):
                                callback()
                continue
            if event_type == "visual_render_result":
                visual = diagnostic_state.get("visual") if diagnostic_state is not None else None
                if visual is not None:
                    with suppress(Exception):
                        visual.resolve_render(str(command.get("request_id") or "")[:32], bool(command.get("rendered")),
                                              str(command.get("reason") or ""))
                continue
            if event_type == "camera_state":
                visual = diagnostic_state.get("visual") if diagnostic_state is not None else None
                if visual is not None:
                    with suppress(Exception):
                        visual.set_camera(on=bool(command.get("on")), mirrored=bool(command.get("mirrored")))
                continue
            if event_type == "visual_source":
                # One primary visual source at a time (none | camera | screen); a label only, never frame data.
                source = str(command.get("source") or "none")
                if diagnostic_state is not None and source in GeminiLiveTransport.VISUAL_SOURCES:
                    diagnostic_state["visual_source"] = source
                    visual = diagnostic_state.get("visual")
                    if visual is not None and source != "camera":
                        with suppress(Exception):
                            visual.set_camera(on=False, mirrored=False)
                continue
            if event_type == "device_capabilities":
                # Marker 197: the Windows app says which device actions it can do (labels only, no data).
                visual = diagnostic_state.get("visual") if diagnostic_state is not None else None
                if visual is not None:
                    with suppress(Exception):
                        visual.set_device_capabilities(command.get("capabilities"))
                        caps = visual.device_capabilities or {}
                        GeminiLiveTransport._turn_event(diagnostic_state, "device_capabilities", 0,
                                                        "+".join(sorted(k for k, v in caps.items() if v)) or "none")
                continue
            if event_type == "quiet_mode":
                # Marker 203: quiet mode survives a reconnect / return to native Live (a NEW bridge has a fresh
                # LiveVisualGrounding; real WT 17 Oct the new session forgot it). The client re-announces it.
                visual = diagnostic_state.get("visual") if diagnostic_state is not None else None
                if visual is not None:
                    visual.quiet_mode = bool(command.get("on"))
                    visual.quiet_wake_on_name = command.get("wake_on_name") is not False
                continue
            if event_type == "device_action_result":
                visual = diagnostic_state.get("visual") if diagnostic_state is not None else None
                if visual is not None:
                    with suppress(Exception):
                        visual.resolve_device(str(command.get("request_id") or "")[:32], bool(command.get("ok")),
                                              str(command.get("summary") or ""), bool(command.get("finished")))
                continue
            if event_type == "device_update":
                # Progress / outcome of a running phone or computer task: spoken between turns only (never over the
                # user or a reply). Marker 217: one that arrives mid-turn is kept and spoken when the turn ends.
                text = " ".join(str(command.get("text") or "").split())[:300]
                if text and diagnostic_state is not None:
                    diagnostic_state["pending_update"] = text
                    await GeminiLiveTransport._speak_pending_update(session, diagnostic_state)
                continue
            if event_type == "user_text":
                await GeminiLiveTransport._submit_text_turn(websocket, session, diagnostic_state, command)
                continue
            if event_type == "audio_stream_end":
                await session.send_realtime_input(audio_stream_end=True)
            elif event_type == "activity_start":
                if diagnostic_state is not None:
                    if diagnostic_state.get("collecting"):
                        continue
                    # A barge-in may start before the old interrupted packet.
                    # That old terminal must not erase the new input's buffer.
                    logical_id = str(command.get("logical_turn_id") or "")
                    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", logical_id):
                        logical_id = ""  # Legacy clients retain numeric ownership.
                    owners = diagnostic_state.setdefault("logical_turns", {})
                    current_owner = owners.get(diagnostic_state.get("input_turn"), "")
                    continuation = bool(command.get("continuation")) and bool(logical_id) and logical_id == current_owner
                    if logical_id and logical_id in owners.values() and not continuation:
                        continue  # A duplicate start cannot replace/reopen A's buffers.
                    pending_app = diagnostic_state.get("app_turn")
                    if pending_app and pending_app.get("phase") == "silent":
                        # Marker 206 (real WT 23 Oct): a silenced quiet-mode turn still has provider terminals to
                        # absorb (its cut draft + the tiny "Mm" generation). Cancelling it here let those terminals
                        # through as ordinary turns: the turn counter moved 2-3 times, the next reply ("Jupiter jag
                        # jao") was stamped with a retired turn id, the client dropped every packet as stale, and
                        # after 20 s Live fell back to another model (different voice). Keep absorbing; bounded below.
                        pending_app.setdefault("superseded_at", time.monotonic())
                    elif pending_app:
                        # The user spoke again (barge-in / continuation): the pending app result is obsolete.
                        done_note = str(pending_app.get("result_note") or "") if pending_app.get("result_ok") else ""
                        GeminiLiveTransport._cancel_app_turn(diagnostic_state, "new_activity")
                        if done_note:
                            # Marker 213 (real WT 24 Oct: "maximize my Edge window" worked, but someone spoke before
                            # Jupiter said so, and it never said anything): what was already done stays in the
                            # conversation and the next reply mentions it.
                            from google.genai import types
                            with suppress(Exception):
                                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(
                                    text=GeminiLiveTransport.DONE_CARRY_NOTE + done_note)]), turn_complete=False)
                    if continuation:
                        # SAME logical turn: the user resumed speaking after activity_end, before any reply was
                        # presented. A new provider activity carries the rest of the utterance (its start interrupts
                        # the pending generation); earlier segments stay attributed to this logical turn.
                        diagnostic_state.setdefault("continuation_priors", {})[
                            max(int(diagnostic_state["turn"]) + int(bool(diagnostic_state.get("pending"))),
                                int(diagnostic_state.get("input_turn", 0)) + 1)] = int(diagnostic_state.get("input_turn", 0))
                        GeminiLiveTransport._turn_event(diagnostic_state, "continuation_started",
                                                        int(diagnostic_state.get("input_turn", 0)))
                    input_turn = max(int(diagnostic_state["turn"]) + int(bool(diagnostic_state.get("pending"))),
                                     int(diagnostic_state.get("input_turn", 0)) + 1)
                    diagnostic_state["empty_activity_previous"] = int(diagnostic_state.get("input_turn", 0))
                    diagnostic_state.update(collecting=True, replay=[], replay_bytes=0,
                                            replayable=True, pending=False, response_started=False,
                                            input_turn=input_turn, current_grounding="")
                    owners[input_turn] = logical_id
                    while len(owners) > 64:
                        retired = next(iter(owners))
                        owners.pop(retired)
                        diagnostic_state.setdefault("audio_input_turns", set()).discard(retired)
                    await websocket.send_json({"type": "activity_started", "provider_turn_id": input_turn,
                                               "logical_turn_id": logical_id,
                                               "attempt_id": f'{diagnostic_state.get("connection_id", "legacy")}:{input_turn}',
                                               **({"continuation": True} if continuation else {})})
                else:
                    await session.send_realtime_input(activity_start=types.ActivityStart())
            elif event_type == "activity_end":
                if diagnostic_state is not None:
                    if not diagnostic_state.get("collecting"):
                        accepted_owner = diagnostic_state.get("logical_turns", {}).get(diagnostic_state.get("input_turn"), "")
                        if not (diagnostic_state.get("pending") and accepted_owner
                                and command.get("logical_turn_id") == accepted_owner):
                            # Not a harmless duplicate close of the turn already being answered.
                            await GeminiLiveTransport._reject_activity(websocket, diagnostic_state, command, "no_open_activity")
                        continue
                    if diagnostic_state.get("input_turn") not in diagnostic_state.get("audio_input_turns", set()):
                        diagnostic_state["collecting"] = False
                        empty_turn = diagnostic_state["input_turn"]
                        diagnostic_state.get("logical_turns", {}).pop(empty_turn, None)
                        diagnostic_state["input_turn"] = diagnostic_state.pop("empty_activity_previous", 0)
                        await GeminiLiveTransport._reject_activity(websocket, diagnostic_state, command, "no_audio")
                        continue
                    owner = diagnostic_state.get("logical_turns", {}).get(diagnostic_state.get("input_turn"), "")
                    if owner and command.get("logical_turn_id") != owner:
                        continue  # A delayed close may not finalize a newer activity (the newer turn owns it).
                    diagnostic_state["end_requested_at"] = time.monotonic()   # cleared once the provider has it
                    grounding = live_verbatim_grounding(str(command.get("current_transcript") or "")[:16000])
                    note = GeminiLiveTransport._visual_context_note(diagnostic_state)
                    if note:
                        grounding = f"{note}\n{grounding}" if grounding else note
                    party = "\n".join(part for part in (GeminiLiveTransport._multi_party_note(diagnostic_state),
                                                        GeminiLiveTransport._turn_context_notes(diagnostic_state))
                                      if part)
                    if party:
                        grounding = f"{party}\n{grounding}" if grounding else party
                    continued = GeminiLiveTransport._continuation_note(diagnostic_state)
                    if continued:
                        grounding = f"{continued}\n{grounding}" if grounding else continued
                    diagnostic_state["current_grounding"] = grounding
                    if grounding:
                        await session.send_realtime_input(text=grounding)
                if diagnostic_state and diagnostic_state.get("enabled"):
                    target = command.get("diagnostic_target")
                    if isinstance(target, dict):
                        chat_id = str(target.get("chatId") or "")
                        message_id = str(target.get("messageId") or "")
                        turn_id = int(diagnostic_state.get("input_turn", diagnostic_state["turn"]))
                        # Registration precedes provider generation. The later
                        # HTTP receipt cannot choose or change its destination.
                        if (chat_id == diagnostic_state.get("chat_id")
                                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", message_id)
                                and len(diagnostic_state["used_messages"]) < 4096
                                and message_id not in diagnostic_state["used_messages"]
                                and turn_id not in diagnostic_state["targets"]):
                            # Fix destination now, but validate in a separate task.
                            # SQLite must never hold activity_end behind its lock.
                            diagnostic_state["used_messages"].add(message_id)
                            target_record = {"target": (chat_id, message_id), "ready": asyncio.Event(), "available": False}
                            diagnostic_state["targets"][turn_id] = target_record
                            async def validate(record=target_record, chat=chat_id, msg=message_id):
                                record["available"] = await asyncio.to_thread(
                                    LiveMessageDiagnostics.target_available,
                                    diagnostic_state["user_id"], chat, msg,
                                )
                                record["ready"].set()
                            GeminiLiveTransport._auxiliary(diagnostic_state, validate())
                if diagnostic_state is not None:
                    diagnostic_state.update(collecting=False)
                await session.send_realtime_input(activity_end=types.ActivityEnd())
                if diagnostic_state is not None:
                    diagnostic_state.pop("end_requested_at", None)
                if diagnostic_state is not None:
                    submitted = diagnostic_state.get("input_turn")
                    if submitted not in diagnostic_state.get("retired_input_turns", set()):
                        diagnostic_state.update(pending=True, progress_at=time.monotonic())
                        GeminiLiveTransport._turn_event(diagnostic_state, "activity_submitted", submitted)
                        await websocket.send_json({"type": "activity_submitted", "provider_turn_id": submitted,
                            "logical_turn_id": diagnostic_state.get("logical_turns", {}).get(submitted, ""),
                            "attempt_id": f'{diagnostic_state.get("connection_id", "legacy")}:{submitted}'})
                        # Marker 217 (WT test 25 Oct: "Jupiter, can you open." ... "the settings" - the rest was
                        # transcribed while the user was still speaking, so the whole sentence was never decided and
                        # Live said "let me open that" with nothing done): decide once more on what has arrived.
                        visual = diagnostic_state.get("visual")
                        if visual is not None:
                            with suppress(Exception):             # an optional feature never blocks the turn
                                if visual.user_text(GeminiLiveTransport._logical_input_turns(diagnostic_state, submitted)):
                                    await GeminiLiveTransport._maybe_start_app_turn(websocket, session, diagnostic_state,
                                                                                     visual)
            elif (
                event_type == "text"
                and command.get("channel") == "USER_TRANSCRIPT"
                and command.get("text")
            ):
                if diagnostic_state is not None and (
                    not diagnostic_state.get("collecting")
                    or diagnostic_state.get("input_turn") not in diagnostic_state.get("audio_input_turns", set())
                ):
                    continue
                await session.send_realtime_input(text=str(command["text"])[:2000])

    @staticmethod
    async def _send_provider_events(
        websocket, session, model_id: str, purpose: str, user_id: str, *, persist_diagnostics: bool = False,
        diagnostic_state: dict | None = None,
    ) -> None:
        provider_turn_id = int((diagnostic_state or {}).get("turn", 1))
        connection_id = (diagnostic_state or {}).get("connection_id")
        session_id = secrets.token_hex(16)
        audio_sequence = 0
        interrupted_boundary = False
        completed_input_turn = None
        generation_open = False
        from services.live_output_filter import StructuredOutputFilter
        output_filter = StructuredOutputFilter()
        async def emit(event, *, owner_turn=None):
            event_turn = provider_turn_id if owner_turn is None else owner_turn
            if diagnostic_state is not None and diagnostic_state.get("connection_id") != connection_id:
                return
            target = (diagnostic_state or {}).get("targets", {}).get(event_turn)
            output = bool((event.get("type") == "output_transcript" and str(event.get("text", "")).strip())
                          or (event.get("type") == "audio" and event.get("data")))
            if (diagnostic_state is not None and output
                    and diagnostic_state.get("input_turn", event_turn) == event_turn):
                if not diagnostic_state.get("response_started"):
                    GeminiLiveTransport._turn_event(diagnostic_state, "first_output", event_turn)
                diagnostic_state.update(response_started=True, progress_at=time.monotonic())
            # First output is sent before scheduling even optional receipt work.
            await websocket.send_json({**event, "provider_turn_id": event_turn,
                "attempt_id": f'{connection_id or "legacy"}:{event_turn}',
                "logical_turn_id": (diagnostic_state or {}).get("logical_turns", {}).get(event_turn, "")})
            if persist_diagnostics and target and not target.get("scheduled") and output:
                target["scheduled"] = True
                async def deliver(record=target, turn=provider_turn_id):
                    await record["ready"].wait()
                    if not record["available"]:
                        return
                    chat, msg = record["target"]
                    token = await asyncio.to_thread(
                        LiveMessageDiagnostics.issue, user_id, session_id, turn, model_id,
                        chat_id=chat, message_id=msg,
                    )
                    if token:
                        await websocket.send_json({"type": "diagnostic_receipt", "provider_turn_id": turn,
                                                   "message_id": msg, "diagnostic_receipt": token})
                GeminiLiveTransport._auxiliary(diagnostic_state, deliver())
        while True:
            # SDK receive() intentionally ends at each model turn; restarting
            # it preserves one multi-turn Live session.
            received = False
            async for response in session.receive():
                if diagnostic_state is not None and diagnostic_state.get("connection_id") != connection_id:
                    return
                received = True
                content = getattr(response, "server_content", None)
                # GENERATION ATTRIBUTION. Gemini counts model generations, not input activities. When a continuation
                # retired an activity BEFORE Gemini produced anything for it, no `interrupted`/`turn_complete` ever
                # closes that provider turn, so the reply to the continued activity would be stamped with the retired
                # turn's attempt id and the client would drop every packet as stale (real WT 19:13: 59 packets).
                # The FIRST output of a new generation therefore belongs to the latest submitted continuation.
                if (content is not None and diagnostic_state is not None and not generation_open
                        and (getattr(content, "model_turn", None) is not None
                             or getattr(getattr(content, "output_transcription", None), "text", ""))):
                    latest = diagnostic_state.get("input_turn")
                    priors = diagnostic_state.get("continuation_priors", {})
                    supersedes = diagnostic_state.get("text_supersedes", {})
                    # A typed turn that superseded a still-silent turn owns the next generation the same way.
                    if (latest and latest != provider_turn_id and diagnostic_state.get("pending")
                            and ((latest in priors and provider_turn_id in priors.values())
                                 or supersedes.get(latest) == provider_turn_id)):
                        provider_turn_id = int(latest)
                        diagnostic_state["turn"] = provider_turn_id
                        audio_sequence = 0
                    generation_open = True
                    diagnostic_state["generation_active"] = True
                if not content:
                    continue
                if diagnostic_state is not None and provider_turn_id not in diagnostic_state.get("audio_input_turns", set()):
                    # A final transcription can trail turn_complete. Retain its
                    # proven previous owner ONLY until another input activity
                    # starts. Never guess an old final belongs to new input B.
                    final_input = getattr(content, "input_transcription", None)
                    if (completed_input_turn is not None
                            and diagnostic_state.get("input_turn") == completed_input_turn
                            and not diagnostic_state.get("collecting")
                            and getattr(final_input, "text", "")):
                        # Conversational Live input_transcription is incremental;
                        # its final segment need not carry finished=True. The
                        # proven closed input boundary supplies finality here.
                        await emit({"type": "input_transcript", "channel": "USER_TRANSCRIPT",
                                    "text": str(final_input.text), "final": True},
                                   owner_turn=completed_input_turn)
                    continue  # Unsolicited provider output must not start a turn or diagnostics.
                has_input = any(getattr(getattr(content, key, None), "text", "") for key in (
                    "interim_input_transcription", "input_transcription"
                ))
                # A typed turn that interrupted a streaming reply produces no input transcript to mark its start.
                # Late transcript/terminal packets of the interrupted reply are dropped; the new generation's first
                # AUDIO (which precedes its own transcription) opens the typed turn's output.
                if (interrupted_boundary and not has_input and diagnostic_state is not None
                        and diagnostic_state.get("text_boundary_turn") == provider_turn_id):
                    if not GeminiLiveTransport._has_model_audio(content):
                        continue
                    interrupted_boundary = False
                    diagnostic_state.pop("text_boundary_turn", None)
                # An interruption closes the output generation even when Google
                # omits turn_complete. Otherwise the next user's packets reuse a
                # discarded ID and the client rejects their entire second turn.
                if interrupted_boundary and not has_input:
                    # Late output/terminal packets still belong to the retired
                    # generation. Do not relabel them as the next user's audio.
                    continue
                if has_input:
                    interrupted_boundary = False
                interim = getattr(content, "interim_input_transcription", None)
                input_transcription = getattr(content, "input_transcription", None)
                output_transcription = getattr(content, "output_transcription", None)
                resync = GeminiLiveTransport._expire_silent_app_turn(diagnostic_state)
                if resync:
                    provider_turn_id = resync
                    generation_open = False
                    audio_sequence = 0
                suppressed = GeminiLiveTransport._app_suppressing(diagnostic_state)
                for event_name, transcription in (
                    ("input_transcript", interim),
                    ("input_transcript", input_transcription),
                    ("output_transcript", output_transcription),
                ):
                    text = str(getattr(transcription, "text", "") or "")
                    if text and event_name == "output_transcript":
                        # Held-back draft of an app-owned turn, or structured detection payload: never user text.
                        text = "" if suppressed else output_filter.clean(text)
                    if text:
                        # An idle/retired receive window cannot create fresh input.
                        # Preserve the generation's owner; never stamp a late A
                        # packet with whichever client turn happens to be active.
                        if (event_name == "input_transcript" and diagnostic_state is not None
                                and "logical_turns" in diagnostic_state):
                            owners_now = diagnostic_state.get("logical_turns", {})
                            prior_of_current = (
                                owners_now.get(provider_turn_id)
                                and owners_now.get(provider_turn_id) == owners_now.get(diagnostic_state.get("input_turn"))
                                and provider_turn_id in diagnostic_state.get("continuation_priors", {}).values())
                            if (provider_turn_id not in owners_now
                                    or provider_turn_id not in diagnostic_state.get("audio_input_turns", set())
                                    or (provider_turn_id in diagnostic_state.get("retired_input_turns", set())
                                        and not prior_of_current)):
                                continue
                        if event_name == "output_transcript" and diagnostic_state is not None:
                            diagnostic_state["said_text"] = (str(diagnostic_state.get("said_text") or "") + text)[-2000:]
                        visual = (diagnostic_state or {}).get("visual")
                        if visual is not None:
                            with suppress(Exception):
                                if event_name == "input_transcript":
                                    visual.note_user(int(diagnostic_state.get("input_turn", provider_turn_id) or 0), text)
                                else:
                                    visual.note_assistant(provider_turn_id, text)
                        finished = bool(getattr(transcription, "finished", False))
                        # Identity/history processing belongs to the accepted
                        # canonical user turn, never an unvalidated ASR event.
                        await emit({
                            "type": event_name,
                            "channel": (
                                "USER_TRANSCRIPT"
                                if event_name == "input_transcript"
                                else "ASSISTANT_TRANSCRIPT"
                            ),
                            "text": text,
                            "final": finished,
                            "language": str(getattr(transcription, "language_code", "") or ""),
                        })
                        if event_name == "input_transcript" and visual is not None:
                            await GeminiLiveTransport._maybe_start_app_turn(websocket, session, diagnostic_state, visual)
                            suppressed = GeminiLiveTransport._app_suppressing(diagnostic_state)
                model_turn = getattr(content, "model_turn", None)
                if suppressed:
                    # The held-back draft of an app-owned turn: nothing of it reaches the client. Its terminal only
                    # closes the provider generation; the SAME turn stays pending for the app result's answer.
                    if getattr(content, "turn_complete", False) or getattr(content, "interrupted", False):
                        generation_open = False
                        audio_sequence = 0
                        output_filter.reset()
                        if diagnostic_state is not None:
                            diagnostic_state["generation_active"] = False
                            app = diagnostic_state.get("app_turn") or {}
                            was_draining = app.get("phase") == "draining"
                            if was_draining:
                                app["phase"] = "answering"
                            if getattr(content, "interrupted", False):
                                app["draft_interrupted"] = True
                            # 17 Oct (real WT, web search): the provider closes an interrupted draft with TWO
                            # messages, "interrupted" then a separate "turn_complete". That trailing turn_complete
                            # belongs to the dropped draft, not to the answer to the APP RESULT, and was read as
                            # "Live completed without output" -> fallback to another model without the results.
                            if getattr(content, "interrupted", False) and not getattr(content, "turn_complete", False):
                                app["draft_tail"] = True
                            if getattr(content, "turn_complete", False):
                                app["draft_closed"] = True
                            GeminiLiveTransport._turn_event(diagnostic_state, "app_turn_draft_dropped",
                                                            provider_turn_id)
                            # Quiet mode (marker 203): a silenced turn's provider terminal still closes the turn on
                            # the normal path (turn ids stay in step); none of its output ever reaches the client.
                            if app.get("phase") == "silent" and getattr(content, "turn_complete", False):
                                app["terminals_left"] = int(app.get("terminals_left", 1)) - 1
                                if app["terminals_left"] <= 0:          # the LAST terminal of the silenced turn
                                    diagnostic_state.pop("app_turn", None)
                                    model_turn = None
                                    suppressed = False
                    if suppressed:
                        continue
                for part in list(getattr(model_turn, "parts", None) or []):
                    inline = getattr(part, "inline_data", None)
                    data = getattr(inline, "data", None)
                    if isinstance(data, str):
                        with suppress(ValueError):
                            data = base64.b64decode(data)
                    if isinstance(data, (bytes, bytearray)) and data:
                        audio_sequence += 1
                        await emit({
                            "type": "audio",
                            "audio_sequence": audio_sequence,
                            "data": base64.b64encode(bytes(data)).decode("ascii"),
                            "mime_type": str(getattr(inline, "mime_type", "") or "audio/pcm;rate=24000"),
                        })
                app_now = (diagnostic_state or {}).get("app_turn") or {}
                if (getattr(content, "interrupted", False) and app_now.get("phase") == "answering"
                        and app_now.get("turn") == provider_turn_id and not app_now.get("cancelled")):
                    # Marker 217 (real WT 25 Oct): the app's result came back BEFORE Gemini began its own draft reply
                    # to the user's words, so that draft ("Chora chala rahi hoon Spotify pe!", "Sure, let me open")
                    # was taken for the answer; the result note then interrupted it, the turn closed, and the REAL
                    # answer ("Done, I've opened Spotify") was dropped as a stale turn. An interruption while the app
                    # turn is still open is the note cutting the draft: retract the draft, keep the turn for the answer.
                    with suppress(Exception):
                        await websocket.send_json({"type": "app_turn_retract", "logical_turn_id": app_now.get("owner", ""),
                                                   "provider_turn_id": provider_turn_id,
                                                   "attempt_id": f'{connection_id or "legacy"}:{provider_turn_id}'})
                    diagnostic_state.update(response_started=False, generation_active=False)
                    diagnostic_state.pop("said_text", None)
                    if not getattr(content, "turn_complete", False):
                        app_now["draft_tail"] = True
                    generation_open = False
                    audio_sequence = 0
                    output_filter.reset()
                    GeminiLiveTransport._turn_event(diagnostic_state, "app_turn_draft_retracted", provider_turn_id)
                    continue
                if getattr(content, "interrupted", False):
                    GeminiLiveTransport._turn_event(diagnostic_state, "interrupted", provider_turn_id)
                    await emit({"type": "interrupted"})
                if (getattr(content, "waiting_for_input", False) and diagnostic_state is not None
                        and not diagnostic_state.get("app_turn")
                        and diagnostic_state.get("input_turn", provider_turn_id) == provider_turn_id
                        and diagnostic_state.get("pending") and not diagnostic_state.get("collecting")
                        and not diagnostic_state.get("response_started")):
                    # The user already ENDED this turn (manual activity_end after the >=2 s floor), yet the model says
                    # it is waiting for more speech. Without handling, the turn would sit in "Generating" until a
                    # watchdog. Nudge exactly once per turn; a second wait ends the attempt → existing recovery.
                    await GeminiLiveTransport._resolve_waiting_for_input(session, diagnostic_state, provider_turn_id, emit)
                draft_tail_app = (diagnostic_state or {}).get("app_turn") or {}
                if (draft_tail_app.get("draft_tail") and not getattr(content, "interrupted", False)
                        and (getattr(content, "turn_complete", False) or getattr(content, "generation_complete", False))
                        and not (diagnostic_state or {}).get("response_started")):
                    if getattr(content, "turn_complete", False):
                        draft_tail_app.pop("draft_tail", None)       # the dropped draft is fully closed now
                    GeminiLiveTransport._turn_event(diagnostic_state, "app_turn_draft_dropped", provider_turn_id)
                    continue
                if getattr(content, "generation_complete", False):
                    if (diagnostic_state is not None and diagnostic_state.get("input_turn", provider_turn_id) == provider_turn_id
                            and diagnostic_state.get("pending") and not diagnostic_state.get("response_started")):
                        raise NativeLiveUnavailable("Live completed without output", "empty_response")
                    await emit({"type": "generation_complete"})
                if getattr(content, "turn_complete", False):
                    if (not getattr(content, "interrupted", False) and diagnostic_state is not None
                            and diagnostic_state.get("input_turn", provider_turn_id) == provider_turn_id
                            and diagnostic_state.get("pending") and not diagnostic_state.get("response_started")):
                        raise NativeLiveUnavailable("Live completed without output", "empty_response")
                    GeminiLiveTransport._turn_event(diagnostic_state, "turn_complete", provider_turn_id)
                    await emit({
                        "type": "turn_complete",
                        "generation_mode": "live_native",
                        "purpose": purpose,
                    })
                    if not getattr(content, "interrupted", False):
                        await GeminiLiveTransport._check_action_claim(session, diagnostic_state, provider_turn_id)
                elif getattr(content, "interrupted", False) and diagnostic_state is not None:
                    diagnostic_state.pop("said_text", None)
                if getattr(content, "turn_complete", False) or getattr(content, "interrupted", False):
                    completed_input_turn = None if getattr(content, "interrupted", False) else provider_turn_id
                    visual = (diagnostic_state or {}).get("visual")
                    if visual is not None:
                        with suppress(Exception):
                            visual.close_turn(provider_turn_id, related=[
                                t for t in GeminiLiveTransport._logical_input_turns(diagnostic_state, provider_turn_id)
                                if t != provider_turn_id])
                    if diagnostic_state is not None:
                        retired = diagnostic_state.setdefault("retired_input_turns", set())
                        retired.add(provider_turn_id)
                        retired.intersection_update(diagnostic_state.get("logical_turns", {}))
                        diagnostic_state["targets"].pop(provider_turn_id, None)
                        # The boundary guards only the typed turn right after the interrupted reply; once that turn
                        # (or any later one) closes, it is over.
                        if int(diagnostic_state.get("text_boundary_turn") or 0) and \
                                int(diagnostic_state["text_boundary_turn"]) <= provider_turn_id:
                            diagnostic_state.pop("text_boundary_turn", None)
                        if diagnostic_state.get("input_turn", provider_turn_id) == provider_turn_id:
                            diagnostic_state.update(pending=False, replay=[], collecting=False)
                    provider_turn_id += 1
                    generation_open = False
                    output_filter.reset()
                    if diagnostic_state is not None:
                        diagnostic_state["generation_active"] = False
                        app = diagnostic_state.get("app_turn")
                        if app and app.get("phase") == "answering" and app.get("turn") == provider_turn_id - 1:
                            diagnostic_state.pop("app_turn", None)      # the app-owned turn got its single answer
                    if diagnostic_state is not None:
                        diagnostic_state["turn"] = provider_turn_id
                        if getattr(content, "turn_complete", False):
                            await GeminiLiveTransport._speak_pending_update(session, diagnostic_state)
                    audio_sequence = 0
                    interrupted_boundary = bool(getattr(content, "interrupted", False))
            if not received:
                # A closed/empty stream is not a completed conversational turn.
                # Exit to the bridge recovery path instead of spinning forever.
                raise NativeLiveUnavailable("Live provider stream closed", "transport_closed")

    @staticmethod
    def _record_live_route(user_id: str, model_id: str, scope, credential_id: str, route_role: str, *,
                           success: bool, fallback_reason: str | None = None, skip_reason: str | None = None,
                           camera_live: bool = False) -> None:
        """Non-secret native Live observability (labels only; never keys, audio, frames or transcripts)."""
        try:
            from services.api_health_center import api_health_center
            api_health_center().record_route_event(
                user_id=user_id,
                task_type="LIVE_CAMERA" if camera_live else "LIVE_NATIVE",
                generation_mode="live_native",
                provider="google",
                api_family="gemini-live-bidi",
                exact_model_id=model_id,
                credential_label="user-google" if scope == CredentialScope.USER_GOOGLE else credential_id,
                route_role=route_role,
                success=success,
                fallback_reason=fallback_reason,
                native_live=True,
                camera_live=camera_live,
                skip_reason=skip_reason,
            )
        except Exception:
            pass

    async def bridge(
        self,
        websocket,
        *,
        user_id: str,
        purpose: str = "conversation",
        target_language: str = "",
        session_context: str = "",
        persist_diagnostics: bool = False,
        chat_id: str = "",
        visual_profile: dict | None = None,
        context_ms: float | None = None,
        recent_dialogue: list[dict] | None = None,
        ui_language: str = "",
        live_voice: str = "",
        call_opening: bool = False,
        time_zone: str = "",
    ) -> None:
        routes = [route for route in LiveVoiceRouter().routes(purpose) if route.active]
        if not routes:
            raise NativeLiveUnavailable("No eligible authenticated Gemini Live route is available.")
        last_category = "LIVE_ROUTES_FAILED"
        exhausted_quota_groups: set[tuple[str, str]] = set()
        auth_blocked_credentials: set[str] = set()
        diagnostic_state = {"enabled": persist_diagnostics, "chat_id": chat_id,
                            "user_id": user_id, "turn": 1, "targets": {}, "used_messages": set()}
        if purpose == "conversation":
            # OPTIONAL and LAZY: constructing this touches no provider, no Cloud Vision / Face++ client and no face
            # store. Detector/face work happens only inside an explicit visual request. A failure here only disables
            # on-demand marking; Live startup continues.
            try:
                from services.live_visual_grounding import LiveVisualGrounding

                def grounder(**kwargs):
                    from services.visual_labels import visual_label_service
                    return visual_label_service().ground(**kwargs)

                def references():
                    from services.visual_labels import enrolled_references_readonly
                    return enrolled_references_readonly(user_id)
                profile = visual_profile or {}

                def personal_intent(text):
                    # Same deterministic classifier as typed chat ("call me when done" stays a normal Live reply),
                    # plus questions about the user's own saved facts (looked up per turn, read-only).
                    from services.clock_context import is_time_question
                    if is_time_question(text):           # marker 220: the app answers from the live clock
                        return True
                    from services.personal_intents import TASK_NOTIFY, classify_personal_intent
                    intent = classify_personal_intent(text)
                    if intent.is_personal:
                        return intent.kind != TASK_NOTIFY
                    from router.api import live_memory_question
                    return live_memory_question(text, recent_user_texts(), user_id)

                def personal(text):
                    # Marker 174: remember / recall / forget / reminders spoken in Live are performed by the SAME
                    # personal assistant as typed chat, for this account and chat; temporary/test sessions never save.
                    from services.personal_assistant import PersonalAssistant
                    try:
                        from router.api import _find_registered_identity as find_user
                        from router.api import _find_registered_identities as find_users
                    except Exception:
                        find_user = find_users = None
                    from services.clock_context import is_time_question, time_answer, user_timezone
                    if is_time_question(text):
                        # Marker 220 (real 26 Oct: "12:52" said at 12:55): never a guess, never the session-start time.
                        return time_answer(text, user_timezone(user_id, time_zone))
                    from services.personal_intents import classify_personal_intent
                    if not classify_personal_intent(text).is_personal:
                        from router.api import live_memory_lookup_for
                        return live_memory_lookup_for(user_id, text, recent_user_texts())
                    from router.api import (place_urgent_call, request_contact_details, self_account_details,
                                            send_directed_message)
                    outcome = PersonalAssistant(user_id, chat_id=chat_id or None, persist=bool(persist_diagnostics),
                                                find_user=find_user,
                                                find_users=find_users,
                                                account_lookup=lambda: self_account_details(user_id),
                                                contact_request=lambda target, fields: request_contact_details(
                                                    user_id, target, fields),
                                                directed_send=lambda recipient, body, **kw: send_directed_message(
                                                    user_id, recipient, body, **kw),
                                                urgent_call=lambda recipient, body, **kw: place_urgent_call(
                                                    user_id, recipient, body, **kw)).handle(text)
                    if outcome.handled:
                        return outcome.reply
                    from router.api import live_memory_lookup        # e.g. "X's email" with no such Jupiter account
                    return live_memory_lookup(user_id, text)

                live_history: list[dict] = []
                session_dialogue = list(recent_dialogue or [])[-12:]

                def recent_user_texts() -> list[str]:
                    return [str(item.get("content") or "") for item in [*session_dialogue, *live_history]
                            if item.get("role") == "user" and str(item.get("content") or "").strip()][-4:]
                diagnostic_state["dialogue"] = lambda: [*session_dialogue, *live_history][-12:]
                diagnostic_state["ui_language"] = str(ui_language or "")
                diagnostic_state["multi_party"] = False
                with suppress(Exception):
                    from brain.conversation import temporary_speaker_possible
                    diagnostic_state["multi_party"] = temporary_speaker_possible(session_dialogue)

                def on_turn(user_text, assistant_text, turn):
                    # Marker 177: what the user says in Live feeds the SAME implicit memory learning as typed chat
                    # (identity-claim rules, guest/temporary-speaker guard, reminders/events), off the event loop.
                    # Temporary / test sessions and app-owned (already handled personal) turns never learn.
                    snapshot = [*session_dialogue, *live_history][-12:]   # a handoff before Live still guards
                    unclear = False
                    with suppress(Exception):
                        from services.live_language import unclear_fragment
                        unclear = unclear_fragment(user_text, [m.get("content", "") for m in snapshot
                                                               if m.get("role") == "user"],
                                                   str(ui_language or ""))
                    if unclear:
                        # Marker 179: a mis-heard fragment ("¿Qué?") is not a confirmed statement: it is never
                        # learned and never shifts later language/handoff decisions.
                        user_text = "[unclear speech]"
                    live_history.extend([{"role": "user", "content": user_text},
                                         {"role": "assistant", "content": assistant_text or ""}])
                    del live_history[:-24]
                    with suppress(Exception):
                        from brain.conversation import temporary_speaker_possible
                        diagnostic_state["multi_party"] = temporary_speaker_possible(
                            [*session_dialogue, *live_history])
                    if unclear or not persist_diagnostics or diagnostic_state.get("app_decided_turn") == turn:
                        return

                    earlier = [str(item.get("content") or "") for item in snapshot[:-2]
                               if item.get("role") == "user"][-4:]

                    def learn():
                        from router.api import _persist_normal_turn_extraction, live_turn_memory
                        _persist_normal_turn_extraction(user_id, user_text, snapshot, chat_id or None, None, None)
                        return live_turn_memory(user_id, user_text, earlier, chat_id or None)   # marker 221

                    async def memory_job():
                        receipt = await asyncio.get_running_loop().run_in_executor(None, learn)
                        if getattr(receipt, "saved", False):
                            # Marker 221: Live never says "saved" on its own; the app confirms a REAL save, once.
                            with suppress(Exception):
                                await websocket.send_json({"type": "memory_saved",
                                                           "text": GeminiLiveTransport.memory_saved_note(receipt.lines)})
                            GeminiLiveTransport._turn_event(diagnostic_state, "memory_saved", turn,
                                                            f"items={len(receipt.lines)}")
                        return receipt
                    with suppress(Exception):
                        jobs = diagnostic_state.setdefault("memory_jobs", {})
                        jobs[turn] = asyncio.get_running_loop().create_task(memory_job())
                        while len(jobs) > 8:
                            jobs.pop(next(iter(jobs)))

                diagnostic_state["visual"] = LiveVisualGrounding(
                    user_id=user_id, grounder=grounder, references=references,
                    owner_account=bool(profile.get("owner_account")),
                    profile_first_name=str(profile.get("profile_first_name") or ""),
                    telemetry=lambda event, detail: self._turn_event(diagnostic_state, event, detail=detail),
                    personal=personal, personal_intent=personal_intent, on_turn=on_turn)
            except Exception:
                diagnostic_state.pop("visual", None)
        reconnecting = False
        last_reason = "live_model_unavailable"
        connect_deadline = time.monotonic() + self.STARTUP_BUDGET_S  # inside the client's 45 s startup deadline
        # 17 Oct (real WT): a 15 s silence on gemini-3.8-live moved the conversation to gemini-3.1-flash-live, and the
        # user heard Jupiter's voice change mid-conversation. A silent turn is now replayed once on the SAME model in a
        # fresh session (same voice); a different model is used only when that model cannot connect or keeps failing.
        route_queue = list(routes)
        same_route_replayed: set[str] = set()
        while route_queue:
            route = route_queue.pop(0)
            credentials = self._credential_attempts(route.model_id)
            connection_attempts = 0
            user_failure_allows_shared = not any(
                scope == CredentialScope.USER_GOOGLE for scope, *_ in credentials
            )
            for scope, api_key, credential_id, quota_group in credentials:
                if not self._catalog_allows(route, scope, api_key):
                    continue  # Absence is scoped to this credential, never another user's catalog.
                quota_key = (quota_group, route.model_id)
                if credential_id in auth_blocked_credentials or quota_key in exhausted_quota_groups:
                    continue
                if scope == CredentialScope.JUPITER_SHARED_GOOGLE and not user_failure_allows_shared:
                    continue
                if connection_attempts >= 2:
                    break
                connect_timeout = min(self.PROVIDER_SETUP_TIMEOUT_S, connect_deadline - time.monotonic())
                if connect_timeout <= 0:
                    self._turn_event(diagnostic_state, "startup_timeout", 0, "startup_budget_exhausted")
                    raise NativeLiveUnavailable("Live connection budget exhausted", "connect_timeout")
                model = ProviderModel(
                    provider="google",
                    model_id=route.model_id,
                    display_name=route.model_id,
                    credential_scope=scope,
                    credential_owner_id=user_id if scope == CredentialScope.USER_GOOGLE else credential_id,
                    metadata={"google_quota_group": quota_group, "paid_usage_allowed": False},
                )
                governor = quota_governor()
                admission = governor.reserve(model, 100)
                if not admission.allowed:
                    last_category = "FREE_QUOTA_EXHAUSTED"
                    if scope == CredentialScope.USER_GOOGLE:
                        user_failure_allows_shared = True
                    else:
                        exhausted_quota_groups.add(quota_key)
                    continue
                connected = False
                connection_attempts += 1
                client = None
                tasks = set()
                try:
                    diagnostic_state["connection_id"] = secrets.token_hex(12)
                    startup_label = f'{route.model_id}|{"user-google" if scope == CredentialScope.USER_GOOGLE else credential_id}'
                    if context_ms is not None and not diagnostic_state.get("context_reported"):
                        # Server-side CONNECTING stages (A2): context assembly time, then candidate/setup/ready events.
                        diagnostic_state["context_reported"] = True
                        self._turn_event(diagnostic_state, "context_ready", 0, f"context_ms={int(context_ms)}")
                    self._turn_event(diagnostic_state, "provider_candidate_selected", 0, startup_label)
                    client_started = time.perf_counter()
                    client = await asyncio.to_thread(_live_client, api_key)   # reused per credential; off the loop
                    # Marker 183: the stage that held the first connection for up to 9.6 s, now measured directly.
                    self._turn_event(diagnostic_state, "provider_client_ready", 0,
                                     f"{startup_label}|client_ms={int((time.perf_counter() - client_started) * 1000)}")
                    async with AsyncExitStack() as stack:
                        try:
                            self._turn_event(diagnostic_state, "provider_setup_sent", 0, startup_label)
                            session = await asyncio.wait_for(stack.enter_async_context(client.aio.live.connect(
                                model=route.model_id,
                                config=self._config(purpose, target_language, session_context, route.model_id,
                                                    live_voice),
                            )), timeout=connect_timeout)
                        except asyncio.TimeoutError:
                            self._turn_event(diagnostic_state, "startup_timeout", 0, f"{startup_label}|phase=provider_setup")
                            raise NativeLiveUnavailable("Live connection timed out", "connect_timeout") from None
                        if hasattr(session, "setup_complete") and session.setup_complete is None:
                            # The SDK yields a session even when the first server message is NOT setupComplete. Such a
                            # session looks connected but never answers (real WT 22:05: no turn was ever accepted).
                            self._turn_event(diagnostic_state, "provider_setup_rejected", 0,
                                             f"{startup_label}|phase=setup_complete|reason=missing")
                            raise NativeLiveUnavailable("Live setup was not confirmed", "setup_rejected")
                        self._turn_event(diagnostic_state, "provider_setup_accepted", 0, startup_label)
                        connected = True
                        diagnostic_state["model_id"] = route.model_id          # server-side telemetry label only
                        route_role = "primary" if (route is routes[0] and connection_attempts == 1) else "fallback"
                        self._record_live_route(user_id, route.model_id, scope, credential_id, route_role,
                                                success=True, fallback_reason=None if route_role == "primary" else last_reason)
                        diagnostic_state["on_camera_live"] = (
                            lambda model=route.model_id, sc=scope, cid=credential_id, role=route_role:
                            self._record_live_route(user_id, model, sc, cid, role, success=True, camera_live=True))
                        if reconnecting:
                            # An app-owned turn belonged to the retired provider session; the replayed turn is
                            # decided again from its fresh transcript. Marker 215 (real WT 25 Oct: Spotify started,
                            # the answer timed out, the reconnected session said "I can't play Spotify"): a device
                            # action that already ran is NOT run again; the replay carries its real result.
                            prior_app = diagnostic_state.get("app_turn") or {}
                            carried = str(prior_app.get("result_note") or "")
                            self._cancel_app_turn(diagnostic_state, "reconnect")
                            if carried and prior_app.get("turn") == diagnostic_state.get("input_turn"):
                                diagnostic_state["carried_note"] = carried
                                diagnostic_state["app_decided_turn"] = prior_app.get("turn")
                            else:
                                diagnostic_state.pop("app_decided_turn", None)
                        if reconnecting and diagnostic_state.get("pending"):
                            try:
                                await asyncio.wait_for(self._replay_turn(session, diagnostic_state), 10)
                            except asyncio.TimeoutError:
                                raise NativeLiveUnavailable("Live input replay timed out", "transport_closed") from None
                        await websocket.send_json({
                            "type": "transport_recovered" if reconnecting else "session_ready",
                            "generation_mode": "live_native",
                            "purpose": purpose,
                            "input_audio": self.INPUT_MIME,
                            "output_audio": "audio/pcm;rate=24000",
                            "native_voice": normalize_live_voice(live_voice),
                            "turn_identity": "logical-v1",
                            "response_attempts": "attempt-v1",
                            "attempt_id": f'{diagnostic_state["connection_id"]}:{diagnostic_state.get("input_turn", 1)}',
                            "logical_turn_id": diagnostic_state.get("logical_turns", {}).get(diagnostic_state.get("input_turn"), ""),
                        })
                        self._turn_event(diagnostic_state, "transport_recovered" if reconnecting else "live_ready", 0,
                                         startup_label)
                        self._turn_event(diagnostic_state, "call_opening_check", 0,
                                         f"call={int(bool(call_opening))}:reconnect={int(bool(reconnecting))}")
                        if call_opening and not reconnecting and not diagnostic_state.get("call_opened"):
                            diagnostic_state["call_opened"] = True          # once per answered call, never on reconnect
                            # Marker 220 (auto-test WT 28 Oct: the call connected and Jupiter said nothing): a reply
                            # sent straight to Gemini here had no client turn, so the client dropped every packet as
                            # stale. The client sends this note as a hidden app update instead, which owns a turn.
                            try:
                                await websocket.send_json({"type": "call_opening", "text": self.CALL_OPENING_NOTE})
                                self._turn_event(diagnostic_state, "call_opening_sent", 0, startup_label)
                            except Exception as error:
                                self._turn_event(diagnostic_state, "call_opening_failed", 0, type(error).__name__)
                        sender = asyncio.create_task(self._send_client_audio(websocket, session, diagnostic_state))
                        receiver = asyncio.create_task(
                            self._send_provider_events(
                                websocket, session, route.model_id, purpose, user_id,
                                persist_diagnostics=persist_diagnostics,
                                diagnostic_state=diagnostic_state,
                            )
                        )
                        watchdog = asyncio.create_task(self._watch_response(diagnostic_state, session))
                        tasks = {sender, receiver, watchdog}
                        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in pending:
                            task.cancel()
                        for task in pending:
                            with suppress(asyncio.CancelledError):
                                await task
                        for task in done:
                            task.result()
                    governor.success(model, admission=admission)
                    recorder = current_request_context().credential_status_recorder
                    if recorder and scope == CredentialScope.USER_GOOGLE:
                        recorder(CredentialScope.USER_GOOGLE.value, "ACTIVE")
                    return
                except Exception as error:
                    last_category = VerifiedFreeTTSService._failure_category(error)
                    last_reason = getattr(error, "category", "live_model_unavailable")
                    if not connected and not isinstance(error, NativeLiveUnavailable):
                        # Provider rejected the setup/config (e.g. APIError 1007/1008): bounded, sanitized metadata.
                        from core.security import redact
                        code = getattr(error, "code", None) or getattr(error, "status_code", None) or ""
                        message = redact(str(getattr(error, "message", "") or error), 120).replace("|", "/")
                        self._turn_event(diagnostic_state, "provider_setup_rejected", 0,
                                         f"{route.model_id}|category={last_category}|code={code}|{message}")
                    if last_category in {"FREE_QUOTA_EXHAUSTED", "RATE_LIMIT"}:
                        last_reason = "quota_unavailable"
                    elif last_category == "AUTH_FAILED":
                        last_reason = "credential_unavailable"
                    elif last_reason == "live_model_unavailable":
                        last_reason = "provider_error" if connected else "native_primary_unavailable"
                    from core.provider_adapters import GoogleProvider
                    governor.failure(model, GoogleProvider(scope).normalize_error(error))
                    self._record_live_route(user_id, route.model_id, scope, credential_id,
                                            "fallback" if connected else "skipped", success=False,
                                            skip_reason=last_reason)
                    recorder = current_request_context().credential_status_recorder
                    if (
                        recorder
                        and scope == CredentialScope.USER_GOOGLE
                        and last_category == "AUTH_FAILED"
                    ):
                        recorder(CredentialScope.USER_GOOGLE.value, "AUTH_FAILED")
                    if connected:
                        reason = last_reason if last_reason != "live_model_unavailable" else "transport_closed"
                        can_replay = (diagnostic_state.get("pending")
                                      and diagnostic_state.get("replayable")
                                      and (diagnostic_state.get("replay")
                                           or diagnostic_state.get("text_turns", {}).get(diagnostic_state.get("input_turn")))
                                      and diagnostic_state.get("input_turn") == diagnostic_state["turn"]
                                      and not diagnostic_state.get("response_started"))
                        # Two different native models staying silent on the SAME replayed turn means the input is the
                        # cause (side conversation, unclear audio), not the model: try one more model at most, then
                        # hand over to the degraded path instead of keeping the user on "Thinking" for every route.
                        silent_again = (reason == "first_output_timeout"
                                        and diagnostic_state.get("silent_replay_turn") == diagnostic_state.get("input_turn"))
                        replay_same = (reason in {"first_output_timeout", "empty_response"}
                                       and route.model_id not in same_route_replayed)
                        if can_replay and (replay_same or route_queue) and not silent_again:
                            if reason == "first_output_timeout":
                                diagnostic_state["silent_replay_turn"] = diagnostic_state.get("input_turn")
                            if replay_same:
                                same_route_replayed.add(route.model_id)
                                route_queue.insert(0, route)
                            # Retry only an intact, endpointed turn with NO output.
                            # Never concatenate a second model onto half a reply.
                            reconnecting = True
                            connect_deadline = time.monotonic() + self.STARTUP_BUDGET_S
                            self._turn_event(diagnostic_state, "fallback_started", 0, reason)
                            await websocket.send_json({"type": "transport_recovering", "reason": reason,
                                "attempt_id": f'{diagnostic_state["connection_id"]}:{diagnostic_state.get("input_turn")}',
                                "logical_turn_id": diagnostic_state.get("logical_turns", {}).get(diagnostic_state.get("input_turn"), "")})
                            break  # Next compatible native route, not an endless key loop.
                        raise NativeLiveUnavailable("The active Live session ended", reason) from None
                    if scope == CredentialScope.USER_GOOGLE and last_category in {
                        "AUTH_FAILED", "FREE_QUOTA_EXHAUSTED", "RATE_LIMIT"
                    }:
                        user_failure_allows_shared = True
                        continue
                    if last_category == "AUTH_FAILED":
                        auth_blocked_credentials.add(credential_id)
                        continue
                    if last_category in {"FREE_QUOTA_EXHAUSTED", "RATE_LIMIT"}:
                        exhausted_quota_groups.add(quota_key)
                        continue
                    break
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    # The client is shared per credential (_live_client); its websocket session is closed by the
                    # exit stack above. It is only closed when evicted from the bounded cache.
        reason = "quota_unavailable" if last_category in {"FREE_QUOTA_EXHAUSTED", "RATE_LIMIT"} else last_reason
        raise NativeLiveUnavailable("No eligible Live route could connect", reason)
