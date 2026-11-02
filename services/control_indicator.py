"""On-screen indicator for an authorised computer-control session (Windows host).

Two pieces, both owned by the Jupiter host process (so the planner can never click them — Jupiter's own windows are
excluded from control):

* ``OutlineOverlay``  a click-through, non-activating, topmost layered Win32 window drawn along every desktop edge.
                      It remains visible for a minimised target, ends when the target closes, and briefly
                      marks the spot where Jupiter acted. That marker is an honest "Jupiter acted here" ring — Windows
                      has a single system pointer, so Jupiter does not fake a second mouse; background actions (UI
                      Automation) do not move the user's pointer at all.
* ``OrbIndicator``    a compact floating Jupiter orb (pywebview window, ``focus=False`` => WS_EX_NOACTIVATE) with the
                      current state and accessible Pause/Resume and Stop buttons. It never takes keyboard focus from
                      the user's work and is not a screen-obscuring banner.

``ControlIndicator`` ties them to the session lifecycle: ``show`` on Start, ``update`` on every snapshot, ``acted``
on every executed step, ``end`` on any terminal state or host shutdown (idempotent; releases windows, timers and the
message-loop thread). Observing (reading the window / screen sharing) and controlling (executing a step) are drawn
differently and labelled differently.
"""

from __future__ import annotations

import ctypes
import html as html_lib
import json
import math
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable

TERMINAL = frozenset({"COMPLETED", "STOPPED", "CANCELLED", "EXPIRED", "FAILED", "IDLE"})

# Native indicator windows are created ONLY inside the Windows desktop host, which calls enable_native(True) at
# startup. Everywhere else (backend, tests, scripts) the default factories create nothing.
_NATIVE = {"enabled": False}


def enable_native(enabled: bool = True) -> None:
    _NATIVE["enabled"] = bool(enabled) and sys.platform == "win32"


def native_enabled() -> bool:
    return bool(_NATIVE["enabled"])


_WIN32: dict[str, Any] = {}


def _win32() -> dict[str, Any]:
    """A PRIVATE, fully typed user32/gdi32 binding (64-bit handles are never truncated; other modules' shared
    ctypes.windll argtypes are never touched)."""
    if _WIN32:
        return _WIN32
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    H, U, W, L = wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    LRESULT = ctypes.c_ssize_t
    sig = {
        (user32, "DefWindowProcW"): ([H, U, W, L], LRESULT),
        (user32, "PostMessageW"): ([H, U, W, L], wintypes.BOOL),
        (user32, "InvalidateRect"): ([H, ctypes.c_void_p, wintypes.BOOL], wintypes.BOOL),
        (user32, "BeginPaint"): ([H, ctypes.c_void_p], wintypes.HDC),
        (user32, "EndPaint"): ([H, ctypes.c_void_p], wintypes.BOOL),
        (user32, "GetClientRect"): ([H, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        (user32, "GetWindowRect"): ([H, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        (user32, "FillRect"): ([wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH], ctypes.c_int),
        (user32, "IsWindow"): ([H], wintypes.BOOL),
        (user32, "IsIconic"): ([H], wintypes.BOOL),
        (user32, "IsWindowVisible"): ([H], wintypes.BOOL),
        (user32, "ShowWindow"): ([H, ctypes.c_int], wintypes.BOOL),
        (user32, "GetWindow"): ([H, U], H),
        (user32, "SetWindowPos"): ([H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, U], wintypes.BOOL),
        (user32, "SetTimer"): ([H, ctypes.c_size_t, U, ctypes.c_void_p], ctypes.c_size_t),
        (user32, "KillTimer"): ([H, ctypes.c_size_t], wintypes.BOOL),
        (user32, "PostQuitMessage"): ([ctypes.c_int], None),
        (user32, "RegisterClassW"): ([ctypes.c_void_p], wintypes.ATOM),
        (user32, "UnregisterClassW"): ([wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
        (user32, "CreateWindowExW"): ([wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, wintypes.HMENU,
                                       wintypes.HINSTANCE, ctypes.c_void_p], H),
        (user32, "SetLayeredWindowAttributes"): ([H, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD], wintypes.BOOL),
        (user32, "GetMessageW"): ([ctypes.POINTER(wintypes.MSG), H, U, U], wintypes.BOOL),
        (user32, "TranslateMessage"): ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
        (user32, "DispatchMessageW"): ([ctypes.POINTER(wintypes.MSG)], LRESULT),
        (gdi32, "CreateSolidBrush"): ([wintypes.COLORREF], wintypes.HBRUSH),
        (gdi32, "CreatePen"): ([ctypes.c_int, ctypes.c_int, wintypes.COLORREF], wintypes.HPEN),
        (gdi32, "SelectObject"): ([wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
        (gdi32, "GetStockObject"): ([ctypes.c_int], wintypes.HGDIOBJ),
        (gdi32, "DeleteObject"): ([wintypes.HGDIOBJ], wintypes.BOOL),
        (gdi32, "Ellipse"): ([wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int], wintypes.BOOL),
        (kernel32, "GetModuleHandleW"): ([wintypes.LPCWSTR], wintypes.HMODULE),
        (dwmapi, "DwmGetWindowAttribute"): ([H, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD], ctypes.c_long),
    }
    for (library, name), (argtypes, restype) in sig.items():
        function = getattr(library, name)
        function.argtypes, function.restype = argtypes, restype
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte),
                    ("AlphaFormat", ctypes.c_ubyte)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    more = {
        (user32, "GetDC"): ([H], wintypes.HDC),
        (user32, "ReleaseDC"): ([H, wintypes.HDC], ctypes.c_int),
        (user32, "DestroyWindow"): ([H], wintypes.BOOL),
        (user32, "SystemParametersInfoW"): ([U, U, ctypes.c_void_p, U], wintypes.BOOL),
        (user32, "UpdateLayeredWindow"): ([H, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p, wintypes.HDC,
                                           ctypes.c_void_p, wintypes.COLORREF, ctypes.c_void_p, wintypes.DWORD],
                                          wintypes.BOOL),
        (gdi32, "CreateCompatibleDC"): ([wintypes.HDC], wintypes.HDC),
        (gdi32, "DeleteDC"): ([wintypes.HDC], wintypes.BOOL),
        (gdi32, "CreateDIBSection"): ([wintypes.HDC, ctypes.c_void_p, U, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD], wintypes.HBITMAP),
    }
    for (library, name), (argtypes, restype) in more.items():
        function = getattr(library, name)
        function.argtypes, function.restype = argtypes, restype
    _WIN32.update(user32=user32, gdi32=gdi32, kernel32=kernel32, dwmapi=dwmapi, LRESULT=LRESULT,
                  BITMAPINFOHEADER=BITMAPINFOHEADER, BITMAPINFO=BITMAPINFO, BLENDFUNCTION=BLENDFUNCTION, SIZE=SIZE)
    return _WIN32


@dataclass(frozen=True)
class IndicatorState:
    visible: bool
    mode: str            # observing | controlling | paused | attention | sharing
    label: str
    input_mode: str = "background"
    can_foreground: bool = False


def indicator_state(snapshot: dict[str, Any] | None) -> IndicatorState:
    """Pure mapping from a session snapshot to what the indicator shows (unit-tested)."""
    snap = snapshot or {}
    state = str(snap.get("state") or "IDLE")
    if not snap.get("session_id") or state in TERMINAL:
        return IndicatorState(False, "hidden", "")
    input_mode = str(snap.get("input_mode") or "background")
    # Marker 218 (user, 26 Oct: the pill said "Jupiter needs you"; it should say "Jupiter is using your computer"):
    # one headline for every active state, the detail after it.
    head = "Jupiter is using your computer"
    if state == "PAUSED" and snap.get("reason") == "user_active":
        if snap.get("resuming"):
            return IndicatorState(True, "yield", f"{head} · resuming in 1 s…", input_mode)
        return IndicatorState(True, "yield", f"{head} · paused while you use the mouse", input_mode)
    if state == "PAUSED":
        return IndicatorState(True, "paused", f"{head} · paused", input_mode)
    if state == "NEEDS_ATTENTION":
        if snap.get("reason") == "needs_foreground":
            return IndicatorState(True, "attention", f"{head} · needs your OK for the mouse", input_mode, True)
        if snap.get("reason") == "user_busy":
            return IndicatorState(True, "attention", f"{head} · waiting while you work", input_mode)
        return IndicatorState(True, "attention", f"{head} · needs you", input_mode)
    if snap.get("activity") == "controlling":
        return IndicatorState(True, "controlling", f"{head} · Esc to stop", input_mode)
    return IndicatorState(True, "observing", f"{head} · Esc to stop", input_mode)


SHARING_STATE = IndicatorState(True, "sharing", "Screen shared with Jupiter (observing only)")


def desktop_frame_geometry(user32) -> tuple[tuple[int, int, int, int], list[tuple[int, int, int, int]]]:
    """Actual monitor bounds, including negative origins; no assumed screen size."""
    monitors = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def collect(_monitor, _dc, rect, _data):
        r = rect.contents
        monitors.append((r.left, r.top, r.right, r.bottom))
        return True

    callback = callback_type(collect)
    user32.EnumDisplayMonitors(None, None, callback, 0)
    if not monitors:
        x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        monitors = [(x, y, x + max(1, user32.GetSystemMetrics(78)), y + max(1, user32.GetSystemMetrics(79)))]
    bounds = (min(r[0] for r in monitors), min(r[1] for r in monitors),
              max(r[2] for r in monitors), max(r[3] for r in monitors))
    return bounds, monitors

# Outline colours (COLORREF is 0x00BBGGRR). Observing: thin, dim. Controlling: thicker, Jupiter blue (#4FACFE).
_KEY = 0x00FF00FF                     # magenta colour key => fully transparent
_STYLE = {"observing": (2, 0x00C88A5A), "controlling": (3, 0x00FEAC4F), "paused": (2, 0x00A0A0A0),
          "attention": (3, 0x00FEAC4F), "sharing": (2, 0x00C88A5A)}     # marker 212: blue, never orange


class OutlineOverlay:
    """Desktop-frame neon edge glow (all monitor edges; Codex 165 design) + the Jupiter action cursor
    (prototype/cursor_demo design).

    Two per-pixel-alpha layered windows (WS_EX_LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE): always click-through,
    never activated, never in the taskbar, topmost. The frame covers every actual monitor (not target-sized) and stays
    visible while the target is minimised; the cursor marks where Jupiter acted and hides while the target is
    minimised. It is an honest "Jupiter acted here" marker; it never moves the user's real pointer.
    Windows "animation effects off" (SPI_GETCLIENTAREAANIMATION) => no glide, no particles, no breathing.
    """

    FRAME_MS = 33
    LABEL_SECONDS = 1.4

    def __init__(self, target_handle: int):
        from services.control_overlay_art import CursorAnimator
        self.target = int(target_handle)
        self.mode = "observing"
        self._label = ""
        self._label_until = 0.0
        self._animator = CursorAnimator(0.0, 0.0)
        self._placed = False
        self._lock = threading.Lock()
        self._hwnd = 0                 # glow window (owns the message loop)
        self._cursor_hwnd = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False
        self._glow_key = None
        self.frames_pushed = 0

    # public API (thread-safe) ---------------------------------------------------------------------------------
    def start(self) -> "OutlineOverlay":
        if sys.platform != "win32" or self._thread or not native_enabled():
            return self
        self._thread = threading.Thread(target=self._run, name="jupiter-control-overlay", daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return self

    def set_mode(self, mode: str) -> None:
        from services.control_overlay_art import MODES
        self.mode = mode if mode in MODES else "observing"

    def pulse(self, point: tuple[int, int] | None, kind: str = "click") -> None:
        from services.control_overlay_art import ACTION_LABELS
        now = time.monotonic()
        with self._lock:
            self._label, self._label_until = ACTION_LABELS.get(kind, ACTION_LABELS["click"]), now + self.LABEL_SECONDS
            if point:
                if not self._placed:
                    self._animator.x, self._animator.y, self._placed = float(point[0]), float(point[1]), True
                self._animator.move_to(point[0], point[1], now)

    def close(self) -> None:
        self._closed = True
        if self._hwnd and sys.platform == "win32":
            _win32()["user32"].PostMessageW(self._hwnd, 0x0010, 0, 0)               # WM_CLOSE
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3.0)
        self._thread = None
        self._hwnd = self._cursor_hwnd = 0

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # overlay thread --------------------------------------------------------------------------------------------
    def _run(self) -> None:                                            # pragma: no cover - needs a Windows desktop
        from services import control_overlay_art as art
        from PIL import Image
        win = _win32()
        user32, gdi32, kernel32, dwmapi = win["user32"], win["gdi32"], win["kernel32"], win["dwmapi"]
        LRESULT = win["LRESULT"]
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        animation = wintypes.BOOL(1)
        user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(animation), 0)          # SPI_GETCLIENTAREAANIMATION
        reduced = not bool(animation.value)
        self._animator.reduced_motion = reduced
        overlay = self

        def push(hwnd: int, image, x: int, y: int, alpha: int = 255) -> None:
            width, height = image.size
            data = image.convert("RGBa").tobytes("raw", "BGRa")                   # premultiplied BGRA
            screen = user32.GetDC(None)
            memory = gdi32.CreateCompatibleDC(screen)
            info = win["BITMAPINFO"]()
            info.bmiHeader.biSize = ctypes.sizeof(win["BITMAPINFOHEADER"])
            info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height
            info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
            bits = ctypes.c_void_p()
            bitmap = gdi32.CreateDIBSection(memory, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
            try:
                if bitmap and bits.value:
                    ctypes.memmove(bits, data, len(data))
                    previous = gdi32.SelectObject(memory, bitmap)
                    blend = win["BLENDFUNCTION"](0, 0, alpha, 1)                     # AC_SRC_OVER, AC_SRC_ALPHA
                    user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wintypes.POINT(x, y)),
                                               ctypes.byref(win["SIZE"](width, height)), memory,
                                               ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), 2)
                    gdi32.SelectObject(memory, previous)
                    overlay.frames_pushed += 1
            finally:
                if bitmap:
                    gdi32.DeleteObject(bitmap)
                gdi32.DeleteDC(memory)
                user32.ReleaseDC(None, screen)

        def set_alpha(hwnd: int, alpha: int) -> None:
            blend = win["BLENDFUNCTION"](0, 0, max(0, min(255, alpha)), 1)
            user32.UpdateLayeredWindow(hwnd, None, None, None, None, None, 0, ctypes.byref(blend), 2)

        SWP_NOACTIVATE, SWP_NOSIZE, SWP_SHOWWINDOW, SWP_HIDEWINDOW = 0x0010, 0x0001, 0x0040, 0x0080
        geometry = {"checked": 0.0, "value": None}

        def frame_geometry(now: float):
            # Desktop frame (Codex 165 design): every actual monitor edge, refreshed about once a second.
            if geometry["value"] is None or now - geometry["checked"] > 1.0:
                bounds, monitors = desktop_frame_geometry(user32)
                geometry["value"], geometry["checked"] = (tuple(bounds), tuple(tuple(m) for m in monitors)), now
            return geometry["value"]

        def topmost(hwnd: int, x: int, y: int, show: bool = True) -> None:
            user32.SetWindowPos(hwnd, -1, x, y, 0, 0, SWP_NOACTIVATE | SWP_NOSIZE |
                                (SWP_SHOWWINDOW if show else SWP_HIDEWINDOW))      # HWND_TOPMOST, never activated

        def tick(hwnd: int) -> None:
            target = overlay.target
            if overlay._closed or not user32.IsWindow(target):
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
                return
            now = time.monotonic()
            mode = overlay.mode
            bounds, monitors = frame_geometry(now)
            key = (bounds, monitors, mode)
            if overlay._glow_key != key:                                             # the frame is NOT target-sized
                push(overlay._hwnd, art.render_screen_glow(bounds, monitors, mode), bounds[0], bounds[1])
                overlay._glow_key = key
            topmost(overlay._hwnd, bounds[0], bounds[1])                             # stays up while target minimised
            if not reduced and mode in {"observing", "controlling", "attention", "sharing", "yield"}:
                period, low, high = (1.6, 170, 255) if mode == "controlling" else (3.2, 150, 230)
                set_alpha(overlay._hwnd, int(low + (high - low) * (0.5 + 0.5 * math.sin(now * 2 * math.pi / period))))
            # cursor: only where Jupiter can act; hidden while the target is minimised ------------------------------
            if user32.IsIconic(target) or not user32.IsWindowVisible(target):
                user32.ShowWindow(overlay._cursor_hwnd, 0)
                return
            with overlay._lock:
                if not overlay._placed:
                    rect = wintypes.RECT()
                    user32.GetWindowRect(target, ctypes.byref(rect))
                    overlay._animator.x = (rect.left + rect.right) / 2
                    overlay._animator.y = (rect.top + rect.bottom) / 2
                    overlay._placed = True
                overlay._animator.step(now)
                cx, cy = overlay._animator.x, overlay._animator.y
                effects_origin = (int(cx) - art.HOTSPOT[0], int(cy) - art.HOTSPOT[1])
                effects = art.render_effects(overlay._animator, effects_origin, now) if overlay._animator.busy else None
            _edge, _glow, default_label, dot = art.MODES[mode]
            label = overlay._label if now < overlay._label_until else default_label
            if label != default_label:
                dot = art.CYAN                                       # marker 212: blue action badges
            moon = 0.0 if reduced else (now * 2 * math.pi / 2.4) % (2 * math.pi)
            press = 1.0 if any(now - wave.started < 0.12 for wave in overlay._animator.waves) else 0.0
            image = art.render_cursor(label, dot, moon_angle=moon, press=press, dim=mode == "paused")
            if effects is not None:
                image = Image.alpha_composite(effects, image)
            push(overlay._cursor_hwnd, image, *effects_origin)
            topmost(overlay._cursor_hwnd, *effects_origin)

        def wndproc(hwnd, message, wparam, lparam):
            if message == 0x0113 and hwnd == overlay._hwnd:                          # WM_TIMER
                try:
                    tick(hwnd)
                except Exception:
                    pass                                                              # the indicator never breaks control
                return 0
            if message == 0x0084:                                                    # WM_NCHITTEST
                return -1                                                            # HTTRANSPARENT
            if message == 0x0021:                                                    # WM_MOUSEACTIVATE
                return 3                                                             # MA_NOACTIVATE
            if message == 0x0002 and hwnd == overlay._hwnd:                          # WM_DESTROY (glow owns loop)
                user32.KillTimer(hwnd, 1)
                if overlay._cursor_hwnd:
                    user32.DestroyWindow(overlay._cursor_hwnd)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._proc = WNDPROC(wndproc)
        instance = kernel32.GetModuleHandleW(None)
        class_name = f"JupiterControlOverlay{id(self)}"
        user32.RegisterClassW(ctypes.byref(WNDCLASSW(0, self._proc, 0, 0, instance, None, None, None, None, class_name)))
        ex_style = 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000   # LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE
        windows = []
        for title in ("Jupiter control glow", "Jupiter action cursor"):
            windows.append(user32.CreateWindowExW(ex_style, class_name, title, 0x80000000, 0, 0, 1, 1,
                                                  None, None, instance, None))
        self._hwnd, self._cursor_hwnd = (int(h or 0) for h in windows)
        if not (self._hwnd and self._cursor_hwnd):
            self._ready.set()
            return
        try:                                                          # warm the caches before the first frame
            for warm in ("observing", "controlling"):
                art.render_cursor(art.MODES[warm][2], art.MODES[warm][3])
            art.render_cursor(art.ACTION_LABELS["click"], art.AMBER, press=1.0)
        except Exception:
            pass
        user32.SetTimer(self._hwnd, 1, self.FRAME_MS, None)
        try:
            tick(self._hwnd)
        except Exception:
            pass
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnregisterClassW(class_name, instance)
        self._hwnd = self._cursor_hwnd = 0


ORB_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Jupiter control</title><style>
:root{color-scheme:dark}html,body{margin:0;height:100%;background:#0f111a;font:13px/1.2 system-ui,'Segoe UI',sans-serif;
overflow:hidden;user-select:none}.pill{display:flex;align-items:center;gap:8px;height:100%;box-sizing:border-box;
padding:0 8px 0 6px;border-radius:999px;background:#0f111a;border:1px solid rgba(79,172,254,.45);color:#e6edf7}.orb{width:28px;height:28px;border-radius:50%;flex:none;display:grid;
place-items:center;color:#4facfe;background:rgba(79,172,254,.10);box-shadow:0 0 12px rgba(79,172,254,.35);
animation:float 3.2s ease-in-out infinite}.pill[data-mode=controlling] .orb{box-shadow:0 0 16px rgba(79,172,254,.8)}
.pill[data-mode=paused] .orb{color:#a0a0a0;box-shadow:none;animation:none}.pill[data-mode=attention] .orb{color:#7cc4ff;
box-shadow:0 0 14px rgba(79,172,254,.75)}.pill[data-mode=yield]{border-color:rgba(255,176,46,.6)}
.pill[data-mode=yield] .orb{color:#ffb02e;box-shadow:0 0 14px rgba(255,176,46,.7)}#label{flex:1;min-width:0;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}button{font:inherit;color:#e6edf7;background:rgba(255,255,255,.08);border:1px solid
rgba(255,255,255,.18);border-radius:999px;padding:4px 10px;cursor:pointer}button:focus-visible{outline:2px solid
#4facfe;outline-offset:1px}#stop{background:rgba(229,72,77,.22);border-color:rgba(229,72,77,.6)}[hidden]{display:none}
@keyframes float{50%{transform:translateY(-1.5px)}}@media (prefers-reduced-motion:reduce){.orb{animation:none}}
</style></head><body><div class="pill" id="pill" data-mode="observing" role="status" aria-live="polite">
<span class="orb" aria-hidden="true"><svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
<circle cx="12" cy="12" r="6"/><ellipse cx="12" cy="12" rx="11" ry="3.6" fill="none" stroke="currentColor"
stroke-width="1.6" transform="rotate(-22 12 12)"/></svg></span>
<span id="label" title="Click to hide (Esc still stops Jupiter)">__LABEL__</span>
<button id="pause" type="button" aria-label="Pause Jupiter control">Pause</button>
<button id="resume" type="button" aria-label="Resume Jupiter control" hidden>Resume</button>
<button id="foreground" type="button" aria-label="Choose foreground computer use" hidden>Show</button>
<button id="stop" type="button" aria-label="Stop Jupiter control now" title="Stop Jupiter control">×</button></div><script>
const api=()=>window.pywebview&&window.pywebview.api;const $=(i)=>document.getElementById(i);
window.setJupiterIndicator=(s)=>{$('pill').dataset.mode=s.mode;$('label').textContent=s.label;
const sharing=s.mode==='sharing';$('pause').hidden=sharing||s.mode==='paused'||s.mode==='attention';
$('resume').hidden=sharing||!(s.mode==='paused');$('pause').hidden=$('pause').hidden||s.mode==='yield';$('stop').setAttribute('aria-label',sharing?'Stop screen sharing':
'Stop Jupiter control now');$('foreground').hidden=!s.can_foreground;};
$('pause').onclick=()=>api()&&api().pause();$('resume').onclick=()=>api()&&api().resume();
$('stop').onclick=()=>api()&&api().stop();
$('foreground').onclick=()=>api()&&api().foreground();
$('label').onclick=()=>api()&&api().hide();$('pill').querySelector('.orb').onclick=()=>api()&&api().hide();
document.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();api()&&api().stop();}});
</script></body></html>"""


class OrbApi:
    """js_api of the orb window: only Pause / Resume / Stop, forwarded to the owning host bridge."""

    def __init__(self, on_pause: Callable[[], Any], on_resume: Callable[[], Any], on_stop: Callable[[], Any],
                 on_foreground: Callable[[], Any] | None = None):
        self._pause, self._resume, self._stop = on_pause, on_resume, on_stop
        self._foreground = on_foreground or (lambda: False)
        self.on_hide: Callable[[], Any] = lambda: None

    def hide(self):
        """Marker 218: a click on the pill hides it for the rest of the task (the glow stays; Esc still stops)."""
        self.on_hide()
        return True

    def foreground(self):
        return self._foreground()

    def pause(self):
        self._pause()
        return True

    def resume(self):
        self._resume()
        return True

    def stop(self):
        self._stop()
        return True


ORB_SIZE = (470, 44)                  # logical px: the pill itself, nothing around it (marker 212; wider 218)


def orb_position(monitors, size, owner_rect=None):
    """Physical coordinates: clamp the compact indicator to the owner's actual monitor."""
    monitor = next((r for r in monitors if r[0] <= 0 < r[2] and r[1] <= 0 < r[3]), monitors[0])
    if owner_rect:
        cx, cy = (owner_rect[0]+owner_rect[2])//2, (owner_rect[1]+owner_rect[3])//2
        monitor = next((r for r in monitors if r[0] <= cx < r[2] and r[1] <= cy < r[3]), monitor)
    l, t, r, b = monitor
    # Marker 218 (user, 26 Oct): at the top centre of the screen, like a "screen is being used" banner.
    return max(l, l + (r - l - size[0]) // 2), min(max(t, t + 10), max(t, b - size[1]))


def _default_orb_factory(api: OrbApi, state: IndicatorState) -> Any:   # pragma: no cover - needs the host GUI
    if not native_enabled():
        return None
    import webview
    # Marker 212 (user screenshot 24 Oct: a white box around "Jupiter paused"): transparent=True only clears WebView2,
    # the form behind it stays white, and the window was taller than the pill. Opaque, exactly the pill's size and
    # clipped to its rounded shape (the Pixie / Juno approach).
    window = webview.create_window("Jupiter control", html=ORB_HTML.replace("__LABEL__", html_lib.escape(state.label)),
                                 js_api=api, width=ORB_SIZE[0], height=ORB_SIZE[1], x=16, y=16, resizable=False,
                                 min_size=(120, 24),           # pywebview's default minimum (200 x 100) made it 100 tall
                                 frameless=True, easy_drag=True, on_top=True, focus=False, transparent=False,
                                 shadow=False, background_color="#0F111A")
    window.events.closed += api.stop           # closing must stop, not leave invisible automation running
    def place():
        # This pywebview runtime scales initial coordinates, but reports screen
        # Bounds in physical pixels. Position our OWN window in physical pixels
        # after creation, without activation or moving any user window.
        try:
            user32 = _win32()['user32']
            hwnd = int(window.native.Handle.ToInt64())
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            owner = wintypes.RECT()
            owner_rect = None
            if webview.windows and webview.windows[0].native:
                if user32.GetWindowRect(int(webview.windows[0].native.Handle.ToInt64()), ctypes.byref(owner)):
                    owner_rect = (owner.left, owner.top, owner.right, owner.bottom)
            _, monitors = desktop_frame_geometry(user32)
            dpi = ctypes.windll.user32.GetDpiForWindow(wintypes.HWND(hwnd)) or 96
            width, height = round(ORB_SIZE[0] * dpi / 96), round(ORB_SIZE[1] * dpi / 96)
            x, y = orb_position(monitors, (width, height), owner_rect)
            user32.SetWindowPos(hwnd, -1, x, y, width, height, 0x0010 | 0x0040)   # NOACTIVATE | SHOWWINDOW
            try:                                                      # no Windows 11 frame / rounding / backdrop
                dwm = ctypes.windll.dwmapi
                for attribute, value in ((2, 2), (33, 1), (34, 0xFFFFFFFE), (38, 1)):
                    setting = ctypes.c_uint(value)
                    dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attribute, ctypes.byref(setting), 4)
            except Exception:
                pass
            gdi32 = ctypes.windll.gdi32
            gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
            gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
            region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, height, height)
            if region:
                ctypes.windll.user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
                ctypes.windll.user32.SetWindowRgn(wintypes.HWND(hwnd), region, True)   # the system owns it now
        except Exception:
            pass  # safe initial top-left position remains available
    window.events.shown += place
    window.events.loaded += place
    return window


class ControlIndicator:
    """Lifecycle owner of the outline + orb for ONE session. Every method is safe to call repeatedly."""

    def __init__(self, *, on_pause: Callable[[], Any], on_resume: Callable[[], Any], on_stop: Callable[[], Any],
                 on_foreground: Callable[[], Any] | None = None,
                 outline_factory: Callable[[int], Any] | None = None,
                 orb_factory: Callable[[OrbApi, IndicatorState], Any] | None = None):
        self._api = OrbApi(on_pause, on_resume, on_stop, on_foreground)
        self._api.on_hide = self.hide_orb
        self.orb_hidden = False
        self._outline_factory = outline_factory or (lambda handle: OutlineOverlay(handle).start())
        self._orb_factory = orb_factory or _default_orb_factory
        self.outline: Any = None
        self.orb: Any = None
        self.state = IndicatorState(False, "hidden", "")
        self._lock = threading.RLock()

    def show(self, target_handle: int | None, state: IndicatorState) -> None:
        with self._lock:
            if target_handle and self.outline is None:
                try:
                    self.outline = self._outline_factory(int(target_handle))
                except Exception:
                    self.outline = None                                # indicator failure never blocks Stop/control
            if self.orb is None:
                try:
                    self.orb = self._orb_factory(self._api, state)
                except Exception:
                    self.orb = None
            self._apply(state)

    def update(self, snapshot: dict[str, Any] | None) -> IndicatorState:
        state = indicator_state(snapshot)
        if not state.visible:
            self.end()
            return state
        with self._lock:
            self._apply(state)
        return state

    def hide_orb(self) -> None:
        with self._lock:
            self.orb_hidden = True
            if self.orb is not None:
                try:
                    self.orb.hide()
                except Exception:
                    pass

    def acted(self, point: tuple[int, int] | None, kind: str = "click") -> None:
        with self._lock:
            if self.outline is not None and point:
                try:
                    self.outline.pulse(point, kind)
                except TypeError:                                      # simple outlines: point only
                    self.outline.pulse(point)
                except Exception:
                    pass

    def _apply(self, state: IndicatorState) -> None:
        self.state = state
        if self.outline is not None:
            try:
                self.outline.set_mode(state.mode)
            except Exception:
                pass
        if self.orb is not None:
            try:
                self.orb.evaluate_js(f"window.setJupiterIndicator&&window.setJupiterIndicator("
                                     f"{json.dumps({'mode': state.mode, 'label': state.label, 'can_foreground': state.can_foreground})})")
            except Exception:
                pass

    def end(self) -> None:
        """Remove every indicator and release its resources (outline thread/window, orb window)."""
        with self._lock:
            outline, orb = self.outline, self.orb
            self.outline = self.orb = None
            self.state = IndicatorState(False, "hidden", "")
        for item, method in ((outline, "close"), (orb, "destroy")):
            if item is not None:
                try:
                    getattr(item, method)()
                except Exception:
                    pass

    @property
    def active(self) -> bool:
        return self.outline is not None or self.orb is not None
