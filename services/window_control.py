"""Marker 208: "maximize the Edge window", "close the window you just opened", and "open Edge" when Edge is already open.

Real WT 23 Oct: Edge was open but minimised; "open Microsoft Edge ... maximize it" launched a SECOND Edge window, and
"close the new window and maximize the existing one" got "I'm afraid I can't". Now:
- open an app that already has a window -> that window is restored and brought to the front (a new one only on request);
- maximize / minimize / restore / close an app's window, or the window Jupiter itself opened last.

Only top-level, visible-or-minimised windows of the named app (matched by its executable, else by window title) are
touched; Jupiter's own windows never are. "Close" posts WM_CLOSE, the same as the window's X button, so an app with
unsaved work still asks the user. No input is typed and nothing is killed.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

ACTIONS = ("maximize", "minimize", "restore", "close", "focus")
_OWN_PROCESSES = {"jupiter.exe", "jupiterwt.exe"}
_OWN_TITLES = ("jupiter", "pixie - jupiter buddy", "juno - jupiter buddy")


@dataclass
class Window:
    hwnd: int
    title: str
    exe: str            # lower-case image name, e.g. "msedge.exe"
    minimized: bool


def app_exes(app: str) -> tuple[str, ...]:
    """Executable names that belong to a spoken app name ("edge" -> msedge.exe)."""
    from services.app_launcher import known_app, normalize
    known = known_app(app)
    exes = [c.lower() for c in (known[1] if known else ()) if not c.startswith("uri:")]
    extra = {"microsoft edge": "msedge.exe", "edge": "msedge.exe", "settings": "systemsettings.exe",
             "windows settings": "systemsettings.exe", "calculator": "calculatorapp.exe", "calc": "calculatorapp.exe",
             "whatsapp": "whatsapp.exe", "teams": "ms-teams.exe", "microsoft teams": "ms-teams.exe",
             "discord": "discord.exe", "telegram": "telegram.exe", "spotify": "spotify.exe", "photos": "photos.exe"}
    name = normalize(app)
    if name in extra and extra[name] not in exes:
        exes.append(extra[name])
    if not exes and name:
        exes.append(name.replace(" ", "") + ".exe")
    return tuple(exes)


def app_label(app: str) -> str:
    from services.app_launcher import known_app
    known = known_app(app)
    return known[0] if known else " ".join(str(app or "").split())[:60]


def matches(window: Window, app: str) -> bool:
    if window.exe in _OWN_PROCESSES or window.title.casefold().startswith(_OWN_TITLES):
        return False
    if window.exe in app_exes(app):
        return True
    title = window.title.casefold()
    words = [w for w in app_label(app).casefold().split() if len(w) > 2]
    if words and all(w in title for w in words):
        return True
    # Marker 217 (WT test 25 Oct: "minimize the settings window" -> "no window"): Store apps (Settings, Calculator) run
    # in ApplicationFrameHost.exe and their window title IS the app's name; the spoken name is matched to it too.
    spoken = " ".join(str(app or "").casefold().split())
    return window.exe in {"applicationframehost.exe", ""} and bool(spoken)      # "" = host not readable and (title == spoken or title.startswith(spoken + " "))


def fuzzy_windows(windows: list[Window], app: str, threshold: float = 0.7) -> list[Window]:
    """Marker 217 (real chat 25 Oct: "maximize the cloud" for Claude): a misheard app name matches the ONE open app whose
    executable name sounds closest (claude.exe for "cloud"); never Jupiter's own windows, never a guess between two."""
    import difflib
    spoken = "".join(str(app or "").casefold().split())
    if len(spoken) < 3:
        return []
    scores: dict[str, float] = {}
    for window in windows:
        if window.exe in _OWN_PROCESSES or window.title.casefold().startswith(_OWN_TITLES):
            continue
        stem = window.exe.removesuffix(".exe")
        scores[window.exe] = max(scores.get(window.exe, 0.0), difflib.SequenceMatcher(None, spoken, stem).ratio())
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked or ranked[0][1] < threshold or (len(ranked) > 1 and ranked[1][1] >= ranked[0][1] - 0.05):
        return []
    return [w for w in windows if w.exe == ranked[0][0]]


class WindowControl:
    """Host-side window actions. ``lister``/``actor`` are injected in tests; the defaults use Win32."""

    def __init__(self, *, lister: Callable[[], list[Window]] | None = None,
                 actor: Callable[[int, str], bool] | None = None, clock: Callable[[], float] = time.monotonic,
                 probe: Callable[[int], dict[str, bool]] | None = None, verify_s: float = 1.5):
        self._list = lister or _list_windows
        self._act = actor or _act
        self._clock = clock
        # Marker 218: the real window state after an action (only with the real Win32 actor; tests inject their own)
        self._probe = probe if probe is not None else (_probe if actor is None else None)
        self._verify_s = verify_s
        self.last_opened: tuple[int, str, float] | None = None      # (hwnd, app label, when) Jupiter opened last
        self.last_app = ""                                          # the app of the last open / window action

    def verified(self, hwnd: int, action: str, ok: bool, *, sleep: Callable[[float], Any] = time.sleep) -> bool:
        """Marker 218 (WT test report 25 Oct): an action counts only when the window really changed - minimised,
        maximised, restored, in front, or gone after "close" (an app asking to save keeps its window: not done)."""
        if not ok or self._probe is None:
            return ok
        expected = {"minimize": lambda s: s["iconic"], "maximize": lambda s: s["zoomed"],
                    "restore": lambda s: s["exists"] and not s["iconic"], "focus": lambda s: s["foreground"],
                    "close": lambda s: not s["exists"]}.get(action)
        if expected is None:
            return ok
        deadline = self._clock() + self._verify_s
        while True:
            try:
                if expected(self._probe(hwnd)):
                    return True
            except Exception:
                return ok
            if self._clock() >= deadline:
                return False
            sleep(0.1)

    def windows_of(self, app: str) -> list[Window]:
        listed = self._list()
        found = [w for w in listed if matches(w, app)]
        return found or fuzzy_windows(listed, app)

    def snapshot(self, app: str) -> set[int]:
        return {w.hwnd for w in self.windows_of(app)}

    def remember_new(self, app: str, before: set[int], *, wait_s: float = 4.0, step_s: float = 0.25,
                     sleep: Callable[[float], Any] = time.sleep) -> int:
        """After Jupiter launched ``app``: wait briefly for its new window and remember it for "close the new one"."""
        deadline = self._clock() + wait_s
        while True:
            new = [w for w in self.windows_of(app) if w.hwnd not in before]
            if new:
                self.last_opened = (new[0].hwnd, app_label(app), self._clock())
                self.last_app = app
                return new[0].hwnd
            if self._clock() >= deadline:
                return 0
            sleep(step_s)

    def front(self, hwnd: int) -> bool:
        """Marker 212: bring ONE open window (never Jupiter's own) to the front for a task the user asked Jupiter to do
        in it (real WT 24 Oct: "close these Edge tabs" paused twice because Edge was behind Jupiter, and a control
        pressed behind the user's front window may raise its app, so background steps wait for the mouse)."""
        window = next((w for w in self._list() if w.hwnd == int(hwnd or 0)), None)
        if window is None or window.exe in _OWN_PROCESSES or window.title.casefold().startswith(_OWN_TITLES):
            return False
        return bool(self._act(window.hwnd, "focus"))

    def bring_existing(self, app: str, *, maximize: bool = False) -> dict[str, Any] | None:
        """``app`` already has a window: restore it (or maximize) and bring it to the front. None when it has none."""
        windows = self.windows_of(app)
        if not windows:
            return None
        self.last_app = app
        target = next((w for w in windows if w.minimized), windows[0]) if not maximize else windows[0]
        ok = self._act(target.hwnd, "maximize" if maximize else "restore")
        ok = self.verified(target.hwnd, "maximize" if maximize else "restore", bool(ok))
        return {"ok": bool(ok), "app": app_label(app), "how": "existing_window", "maximized": bool(maximize and ok),
                "error": None if ok else "window_failed"}

    def act(self, action: str, app: str = "", target: str = "") -> dict[str, Any]:
        """One window action. ``target`` "last" = the window Jupiter opened last; otherwise the named app's window
        (for close: only when exactly one window of it exists, or the newest one Jupiter opened)."""
        action = str(action or "").casefold()
        if action not in ACTIONS:
            return {"ok": False, "error": "unknown_action"}
        if target == "last" or (not app and action == "close"):
            if not self.last_opened:
                return {"ok": False, "error": "nothing_opened", "action": action}
            hwnd, label, _when = self.last_opened
            alive = [w for w in self._list() if w.hwnd == hwnd]
            if not alive:
                self.last_opened = None
                return {"ok": False, "error": "already_closed", "app": label, "action": action}
            ok = self.verified(hwnd, action, bool(self._act(hwnd, action)))
            if ok and action == "close":
                self.last_opened = None
            return {"ok": bool(ok), "app": label, "action": action, "target": "last",
                    "error": None if ok else "window_failed"}
        if not app and self.last_app:              # "... so maximize it": the app this conversation is about
            app, target = self.last_app, target or "existing"
        if not app:
            return {"ok": False, "error": "no_app", "action": action}
        self.last_app = app
        if target == "exe":                       # marker 217: a name the classifier did not know, for "close"
            exes = app_exes(app)
            windows = [w for w in self._list() if w.exe in exes and w.exe not in _OWN_PROCESSES
                       and not w.title.casefold().startswith(_OWN_TITLES)]
        else:
            windows = self.windows_of(app)
        if not windows:
            return {"ok": False, "error": "no_window", "app": app_label(app), "action": action}
        if action == "close" and len(windows) > 1:
            mine = self.last_opened[0] if self.last_opened else 0
            chosen = next((w for w in windows if w.hwnd == mine), None)
            if chosen is None:
                return {"ok": False, "error": "several_windows", "app": app_label(app), "action": action,
                        "count": len(windows)}
            windows = [chosen]
        if target == "existing" and self.last_opened and len(windows) > 1:
            windows = [w for w in windows if w.hwnd != self.last_opened[0]] or windows
        chosen = windows[0] if action != "restore" else next((w for w in windows if w.minimized), windows[0])
        ok = self.verified(chosen.hwnd, action, bool(self._act(chosen.hwnd, action)))
        if not ok and action == "close":
            return {"ok": False, "app": app_label(app), "action": action, "error": "still_open"}
        if ok and action == "close" and self.last_opened and self.last_opened[0] == chosen.hwnd:
            self.last_opened = None
        return {"ok": bool(ok), "app": app_label(app), "action": action, "error": None if ok else "window_failed"}


# ── Win32 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _probe(hwnd: int) -> dict[str, bool]:                                # pragma: no cover - needs Windows
    import ctypes
    user32 = ctypes.windll.user32
    exists = bool(user32.IsWindow(hwnd)) and bool(user32.IsWindowVisible(hwnd))
    return {"exists": exists, "iconic": exists and bool(user32.IsIconic(hwnd)),
            "zoomed": exists and bool(user32.IsZoomed(hwnd)), "foreground": int(user32.GetForegroundWindow() or 0) == hwnd}


def _list_windows() -> list[Window]:                                     # pragma: no cover - needs Windows
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    out: list[Window] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def exe_of(hwnd) -> str:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)         # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(520)
            buf = ctypes.create_unicode_buffer(520)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1].lower()
            return ""
        finally:
            kernel32.CloseHandle(handle)

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):      # GW_OWNER: skip owned popups
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80:                            # WS_EX_TOOLWINDOW
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        cloaked = ctypes.c_int(0)
        try:
            ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)   # DWMWA_CLOAKED
        except Exception:
            pass
        if cloaked.value:
            return True
        exe = exe_of(hwnd)
        if exe == "applicationframehost.exe":                                  # UWP host: match by title instead
            exe = ""
        out.append(Window(int(hwnd), title.value, exe, bool(user32.IsIconic(hwnd))))
        return True

    user32.EnumWindows(proc(visit), 0)
    return out


def _act(hwnd: int, action: str) -> bool:                                # pragma: no cover - needs Windows
    if sys.platform != "win32":
        return False
    import ctypes
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        return False
    if action == "close":
        return bool(user32.PostMessageW(hwnd, 0x0010, 0, 0))                  # WM_CLOSE (same as the X button)
    show = {"maximize": 3, "minimize": 6, "restore": 9, "focus": 9}[action]   # SW_MAXIMIZE / SW_MINIMIZE / SW_RESTORE
    if action == "focus" and not user32.IsIconic(hwnd):
        show = 5                                                               # SW_SHOW
    user32.ShowWindow(hwnd, show)
    if action != "minimize":
        # Windows only lets the foreground thread hand focus over: briefly share its input state (no keys are sent).
        kernel32 = ctypes.windll.kernel32
        foreground = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        me = kernel32.GetCurrentThreadId()
        attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_thread, False)
    return True
