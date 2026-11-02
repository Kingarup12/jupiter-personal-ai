"""Desktop-host bridge for Windows laptop control (marker 159).

Mixed into the pywebview ``js_api`` object of the maintained Windows host (build_app.py). There is NO network
listener: the only caller is the trusted Jupiter page inside the host's own window, and every method first checks
that the window is still on the trusted origin the host itself navigated to. Explicit basic starts are quiet;
physical-input grants and consequential steps use a host-owned confirmation that the planner cannot accept. The backend only plans;
this bridge validates and executes.

Privacy boundary: the page receives only a redacted planner view (control ids/names/roles/flags, never field values,
rects or screenshots). A protected, human-verification or secret-bearing screen returns no view and no step token, so
the page has nothing to send to a planner; the server re-checks every view and fails closed as a second line.
"""

from __future__ import annotations

import json
import os
import ipaddress
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from services.computer_control import (TERMINAL_STATES, ConsentRequired, ControlScope, ControlSession, Operation,
                                       SessionState, ValidationError, planner_view)
from services.control_indicator import SHARING_STATE, ControlIndicator, indicator_state

BROWSER_PROCESSES = frozenset({"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"})
READ_ONLY_OPERATIONS = frozenset({Operation.OBSERVE, Operation.SCROLL, Operation.DONE, Operation.ASK_USER})


def safe_page_url(url: str) -> str:
    """Only public https pages may be opened for browser-first answers (no file:, javascript:, intranet or loopback)."""
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or len(str(url)) > 2048:
        return ""
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")) or "." not in host:
        return ""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return ""
    except ValueError:
        pass
    return parsed.geturl()

BLOCKED_MESSAGES = {
    "private_surface": "This window looks private (passwords, keys, banking or a vault). Control paused; nothing from "
                       "it was captured or shared.",
    "human_verification": "This screen needs you (sign-in, CAPTCHA or a Windows security prompt). Control paused.",
    "sensitive_content": "This screen shows something that looks like a key or a code. Control paused; nothing from "
                         "it was shared.",
}


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(str(url or ""))
    return parsed.scheme, (parsed.hostname or "").casefold(), parsed.port


class ComputerControlBridgeMixin:
    """js_api methods: computer_control_*. Requires ``self._trusted_url()`` and ``self._confirm(title, message)``."""

    _cc_backend_factory: Callable[[], Any] | None = None
    _cc_indicator_factory: Callable[..., Any] | None = None      # tests inject fake outline/orb factories
    _cc_open_url: Callable[[str], Any] | None = None              # tests inject; default: the user's default browser
    _cc_input_guard_factory: Callable[..., Any] | None = None     # marker 218: tests inject a fake guard
    CC_IDLE_RESUME_S = 3.5
    CC_RESUME_COUNTDOWN_S = 1.0

    def _cc_state(self) -> dict[str, Any]:
        if not hasattr(self, "_cc"):
            self._cc = {"session": None, "lock": threading.RLock(), "backend": None, "indicator": None,
                        "sharing": None}
        return self._cc

    # ── on-screen indicator (outline + orb), always tied to the session lifecycle ───────────────────────────────
    def _cc_new_indicator(self) -> ControlIndicator:
        factory = self._cc_indicator_factory or ControlIndicator
        return factory(on_pause=lambda: self._cc_publish_state(self.computer_control_pause()),
                       on_resume=lambda: self._cc_publish_state(self.computer_control_resume()),
                       on_foreground=lambda: self._cc_publish_state(self.computer_control_allow_foreground()),
                       on_stop=lambda: self._cc_publish_state(self.computer_control_stop("indicator_stop")))

    def _cc_publish_state(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Keep the owning page's loop in sync with the native orb buttons."""
        if self._cc_trusted():
            try:
                import webview
                if webview.windows:
                    webview.windows[0].evaluate_js(
                        "window.__jupiterComputerControlHostState&&window.__jupiterComputerControlHostState("
                        + json.dumps(snapshot) + ")")
            except Exception:
                pass
        return snapshot

    # ── Marker 218: cooperative mouse sharing ───────────────────────────────────────────────────────────────────
    def _cc_guard_obj(self):
        return self._cc_state().get("input_guard")

    def _cc_event(self, event: str) -> None:
        """Label-only telemetry (no window content)."""
        try:
            from services.api_health_center import api_health_center
            session = self._cc_session()
            api_health_center().record_live_turn_event(user_id=str(getattr(session, "owner", "") or ""),
                                                       connection_id="computer_control", provider_turn=0, event=event)
        except Exception:
            pass

    def _cc_start_input_guard(self) -> None:
        state = self._cc_state()
        factory = self._cc_input_guard_factory
        if factory is None and os.environ.get("JUPITER_INPUT_GUARD", "").strip() == "0":
            return                        # automated tests on the owner's live desktop never hold the real mouse
        if factory is None:
            from services.control_indicator import native_enabled
            if not native_enabled():
                return
            from services.input_guard import InputGuard as factory
        old = state.get("input_guard")
        if old is not None:
            old.stop()
        guard = factory(on_user_input=self._cc_on_user_input, on_escape=self._cc_on_escape)
        state["input_guard"] = guard
        try:
            guard.start()
        except Exception:
            state["input_guard"] = None

    def _cc_stop_input_guard(self) -> None:
        guard = self._cc_state().pop("input_guard", None)
        if guard is not None:
            try:
                guard.stop()
            except Exception:
                pass

    def _cc_lock_input(self) -> None:
        guard = self._cc_guard_obj()
        if guard is not None:
            guard.lock()
            self._cc_event("cu_mouse_locked")

    def _cc_unlock_input(self) -> bool:
        """Release; True when the user tried to use the mouse/keyboard during the step (they get it now)."""
        guard = self._cc_guard_obj()
        if guard is None:
            return False
        was_locked = guard.locked
        guard.unlock()
        if was_locked:
            self._cc_event("cu_mouse_released")
        return bool(getattr(guard, "user_input_during_lock", False))

    def _cc_on_user_input(self) -> None:
        session = self._cc_session()
        guard = self._cc_guard_obj()
        if session is None or guard is None or guard.locked or session.state != SessionState.ACTIVE:
            return
        self._cc_user_pause(session)

    def _cc_user_pause(self, session: ControlSession) -> None:
        """The user took the mouse: pause at this step (the goal and history stay) and resume when they are idle."""
        session.pause("user_active")
        self._cc_state()["resuming"] = False
        self._cc_event("cu_user_override_paused")
        self._cc_publish_state(self._cc_sync(session))
        state = self._cc_state()
        if state.get("idle_thread") is None or not state["idle_thread"].is_alive():
            state["idle_thread"] = threading.Thread(target=self._cc_idle_watch, args=(session,), daemon=True)
            state["idle_thread"].start()

    def _cc_idle_watch(self, session: ControlSession, *, sleep: Callable[[float], Any] = time.sleep) -> None:
        state = self._cc_state()
        while state.get("session") is session and session.state == SessionState.PAUSED \
                and session.state_reason == "user_active":
            guard = self._cc_guard_obj()
            idle = guard.idle_for() if guard is not None else float("inf")
            if idle >= self.CC_IDLE_RESUME_S:
                state["resuming"] = True                                 # "resuming in 1 s…" on the pill
                self._cc_publish_state(self._cc_sync(session))
                sleep(self.CC_RESUME_COUNTDOWN_S)
                guard = self._cc_guard_obj()
                still_idle = guard is None or guard.idle_for() >= self.CC_IDLE_RESUME_S + self.CC_RESUME_COUNTDOWN_S * 0.9
                state["resuming"] = False
                if still_idle and session.state == SessionState.PAUSED and session.state_reason == "user_active":
                    session.resume()
                    self._cc_event("cu_idle_resumed")
                    self._cc_publish_state(self._cc_sync(session))
                    return
                self._cc_publish_state(self._cc_sync(session))
            sleep(0.25)

    def _cc_on_escape(self) -> None:
        """Esc pressed by the user: stop now, free the mouse, remove the glow (Live says it stopped)."""
        session = self._cc_session()
        if session is None or session.state in {SessionState.COMPLETED, SessionState.STOPPED, SessionState.CANCELLED,
                                                SessionState.EXPIRED, SessionState.FAILED}:
            return
        self._cc_unlock_input()
        self._cc_event("cu_esc_aborted")
        self._cc_publish_state(self.computer_control_stop("escape"))

    def _cc_sync(self, session: ControlSession | None) -> dict[str, Any]:
        """Snapshot + indicator update; a terminal session removes the indicator and releases its resources."""
        snapshot = session.snapshot() if session else {"state": "IDLE"}
        if snapshot.get("state") == "PAUSED" and snapshot.get("reason") == "user_active":
            snapshot["resuming"] = bool(self._cc_state().get("resuming"))
        if session is None or snapshot.get("state") in {"COMPLETED", "STOPPED", "CANCELLED", "EXPIRED", "FAILED"}:
            self._cc_stop_input_guard()
        indicator = self._cc_state().get("indicator")
        if indicator is not None:
            indicator.update(snapshot)
            if not indicator.active:
                self._cc_state()["indicator"] = None
        return snapshot

    def _cc_show_controlling(self, session: ControlSession) -> None:
        indicator = self._cc_state().get("indicator")
        if indicator is not None:
            indicator.update({**session.snapshot(), "activity": "controlling"})

    def _cc_begin_indicator(self, session: ControlSession, handle: int) -> None:
        state = self._cc_state()
        if state.get("indicator") is not None:
            state["indicator"].end()
        indicator = self._cc_new_indicator()
        state["indicator"] = indicator
        try:
            session.backend.on_action = lambda _handle, point, kind: indicator.acted(point, kind)
        except Exception:
            pass
        indicator.show(handle, indicator_state(session.snapshot()))

    # Host hooks (overridden by build_app's bridge; overridable in tests).
    def _trusted_url(self) -> str:                                      # pragma: no cover - host provides
        return ""

    def _current_url(self) -> str:                                      # pragma: no cover - host provides
        return ""

    def _confirm(self, title: str, message: str) -> bool:              # pragma: no cover - host provides
        return False

    def _cc_trusted(self) -> bool:
        trusted, current = _origin(self._trusted_url()), _origin(self._current_url())
        return bool(trusted[1]) and trusted == current and trusted[0] in {"http", "https"}

    def _cc_backend(self):
        state = self._cc_state()
        if state["backend"] is None:
            factory = self._cc_backend_factory
            if factory is None:
                from services.windows_automation import WindowsBackend
                factory = WindowsBackend
            state["backend"] = factory()
        return state["backend"]

    def _cc_guard(self) -> dict[str, Any] | None:
        if not self._cc_trusted():
            return {"ok": False, "error": "untrusted_page"}
        return None

    # ── js_api surface ────────────────────────────────────────────────────────────────────────────────────────
    def computer_control_available(self) -> dict[str, Any]:
        import sys
        return {"ok": True, "available": sys.platform == "win32" and self._cc_trusted()}

    def computer_control_windows(self) -> dict[str, Any]:
        if (blocked := self._cc_guard()) is not None:
            return blocked
        return {"ok": True, "windows": self._cc_backend().list_windows()}

    def computer_control_start(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if (blocked := self._cc_guard()) is not None:
            return blocked
        options = options if isinstance(options, dict) else {}
        state = self._cc_state()
        with state["lock"]:
            current = state["session"]
            if current is not None and current.state not in {SessionState.COMPLETED, SessionState.STOPPED,
                                                             SessionState.CANCELLED, SessionState.EXPIRED,
                                                             SessionState.FAILED}:
                return {"ok": False, "error": "session_already_running"}
            backend = self._cc_backend()
            available = {int(w["handle"]): w for w in backend.list_windows()}
            handles = [int(h) for h in (options.get("handles") or []) if int(h) in available][:3]
            if not handles:
                return {"ok": False, "error": "choose_a_window"}
            minutes = max(1, min(30, int(options.get("minutes") or 10)))
            screenshots = False                    # v1 planner is accessibility-only: never capture unused pixels
            goal = str(options.get("goal") or "").strip()[:300]
            if not goal:
                return {"ok": False, "error": "describe_goal"}
            read_only = bool(options.get("read_only"))
            # The explicit chat/Live request or Start click is the basic background
            # instruction. No repetitive dialog. Sensitive steps and physical
            # input still require their separate action-time grant below.
            operations = READ_ONLY_OPERATIONS if read_only else frozenset({
                Operation.OBSERVE, Operation.FOCUS_WINDOW, Operation.CLICK, Operation.TYPE_TEXT, Operation.PRESS_KEY,
                Operation.SCROLL, Operation.DONE, Operation.ASK_USER})
            scope = ControlScope(window_handles=frozenset(handles), operations=operations,
                                 allow_screenshots=screenshots, max_steps=max(1, min(40, int(options.get("max_steps") or 20))),
                                 max_seconds=minutes * 60)
            session = ControlSession(owner=str(options.get("owner") or "")[:200], scope=scope, backend=backend)
            state["session"] = session
            self._cc_begin_indicator(session, handles[0])
            from services.control_indicator import native_enabled
            indicator = state.get("indicator")
            if native_enabled() and (indicator is None or indicator.orb is None
                                     or not getattr(indicator.outline, "alive", False)):
                session.stop("indicator_unavailable")
                return {"ok": False, "error": "indicator_unavailable", **self._cc_sync(session)}
            self._cc_start_input_guard()
            return {"ok": True, "read_only": read_only, **self._cc_sync(session)}

    def _cc_session(self) -> ControlSession | None:
        return self._cc_state()["session"]

    def computer_control_observe(self) -> dict[str, Any]:
        if (blocked := self._cc_guard()) is not None:
            return blocked
        session = self._cc_session()
        if session is None:
            return {"ok": False, "error": "no_session"}
        if session.state == SessionState.ACTIVE:
            self._cc_lock_input()            # marker 218: held from looking until this step's action is verified
        try:
            observation, token = session.observe()
        except ValidationError as error:
            self._cc_unlock_input()
            return {"ok": False, "error": str(error), **self._cc_sync(session)}
        except OSError:
            self._cc_unlock_input()
            session.stop("window_closed")
            return {"ok": False, "error": "The window was closed.", **self._cc_sync(session)}
        if observation.blocked_reason or not token:                    # minimal safe metadata only
            self._cc_unlock_input()
            reason = observation.blocked_reason or "blocked"
            return {"ok": False, "blocked": reason, "error": BLOCKED_MESSAGES.get(reason, "This screen needs you."),
                    **self._cc_sync(session)}
        return {"ok": True, "step_token": token,
                "observation": {"observation_id": observation.observation_id,
                                "window_handle": observation.window_handle},
                "planner_view": planner_view(observation), **self._cc_sync(session)}

    def computer_control_act(self, action: dict[str, Any], step_token: str) -> dict[str, Any]:
        if (blocked := self._cc_guard()) is not None:
            return blocked
        session = self._cc_session()
        if session is None:
            return {"ok": False, "error": "no_session"}
        if session.backend.secure_desktop_active():                    # locked / UAC: cancel, never act blindly
            session.stop("workstation_locked_or_secure_desktop")
            return {"ok": False, "error": "Windows is locked or showing a secure prompt; control stopped.",
                    **self._cc_sync(session)}
        try:
            result = self._cc_act(session, action, step_token)
        finally:
            took_over = self._cc_unlock_input()                         # marker 218: the step is over
        if took_over and session.state == SessionState.ACTIVE:
            self._cc_user_pause(session)                                # they tried to use the mouse: it is theirs
        result.update(self._cc_sync(session))
        if getattr(session.backend, "last_focus_side_effect", False):     # the APP raised itself; say so honestly
            session.backend.last_focus_side_effect = False
            result["focus_side_effect"] = True
        if session.state == SessionState.NEEDS_ATTENTION and session.state_reason == "needs_foreground":
            result.update(ok=False, needs_foreground=True,
                          error=session.snapshot().get("needs_foreground_message") or "This step needs your mouse.")
        elif session.state == SessionState.NEEDS_ATTENTION and session.state_reason == "user_busy":
            result.update(ok=False, user_busy=True,
                          error="You're using the computer, so Jupiter is waiting instead of taking the mouse.")
        return result

    def _cc_act(self, session: ControlSession, action: dict[str, Any], step_token: str) -> dict[str, Any]:
        self._cc_show_controlling(session)
        try:
            if session.observation_is_stale():                         # slow planner: act only if nothing changed
                step_token = session.revalidate_if_unchanged(step_token)
            record = session.act(action, step_token)
        except ConsentRequired as needed:
            # Action-time consent comes from the user in a native dialog, never from the model or page text. The
            # Yes covers exactly this step and target: it is re-validated on a FRESH observation, and Stop/Pause
            # pressed while the dialog was open wins (observe refuses a session that is no longer active).
            self._cc_unlock_input()                                     # the user needs the mouse for the dialog
            if not self._confirm("Confirm this step", f"{needed}\n\nStep: {needed.subject}\n\n"
                                                      "Allow Jupiter to do this one step now?"):
                with session._lock:
                    if session.state == SessionState.ACTIVE:
                        session.state, session.state_reason = SessionState.NEEDS_ATTENTION, "user_declined_step"
                return {"ok": False, "error": "You declined this step.", **session.snapshot()}
            try:
                observation, fresh = session.observe()
                if observation.blocked_reason or not fresh:
                    return {"ok": False, "blocked": observation.blocked_reason or "blocked",
                            "error": "The screen changed to one that needs you; nothing was done.", **session.snapshot()}
                record = session.act(action, fresh, consent={needed.grant})
            except ConsentRequired:
                return {"ok": False, "error": "The screen changed after you confirmed, so nothing was done. "
                                              "Jupiter will ask again for the new step.", **session.snapshot()}
            except ValidationError as again:
                return {"ok": False, "error": str(again), **session.snapshot()}
            except OSError:
                session.stop("window_closed")
                return {"ok": False, "error": "The window was closed.", **session.snapshot()}
        except ValidationError as error:
            return {"ok": False, "error": str(error), **session.snapshot()}
        verified = None
        if record.operation not in {"done", "ask_user"} and session.state == SessionState.ACTIVE:
            verified = session.verify(record, expect_change=record.operation not in {"scroll", "focus_window"})
        return {"ok": record.outcome == "ok", "outcome": record.outcome, "verified": verified, **session.snapshot()}

    def computer_control_pause(self) -> dict[str, Any]:
        session = self._cc_session()
        if session:
            session.pause()
        return {"ok": True, **self._cc_sync(session)}

    def computer_control_resume(self) -> dict[str, Any]:
        if (blocked := self._cc_guard()) is not None:
            return blocked
        session = self._cc_session()
        if session:
            if session.state_reason == "needs_foreground" and session.input_mode == "background":
                return {"ok": False, "error": "choose_foreground_or_stop", **self._cc_sync(session)}
            session.resume()
        return {"ok": True, **self._cc_sync(session)}

    def computer_control_cancel(self) -> dict[str, Any]:
        session = self._cc_session()
        if session:
            session.cancel()
        return {"ok": True, **self._cc_sync(session)}

    def computer_control_stop(self, reason: str = "user_stop") -> dict[str, Any]:
        """Immediate Stop — deliberately NOT origin-guarded: stopping must always work, from any state. Also removes
        every indicator (outline, orb) and the screen-sharing orb, releasing their windows and threads."""
        session = self._cc_session()
        if session:
            session.stop(str(reason or "user_stop")[:40])
        snapshot = self._cc_sync(session)
        state = self._cc_state()
        for key in ("indicator", "sharing"):
            if state.get(key) is not None:
                state[key].end()
                state[key] = None
        return {"ok": True, **snapshot}

    def computer_control_status(self) -> dict[str, Any]:
        session = self._cc_session()
        return {"ok": True, **self._cc_sync(session)}

    def computer_control_allow_foreground(self) -> dict[str, Any]:
        """The user's explicit, per-session grant to use the REAL pointer/keyboard (native dialog; never the model)."""
        if (blocked := self._cc_guard()) is not None:
            return blocked
        session = self._cc_session()
        if session is None or session.state in TERMINAL_STATES:
            return {"ok": False, "error": "no_session"}
        if not session.scope.operations - READ_ONLY_OPERATIONS:
            return {"ok": False, "error": "read_only_session", **self._cc_sync(session)}
        if not self._confirm("Let Jupiter use your mouse and keyboard?",
                             "Some steps in this window can only be done with your real mouse pointer and keyboard.\n\n"
                             "If you allow this, Jupiter will bring that window to the front for those steps and "
                             "your pointer will move. It still acts only in that window, still asks before "
                             "sending/deleting/buying, and waits while you are using the computer.\n\n"
                             "This lasts only for the current session."):
            return {"ok": False, "error": "declined", **self._cc_sync(session)}
        session.grant_foreground()
        return {"ok": True, **self._cc_sync(session)}

    # ── browser-first answers during computer use ────────────────────────────────────────────────────────────────
    def _cc_open_in_browser(self, url: str) -> bool:
        opener = self._cc_open_url
        if opener is None:                                             # pragma: no cover - needs a desktop
            import os
            os.startfile(url)                                          # the user's default browser, visibly
            return True
        return bool(opener(url))

    def _cc_find_browser_window(self, before: set[int], wait_seconds: float = 6.0) -> dict[str, Any] | None:
        backend = self._cc_backend()
        deadline = time.monotonic() + wait_seconds
        while True:
            browsers = [w for w in backend.list_windows() if str(w.get("process") or "") in BROWSER_PROCESSES]
            fresh = [w for w in browsers if int(w["handle"]) not in before]
            if fresh or (browsers and time.monotonic() >= deadline):
                return (fresh or browsers)[0]
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.4)

    def computer_control_open_page(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Open ONE explicitly requested public https page in a read-only session.

        Opening a page is not reading it: the answer must come from what the read-only session observes."""
        if (blocked := self._cc_guard()) is not None:
            return blocked
        options = options if isinstance(options, dict) else {}
        url = safe_page_url(str(options.get("url") or ""))
        if not url:
            return {"ok": False, "error": "unsafe_url"}
        before = {int(w["handle"]) for w in self._cc_backend().list_windows()
                  if str(w.get("process") or "") in BROWSER_PROCESSES}
        if not self._cc_open_in_browser(url):
            return {"ok": False, "error": "browser_unavailable"}
        window = self._cc_find_browser_window(before, float(options.get("wait_seconds", 6.0)))
        if window is None:
            return {"ok": True, "opened": url, "read": False, "error": "browser_window_not_found"}
        started = self.computer_control_start({"handles": [int(window["handle"])], "read_only": True,
                                               "goal": str(options.get("question") or "")[:300], "minutes": 5,
                                               "max_steps": 8, "owner": str(options.get("owner") or "")})
        return {**started, "opened": url, "read": bool(started.get("ok")), "window_title": str(window.get("title") or "")[:120]}

    # ── Live screen sharing: observing-only indicator (no outline: the WebView picker chooses the surface) ──────
    def computer_control_screen_sharing(self, active: bool = False) -> dict[str, Any]:
        state = self._cc_state()
        if not active:
            if state.get("sharing") is not None:
                state["sharing"].end()
                state["sharing"] = None
            return {"ok": True, "sharing": False}
        if (blocked := self._cc_guard()) is not None:
            return blocked
        if state.get("sharing") is None:
            factory = self._cc_indicator_factory or ControlIndicator
            indicator = factory(on_pause=lambda: None, on_resume=lambda: None,
                                on_stop=lambda: self._cc_request_stop_sharing())
            indicator.show(None, SHARING_STATE)
            state["sharing"] = indicator
        return {"ok": True, "sharing": True}

    def _cc_request_stop_sharing(self) -> None:                         # pragma: no cover - host provides the page
        try:
            import webview
            if webview.windows:
                webview.windows[0].evaluate_js("window.__jupiterStopScreenShare&&window.__jupiterStopScreenShare()")
        except Exception:
            pass
        self.computer_control_screen_sharing(False)
