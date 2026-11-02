"""Marker 218: Jupiter and the user never fight over the mouse.

While Jupiter performs ONE computer-control step (observe -> plan -> act -> verify) the user's PHYSICAL mouse movement
and clicks are held back, so the cursor cannot drift onto another window or invalidate what Jupiter just looked at.
Between steps the user has full control: any deliberate physical mouse movement (> 15 px), click or key press pauses
the task ("user override"), and Jupiter continues by itself once the user has been idle for 3.5 s.

Safety rules (each one is unit-tested through ``GuardPolicy``):
* the KEYBOARD is never blocked: every key always reaches Windows (Esc, Ctrl+Alt+Del, Alt+Tab, typing);
* Esc pressed by the user (not by Jupiter) stops computer control at once and releases the mouse;
* input injected by Jupiter itself (SendInput sets the INJECTED flag) always passes;
* one lock lasts at most ``MAX_LOCK_S`` (8 s): a hung planner call can never keep the mouse held;
* it uses low-level hooks (no BlockInput, no admin rights); Windows also drops a hook that stops responding, and
  closing the hook thread removes the hooks.
"""
from __future__ import annotations

import math
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

MAX_LOCK_S = 8.0
MOVE_THRESHOLD_PX = 15
IDLE_RESUME_S = 3.5
JUPITER_INPUT_TAG = 0x4A555049                      # "JUPI" in dwExtraInfo of Jupiter's own SendInput events
VK_ESCAPE = 0x1B
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_MOUSEMOVE = 0x0200
_MOUSE_BUTTONS = {0x0201, 0x0204, 0x0207, 0x020B, 0x020A, 0x020E}   # L/R/M/X down, wheel, h-wheel
LLMHF_INJECTED, LLKHF_INJECTED = 0x01, 0x10


@dataclass
class GuardPolicy:
    """Pure decisions of the hooks (no Win32): what to swallow, what counts as the user, when Esc aborts."""
    clock: Callable[[], float] = time.monotonic
    locked_at: float | None = None
    anchor: tuple[int, int] | None = None           # where the user's pointer was when we started watching
    last_user_input: float = field(default=0.0)
    pushed: float = 0.0                              # pointer movement the user attempted while it was held

    def lock(self) -> None:
        self.locked_at = self.clock()
        self.pushed = 0.0

    def unlock(self) -> None:
        self.locked_at = None
        self.anchor = None
        self.pushed = 0.0

    @property
    def locked(self) -> bool:
        if self.locked_at is not None and self.clock() - self.locked_at > MAX_LOCK_S:
            self.locked_at = None                    # failsafe: never longer than MAX_LOCK_S
        return self.locked_at is not None

    def mouse(self, message: int, point: tuple[int, int], injected: bool) -> tuple[bool, bool]:
        """(swallow, user_activity) for one low-level mouse event."""
        if injected:
            return False, False
        if self.locked:
            # Held back while Jupiter performs the step, but a deliberate attempt is remembered: the mouse is the
            # user's the moment this step ends (the guard reports it as user input during the lock).
            if message in _MOUSE_BUTTONS:
                return True, True
            if message == WM_MOUSEMOVE:
                if self.anchor is None:
                    self.anchor = point                  # the held cursor stays here; each event is one push
                else:
                    self.pushed += math.hypot(point[0] - self.anchor[0], point[1] - self.anchor[1])
                    if self.pushed > MOVE_THRESHOLD_PX:
                        return True, True
            return True, False
        if message in _MOUSE_BUTTONS:
            self.last_user_input = self.clock()
            return False, True
        if message == WM_MOUSEMOVE:
            if self.anchor is None:
                self.anchor = point
                return False, False
            if math.hypot(point[0] - self.anchor[0], point[1] - self.anchor[1]) > MOVE_THRESHOLD_PX:
                self.last_user_input = self.clock()
                self.anchor = point
                return False, True
        return False, False

    def key(self, message: int, vk: int, injected: bool) -> tuple[bool, str]:
        """(swallow, event) for one low-level key event: never swallowed; event is "escape", "user" or ""."""
        if injected or message not in (WM_KEYDOWN, WM_SYSKEYDOWN):
            return False, ""
        if vk == VK_ESCAPE:
            return False, "escape"
        self.last_user_input = self.clock()
        return False, "user"

    def idle_for(self) -> float:
        return self.clock() - self.last_user_input if self.last_user_input else float("inf")


class InputGuard:
    """Owns the low-level hooks (one thread with its own message loop) for one computer-control session."""

    def __init__(self, *, on_user_input: Callable[[], None], on_escape: Callable[[], None],
                 policy: GuardPolicy | None = None):
        self.policy = policy or GuardPolicy()
        self._on_user_input, self._on_escape = on_user_input, on_escape
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hooks: list[int] = []
        self._procs: list = []                      # keep the ctypes callbacks alive
        self._last_user_event = 0.0
        self._timer: threading.Timer | None = None
        self.user_input_during_lock = False

    # ── public ──────────────────────────────────────────────────────────────────────────────────────────────────
    def start(self) -> bool:
        if sys.platform != "win32" or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._run, name="jupiter-input-guard", daemon=True)
        self._thread.start()
        return self._ready.wait(2.0) and bool(self._hooks)

    def stop(self) -> None:
        self.unlock()
        if self._thread is not None and self._thread_id:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)     # WM_QUIT
            except Exception:
                pass
        self._thread = None

    def lock(self) -> None:
        self.user_input_during_lock = False
        self.policy.lock()
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(MAX_LOCK_S, self.unlock)          # failsafe even without any mouse event
        self._timer.daemon = True
        self._timer.start()

    def unlock(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.policy.unlock()

    @property
    def locked(self) -> bool:
        return self.policy.locked

    def idle_for(self) -> float:
        """Seconds since the user's last physical input (falls back to Windows' own last-input time)."""
        own = self.policy.idle_for()
        if own != float("inf"):
            return own
        return windows_idle_seconds()

    # ── hooks ───────────────────────────────────────────────────────────────────────────────────────────────────
    def _fire(self, callback: Callable[[], None]) -> None:
        threading.Thread(target=self._safe, args=(callback,), daemon=True).start()     # the hook returns at once

    @staticmethod
    def _safe(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            pass

    def _user_event(self) -> None:
        if self.policy.locked:
            self.user_input_during_lock = True
            return
        now = time.monotonic()
        if now - self._last_user_event >= 0.25:     # debounced: one call per burst
            self._last_user_event = now
            self._fire(self._on_user_input)

    def _run(self) -> None:                                              # pragma: no cover - needs a Windows desktop
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK

        def mouse_proc(code, wparam, lparam):
            if code == 0:
                try:
                    info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    injected = bool(info.flags & LLMHF_INJECTED) or info.dwExtraInfo == JUPITER_INPUT_TAG
                    swallow, user = self.policy.mouse(int(wparam), (info.pt.x, info.pt.y), injected)
                    if user:
                        self._user_event()
                    if swallow:
                        return 1
                except Exception:
                    pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def key_proc(code, wparam, lparam):
            if code == 0:
                try:
                    info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    injected = bool(info.flags & LLKHF_INJECTED) or info.dwExtraInfo == JUPITER_INPUT_TAG
                    _swallow, event = self.policy.key(int(wparam), int(info.vkCode), injected)
                    if event == "escape":
                        self.unlock()                   # the mouse is free within the same event
                        self._fire(self._on_escape)
                    elif event == "user":
                        self._user_event()
                except Exception:
                    pass
            return user32.CallNextHookEx(None, code, wparam, lparam)   # keys are NEVER swallowed

        self._procs = [HOOKPROC(mouse_proc), HOOKPROC(key_proc)]
        self._thread_id = kernel32.GetCurrentThreadId()
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE          # 64-bit handle (the default int truncates it)
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        module = kernel32.GetModuleHandleW(None)
        self._hooks = [h for h in (user32.SetWindowsHookExW(14, self._procs[0], module, 0),     # WH_MOUSE_LL
                                   user32.SetWindowsHookExW(13, self._procs[1], module, 0)) if h]   # WH_KEYBOARD_LL
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for hook in self._hooks:
                user32.UnhookWindowsHookEx(hook)
            self._hooks = []
            self.policy.unlock()


def windows_idle_seconds() -> float:                                    # pragma: no cover - needs Windows
    if sys.platform != "win32":
        return float("inf")
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return float("inf")
    return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)


def momentary_click(x: int, y: int, *, double: bool = False, hover_s: float = 0.35) -> bool:   # pragma: no cover
    """Marker 218: one REAL click at (x, y) (physical pixels) for UIs that ignore UI Automation (Spotify's search
    results): the user's mouse is held for this moment only, the pointer goes back where it was, and the events carry
    Jupiter's tag so the guard lets them through."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    guard = InputGuard(on_user_input=lambda: None, on_escape=lambda: None)
    guard.start()
    guard.lock()
    try:
        before = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(before))

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]      # 40 bytes on x64, as SendInput expects

        def send(flags: int) -> None:
            event = INPUT(0, MOUSEINPUT(0, 0, 0, flags, 0, JUPITER_INPUT_TAG))
            user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))

        user32.SetCursorPos(int(x), int(y))
        send(0x0001)                                           # a move, so the page's hover state is set
        time.sleep(hover_s)
        for _ in range(2 if double else 1):
            send(0x0002)                                       # left down
            send(0x0004)                                       # left up
            time.sleep(0.06)
        time.sleep(0.15)
        user32.SetCursorPos(before.x, before.y)
        return True
    except Exception:
        return False
    finally:
        guard.stop()
