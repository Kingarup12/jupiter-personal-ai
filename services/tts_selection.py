"""Ephemeral, single-use custom-TTS clarifications. No memory/database writes."""
from dataclasses import dataclass
import secrets
from threading import Lock
import time


@dataclass(frozen=True)
class PendingSpeech:
    question_id: str
    chat_id: str
    flow_id: str
    turn_id: str
    intent: object
    no_persist: bool
    test_session_id: str | None
    expires: float


class PendingSpeechStore:
    def __init__(self):
        self._lock = Lock()
        self._items = {}

    def create(self, owner, session, req, intent):
        with self._lock:
            now = time.monotonic()
            self._items = {key: value for key, value in self._items.items() if value.expires > now}
            if len(self._items) >= 256:
                self._items.pop(next(iter(self._items)))
            pending = PendingSpeech('tts-voice-' + secrets.token_hex(12), str(req.chatId or ''),
                                    req.ttsFlowId, str(req.messageId or ''), intent,
                                    req.noPersist, req.testSessionId, now + 900)
            self._items[(owner, session)] = pending
            return pending

    def get(self, owner, session, req, question_id, *, consume=False):
        with self._lock:
            pending = self._items.get((owner, session))
            if not pending or pending.expires <= time.monotonic():
                self._items.pop((owner, session), None)
                return None
            if (pending.question_id != question_id or pending.chat_id != str(req.chatId or '')
                    or pending.flow_id != req.ttsFlowId):
                return None
            if consume:
                self._items.pop((owner, session), None)
            return pending

    def cancel(self, owner, session, question_id=''):
        with self._lock:
            pending = self._items.get((owner, session))
            if pending and (not question_id or pending.question_id == question_id):
                self._items.pop((owner, session), None)


pending_speech = PendingSpeechStore()
