"""Windows backend for services.computer_control (marker 159).

Uses only what Jupiter already ships with: ctypes (Win32), pythonnet (.NET UI Automation, already used by the
pywebview host) and PIL (window capture). It never runs a shell command string, never elevates, never touches the
secure desktop (UAC / lock screen / credential prompts) and only ever targets the window handle it is given.
Every action re-checks the target. Background mode uses targeted native messages and pauses rather than invoking
an activation-prone provider while the user works elsewhere. Only explicitly granted foreground mode restores or
focuses a target, waits while the user is active elsewhere, and re-checks it before each SendInput batch. A pointer
click also requires that the point's top-level window IS the target. SendInput is global: a window switch between
the last check and delivery is narrowed, not made impossible; background mode never uses it.
"""

from __future__ import annotations

import ctypes
import io
import os
import re
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from typing import Any, Callable

from services.computer_control import HUMAN_BARRIERS, PRIVATE_SURFACES, Observation

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.WindowFromPoint.argtypes, user32.WindowFromPoint.restype = [wintypes.POINT], wintypes.HWND
    user32.GetAncestor.argtypes, user32.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.GetParent.argtypes, user32.GetParent.restype = [wintypes.HWND], wintypes.HWND
    user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                           wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = ctypes.c_size_t
    user32.GetNextDlgGroupItem.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.BOOL]
    user32.GetNextDlgGroupItem.restype = wintypes.HWND
else:                                                                  # imported on other platforms only by tests
    user32 = kernel32 = None

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_WHEEL, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0800, 0x8000, 0x4000
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
_UIA_STRONG_NAME = "Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35"
BLOCKED_PROCESSES = frozenset({"consent.exe", "logonui.exe", "credentialuibroker.exe", "lockapp.exe",
                               "securityhealthsystray.exe", "taskmgr.exe"})
VK = {"enter": 0x0D, "tab": 0x09, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28,
      "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "space": 0x20,
      "shift": 0x10, "ctrl": 0x11, "a": 0x41, "c": 0x43, "v": 0x56, "z": 0x5A, "s": 0x53, "f": 0x46,
      # marker 209: browser shortcuts (ctrl+t / w / l / r / n, alt+left / right, f5)
      "alt": 0x12, "t": 0x54, "w": 0x57, "l": 0x4C, "r": 0x52, "n": 0x4E, "f5": 0x74}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def _send(inputs: list[INPUT]) -> bool:
    array = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)) == len(inputs)


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    item = INPUT(type=INPUT_KEYBOARD)
    item.union.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return item


def _mouse(dx: int = 0, dy: int = 0, data: int = 0, flags: int = 0) -> INPUT:
    item = INPUT(type=INPUT_MOUSE)
    item.union.mi = MOUSEINPUT(dx, dy, data, flags, 0, 0)
    return item


class WindowsBackend:
    """Performs ONE validated operation on ONE user-selected window. Validation lives in ControlSession."""

    def __init__(self):
        self._elements: dict[str, Any] = {}          # element id -> UIA element from the latest observation
        self._own_pid = os.getpid()

    # ── windows ───────────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _title(handle: int) -> str:
        length = user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    @staticmethod
    def _process_name(handle: int) -> str:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        process = kernel32.OpenProcess(0x1000, False, pid.value)     # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                return os.path.basename(buffer.value).casefold()
            return ""
        finally:
            kernel32.CloseHandle(process)

    @staticmethod
    def _pid(handle: int) -> int:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return int(pid.value)

    def _own(self, handle: int) -> bool:
        """Jupiter's own windows are never controllable: the planner must not click Jupiter's Start/Allow/Stop."""
        return self._pid(handle) == self._own_pid

    def _valid(self, handle: int) -> bool:
        return bool(handle and user32.IsWindow(handle) and user32.IsWindowVisible(handle) and not self._own(handle)
                    and self._process_name(handle) not in BLOCKED_PROCESSES)

    def window_identity(self, handle: int) -> dict[str, str]:
        """Title and process of a window WITHOUT capturing anything (the session's pre-capture privacy check)."""
        if not self._valid(handle):
            return {}
        return {"title": self._title(handle)[:200], "process": self._process_name(handle)}

    def list_windows(self) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(handle, _):
            if user32.IsWindowVisible(handle) and user32.GetWindowTextLengthW(handle) > 0 and not self._own(handle):
                process = self._process_name(handle)
                if process not in BLOCKED_PROCESSES:
                    windows.append({"handle": int(handle), "title": self._title(handle)[:120], "process": process})
            return True

        user32.EnumWindows(collect, 0)
        return windows[:60]

    def _rect(self, handle: int) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        user32.GetWindowRect(handle, ctypes.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom

    def _dpi_scale(self, handle: int) -> float:
        try:
            return max(1.0, user32.GetDpiForWindow(handle) / 96.0)
        except AttributeError:
            return 1.0

    # ── observe ───────────────────────────────────────────────────────────────────────────────────────────────
    def observe(self, handle: int, *, screenshot: bool) -> Observation:
        if not self._valid(handle):
            raise OSError("window_unavailable")
        left, top, right, bottom = self._rect(handle)
        minimized = self._iconic(handle)
        elements = self._uia_elements(handle, left, top, keep_offscreen=minimized)
        image = None
        title = self._title(handle)
        screenshot = screenshot and not minimized                     # a minimised window has no pixels to read
        # Re-read the title right before any pixel capture: a window that turned private is never grabbed.
        if screenshot and right > left and bottom > top and not (PRIVATE_SURFACES.search(title)
                                                                  or HUMAN_BARRIERS.search(title)):
            try:
                from PIL import ImageGrab
                shot = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
                shot.thumbnail((1280, 1280))
                buffer = io.BytesIO()
                shot.convert("RGB").save(buffer, format="JPEG", quality=70)
                image = buffer.getvalue()                             # memory only, never written to disk
            except Exception:
                image = None
        return Observation(observation_id=uuid.uuid4().hex, window_handle=handle, window_title=title,
                           captured_at=time.time(), elements=elements, screenshot_jpeg=image,
                           dpi_scale=self._dpi_scale(handle), bounds=(0, 0) if minimized else (max(0, right - left), max(0, bottom - top)),
                           minimized=minimized)

    def _uia_elements(self, handle: int, left: int, top: int, *, keep_offscreen: bool = False) -> list[dict[str, Any]]:
        self._elements = {}
        try:
            import clr                                                 # pythonnet, already shipped for pywebview
            # Strong names: .NET Framework resolves the WPF UI Automation assemblies from the GAC only this way.
            clr.AddReference(f"UIAutomationClient, {_UIA_STRONG_NAME}")
            clr.AddReference(f"UIAutomationTypes, {_UIA_STRONG_NAME}")
            from System import IntPtr
            from System.Windows.Automation import AutomationElement, Condition, TreeScope
        except Exception:
            return []
        try:
            root = AutomationElement.FromHandle(IntPtr(handle))
            found = root.FindAll(TreeScope.Descendants, Condition.TrueCondition)
        except Exception:
            return []
        elements: list[dict[str, Any]] = []
        for index in range(min(found.Count, 250)):
            try:
                element = found[index]
                info = element.Current
                rect = info.BoundingRectangle
                offscreen = rect.IsEmpty or rect.Width <= 0 or rect.Height <= 0
                role = str(info.ControlType.ProgrammaticName or "").replace("ControlType.", "")[:40]
                # Marker 211: a closed drop-down's options have no rectangle but can still be chosen in the background
                if offscreen and not keep_offscreen and not (role == "ListItem" and self._in_combo_box(element)):
                    continue
                element_id = f"e{index}"
                self._elements[element_id] = element
                item = {
                    "id": element_id, "name": str(info.Name or "")[:120], "role": role,
                    "enabled": bool(info.IsEnabled), "focused": bool(info.HasKeyboardFocus),
                    "password": bool(info.IsPassword),                 # field values are never read
                    # A minimised window's controls have no on-screen rectangle (no coordinate clicks possible).
                    "rect": None if offscreen else [int(rect.X - left), int(rect.Y - top), int(rect.Width), int(rect.Height)],
                }
                if role in self.CHOICE_ROLES:                          # on / off only, so "tick X" is not a blind toggle
                    checked = self._checked(element)
                    if checked is not None:
                        item["checked"] = checked
                elements.append(item)
            except Exception:
                continue
        return elements

    CHOICE_ROLES = frozenset({"CheckBox", "RadioButton", "ListItem", "TabItem", "MenuItem"})

    def _checked(self, element: Any) -> bool | None:
        toggle = self._pattern(element, "TogglePattern")
        if toggle is not None:
            state = self._safe(lambda: int(toggle.Current.ToggleState))
            if state is not None:
                return state == 1
        selection = self._pattern(element, "SelectionItemPattern")
        if selection is not None:
            selected = self._safe(lambda: bool(selection.Current.IsSelected))
            if selected is not None:
                return selected
        return None

    @staticmethod
    def _parent(element: Any) -> Any:
        try:
            from System.Windows.Automation import TreeWalker
            return TreeWalker.ControlViewWalker.GetParent(element)
        except Exception:
            return None

    def _in_combo_box(self, element: Any) -> bool:
        node = element
        for _ in range(2):
            node = self._parent(node)
            if node is None:
                return False
            if str(self._safe(lambda n=node: n.Current.ControlType.ProgrammaticName) or "").endswith("ComboBox"):
                return True
        return False

    # ── act ───────────────────────────────────────────────────────────────────────────────────────────────────
    # BACKGROUND (default): UI Automation control patterns and messages posted to the ONE target control's own window
    # handle. These never move the physical pointer, never call SetForegroundWindow and never send global input, so
    # the user keeps working normally. When neither can do a step, NeedsForeground is raised and the session pauses
    # for the user's explicit grant — there is no silent fallback.
    # FOREGROUND (explicit per-session grant): real pointer/keyboard via SendInput, but only while the user is not
    # actively using another window (UserBusy), and only into the verified target.
    def set_input_mode(self, mode: str) -> None:
        self._mode = mode if mode in {"background", "foreground"} else "background"

    @property
    def input_mode(self) -> str:
        return getattr(self, "_mode", "background")

    def _pulse(self, handle: int, point: tuple[int, int] | None, kind: str) -> None:
        """Tell the on-screen indicator WHERE Jupiter acted (an honest marker, not a second mouse pointer)."""
        callback = getattr(self, "on_action", None)
        if callable(callback):
            try:
                callback(int(handle), point, kind)
            except Exception:
                pass

    @staticmethod
    def _safe(read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception:
            return None

    @staticmethod
    def _element_center(element: Any) -> tuple[int, int] | None:
        try:
            rect = element.Current.BoundingRectangle
            if rect.IsEmpty or rect.Width <= 0 or rect.Height <= 0:
                return None
            return int(rect.X + rect.Width / 2), int(rect.Y + rect.Height / 2)
        except Exception:
            return None

    @staticmethod
    def _pattern(element: Any, name: str) -> Any:
        try:
            import System.Windows.Automation as automation
            return element.GetCurrentPattern(getattr(automation, name).Pattern)
        except Exception:
            return None

    def _uia_activate(self, element: Any) -> bool:
        """Press a control semantically: Invoke, Toggle, SelectionItem.Select or ExpandCollapse. No pointer, no focus."""
        attempts = (
            ("InvokePattern", lambda p: p.Invoke()),
            ("TogglePattern", lambda p: p.Toggle()),
            ("SelectionItemPattern", lambda p: p.Select()),
            ("ExpandCollapsePattern",
             lambda p: p.Collapse() if int(p.Current.ExpandCollapseState) == 1 else p.Expand()),
        )
        for name, run in attempts:
            pattern = self._pattern(element, name)
            if pattern is None:
                continue
            try:
                run(pattern)
                return True
            except Exception:
                continue
        return False

    def _iconic(self, handle: int) -> bool:
        return bool(user32.IsIconic(handle))

    # Foreground-mode etiquette: never pull focus while the user is actively working elsewhere.
    USER_ACTIVE_SECONDS = 2.5

    def _user_active_elsewhere(self, handle: int) -> bool:
        if user32.GetForegroundWindow() == handle:
            return False

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return True                                                # unknown: assume busy, never steal
        now = kernel32.GetTickCount()
        ours = getattr(self, "_last_injection_tick", 0)
        user_input = info.dwTime > ours + 100                         # our own SendInput also updates the clock
        return bool(user_input and (now - info.dwTime) < self.USER_ACTIVE_SECONDS * 1000)

    def _take_foreground(self, handle: int) -> bool:
        from services.computer_control import UserBusy
        if not self._valid(handle):
            return False
        if self._user_active_elsewhere(handle):
            raise UserBusy("user_active")
        if self._iconic(handle):
            user32.ShowWindow(handle, 9)                              # SW_RESTORE (granted foreground mode only)
        user32.SetForegroundWindow(handle)
        time.sleep(0.15)
        return user32.GetForegroundWindow() == handle

    def _sent(self, inputs: list[INPUT]) -> bool:
        ok = _send(inputs)
        self._last_injection_tick = kernel32.GetTickCount()
        return ok

    def focus(self, handle: int) -> bool:
        if not self._valid(handle):
            return False
        if self.input_mode != "foreground":
            return True                     # background actions need no focus; Jupiter never foregrounds the window
        return self._take_foreground(handle)

    @staticmethod
    def _still_foreground(handle: int) -> bool:
        return bool(user32.GetForegroundWindow() == handle)

    @staticmethod
    def _root_at(x_screen: int, y_screen: int) -> int:
        point = wintypes.POINT(x_screen, y_screen)
        child = user32.WindowFromPoint(point)
        return int(user32.GetAncestor(child, 2) or 0) if child else 0       # GA_ROOT

    def _absolute(self, x: int, y: int) -> tuple[int, int]:
        vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw, vh = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)), max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        return int((x - vx) * 65535 / (vw - 1)), int((y - vy) * 65535 / (vh - 1))

    def click(self, handle: int, *, element_id: str | None, x: int | None, y: int | None) -> bool:
        from services.computer_control import NeedsForeground
        if not self._valid(handle):
            return False
        element = None
        if element_id is not None:
            element = self._elements.get(str(element_id))
            if element is None:
                return False
            # Standard Win32 push button: send the button's own BN_CLICKED notification to its parent. Measured on
            # Windows 11 (8 Oct 2026): UI Automation Invoke and a posted BM_CLICK both ACTIVATE the target window
            # (stealing focus from the user); the parent WM_COMMAND presses it with the foreground untouched.
            if self._press_win32_button(handle, element) or self._set_win32_choice(handle, element):
                self._pulse(handle, self._element_center(element), "click")
                return True
            before = int(user32.GetForegroundWindow() or 0)
            # Invoke/Toggle/Select are implemented by the target application,
            # which may activate its window. Do not try them behind the user's
            # current work, then attempt to undo focus afterwards.
            if self.input_mode == "background" and before != int(handle):
                raise NeedsForeground("provider_may_activate")
            if self.input_mode == "foreground" and not self._take_foreground(handle):
                return False
            if self._uia_activate(element):                           # background: semantic press, no pointer
                after = int(user32.GetForegroundWindow() or 0)
                # Some apps' providers bring their own window forward when pressed. Never hidden: reported.
                self.last_focus_side_effect = bool(after != before and after and self._belongs_to(after, handle))
                self._pulse(handle, self._element_center(element), "click")
                return True
            if self.input_mode != "foreground":
                raise NeedsForeground("minimised" if self._iconic(handle) else "no_pattern")
        elif self.input_mode != "foreground":
            raise NeedsForeground("coordinate_click")
        if not self._take_foreground(handle):
            return False                                              # never click into another window
        left, top, right, bottom = self._rect(handle)
        if element is not None:
            center = self._element_center(element)
            if center is None:
                return False
            x_screen, y_screen = center
        else:
            # Window-relative, in the SAME space as the observed element rects (GetWindowRect origin), which is what
            # the validator hit-tested against. No extra DPI multiplication.
            x_screen, y_screen = int(left + int(x)), int(top + int(y))
        if not (left <= x_screen < right and top <= y_screen < bottom):
            return False                                              # outside the target window
        if self._root_at(x_screen, y_screen) != int(handle) or not self._still_foreground(handle):
            return False                                              # covered by another window, or focus moved
        ax, ay = self._absolute(x_screen, y_screen)
        flags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        self._pulse(handle, (x_screen, y_screen), "pointer")
        return self._sent([_mouse(ax, ay, 0, flags | MOUSEEVENTF_MOVE), _mouse(ax, ay, 0, flags | MOUSEEVENTF_LEFTDOWN),
                           _mouse(ax, ay, 0, flags | MOUSEEVENTF_LEFTUP)])

    def _belongs_to(self, window: int, handle: int) -> bool:
        """True when ``window`` is the target or one of its controls (same top-level window)."""
        return int(window) == int(handle) or int(user32.GetAncestor(window, 2) or 0) == int(handle)

    def _press_win32_button(self, handle: int, element: Any) -> bool:
        hwnd = self._control_hwnd(handle, element)
        if not hwnd or self._class_name(hwnd) != "button":
            return False
        style = int(user32.GetWindowLongW(hwnd, -16)) & 0x0F          # GWL_STYLE & BS_TYPEMASK
        if style not in {0x0, 0x1}:                                   # BS_PUSHBUTTON / BS_DEFPUSHBUTTON only
            return False
        parent = int(user32.GetParent(hwnd) or 0)
        control_id = int(user32.GetDlgCtrlID(hwnd)) & 0xFFFF
        if not parent or not control_id:
            return False
        return bool(user32.PostMessageW(parent, 0x0111, control_id, hwnd))   # WM_COMMAND, BN_CLICKED (0) << 16

    # Marker 211: Win32 check boxes, radio buttons, list boxes and drop-downs are set with the control's own messages and
    # announced to the parent like a real click (BN_CLICKED / LBN_SELCHANGE / CBN_SELCHANGE). UI Automation Toggle and
    # Select activate the window on Windows 11, so these steps used to stop and ask for the user's mouse.
    _CHECK_STYLES = frozenset({0x2, 0x3, 0x5, 0x6})       # BS_CHECKBOX, BS_AUTOCHECKBOX, BS_3STATE, BS_AUTO3STATE
    _RADIO_STYLES = frozenset({0x4, 0x9})                 # BS_RADIOBUTTON, BS_AUTORADIOBUTTON

    @staticmethod
    def _send_message(hwnd: int, message: int, wparam: int = 0, lparam: Any = 0) -> int | None:
        """SendMessage that never hangs Jupiter on a frozen app (1 s, abort if hung); None when it did not answer."""
        result = ctypes.c_size_t(0)
        if isinstance(lparam, ctypes.Array):
            lparam = ctypes.cast(lparam, ctypes.c_void_p).value or 0
        ok = user32.SendMessageTimeoutW(hwnd, message, ctypes.c_size_t(wparam).value, lparam, 0x0002, 1000,
                                        ctypes.byref(result))
        return ctypes.c_ssize_t(result.value).value if ok else None

    def _set_win32_choice(self, handle: int, element: Any) -> bool:
        hwnd = self._control_hwnd(handle, element)
        if not hwnd or self._class_name(hwnd) != "button":
            return self._select_win32_item(handle, element)
        style = int(user32.GetWindowLongW(hwnd, -16)) & 0x0F
        parent = int(user32.GetParent(hwnd) or 0)
        control_id = int(user32.GetDlgCtrlID(hwnd)) & 0xFFFF
        if style not in self._CHECK_STYLES | self._RADIO_STYLES or not parent or not control_id:
            return False
        if style in {0x3, 0x6}:                                       # automatic: the box flips itself on a click
            state = self._send_message(hwnd, 0x00F0)                  # BM_GETCHECK
            if state is None:
                return False
            self._send_message(hwnd, 0x00F1, (int(state) + 1) % (3 if style == 0x6 else 2))   # BM_SETCHECK
        elif style == 0x9:                                            # automatic radio: only this one in its group
            sibling = hwnd
            for _ in range(64):
                sibling = int(user32.GetNextDlgGroupItem(parent, sibling, False) or 0)
                if not sibling or sibling == hwnd:
                    break
                if self._class_name(sibling) == "button" and (int(user32.GetWindowLongW(sibling, -16)) & 0x0F) == 0x9:
                    self._send_message(sibling, 0x00F1, 0)
            self._send_message(hwnd, 0x00F1, 1)
        return bool(user32.PostMessageW(parent, 0x0111, control_id, hwnd))   # WM_COMMAND, BN_CLICKED (0) << 16

    def _select_win32_item(self, handle: int, element: Any, text: str | None = None) -> bool:
        """An option of a Win32 list box / drop-down (or, with ``text``, the drop-down itself): choose it by its exact
        text, like picking it with the mouse."""
        name = str(text if text is not None else (self._safe(lambda: element.Current.Name) or "")).strip()
        if not name or element is None:
            return False
        hwnd, kind, node = 0, "", element
        for _ in range(4):                                            # item -> (drop-down list) -> combo box / list box
            candidate = self._control_hwnd(handle, node)
            if candidate and self._class_name(candidate) in {"combobox", "listbox"}:
                hwnd, kind = candidate, self._class_name(candidate)
                break
            node = self._parent(node)
            if node is None:
                return False
        if not hwnd:
            return False
        buffer = ctypes.create_unicode_buffer(name[:255])
        find, select = (0x0158, 0x014E) if kind == "combobox" else (0x01A2, 0x0186)   # *_FINDSTRINGEXACT, *_SETCURSEL
        index = self._send_message(hwnd, find, -1, buffer)
        if index is None or index < 0:
            return False
        if kind == "listbox" and int(user32.GetWindowLongW(hwnd, -16)) & 0x0808:   # LBS_MULTIPLESEL / LBS_EXTENDEDSEL
            self._send_message(hwnd, 0x0185, 1, index)                # LB_SETSEL
        else:
            self._send_message(hwnd, select, index)
        parent = int(user32.GetParent(hwnd) or 0)
        control_id = int(user32.GetDlgCtrlID(hwnd)) & 0xFFFF
        if not parent or not control_id:
            return False
        return bool(user32.PostMessageW(parent, 0x0111, (1 << 16) | control_id, hwnd))   # CBN_ / LBN_SELCHANGE

    # Classic Win32 text controls accept characters posted to their own window handle without focus.
    TEXT_CONTROL_CLASSES = ("edit", "richedit", "scintilla")

    @staticmethod
    def _class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buffer, 128)
        return buffer.value.casefold()

    def _text_target(self, handle: int, element_id: str | None) -> Any:
        """The ONE field to type into: the named control, else the single focused control, else the single editor.
        Ambiguous screens return None — Jupiter never guesses a field."""
        if element_id is not None:
            return self._elements.get(str(element_id))
        focused = [e for e in self._elements.values() if self._safe(lambda e=e: bool(e.Current.HasKeyboardFocus))]
        if len(focused) == 1:
            return focused[0]
        editors = [e for e in self._elements.values()
                   if str(self._safe(lambda e=e: e.Current.ControlType.ProgrammaticName) or "").endswith(("Edit", "Document"))
                   and not self._safe(lambda e=e: bool(e.Current.IsPassword))]
        return editors[0] if len(editors) == 1 else None

    def _control_hwnd(self, handle: int, element: Any) -> int:
        """The element's own native window handle, only if it belongs to the target top-level window."""
        hwnd = int(self._safe(lambda: element.Current.NativeWindowHandle) or 0) if element is not None else 0
        return hwnd if hwnd and int(user32.GetAncestor(hwnd, 2) or 0) == int(handle) else 0

    def type_text(self, handle: int, text: str, element_id: str | None = None) -> bool:
        from services.computer_control import NeedsForeground
        if not self._valid(handle):
            return False
        data = text.encode("utf-16-le")                               # surrogate pairs are sent as two units
        units = [int.from_bytes(data[offset:offset + 2], "little") for offset in range(0, len(data), 2)]
        element = self._text_target(handle, element_id)
        if element is not None and not self._safe(lambda: bool(element.Current.IsPassword)):
            hwnd = self._control_hwnd(handle, element)
            if hwnd and self._class_name(hwnd) == "combobox" and self._select_win32_item(handle, element, text=text):
                self._pulse(handle, self._element_center(element), "type")   # typing an option's name picks it
                return True
            if hwnd and self._class_name(hwnd).startswith(self.TEXT_CONTROL_CLASSES):
                chars = [0x0D if unit == 0x0A else unit for unit in units if unit != 0x0D]
                if all(user32.PostMessageW(hwnd, 0x0102, unit, 1) for unit in chars):   # WM_CHAR to that control
                    self._pulse(handle, self._element_center(element), "type")
                    return True
            value = self._pattern(element, "ValuePattern")
            if value is not None and not self._safe(lambda: bool(value.Current.IsReadOnly)):
                # Marker 212: an address / search bar gets the new address (typing there replaces the selected URL);
                # other fields keep what is in them and get the text added at the end, like typing.
                name = str(self._safe(lambda: element.Current.Name) or "")
                replace = bool(self.REPLACE_FIELDS.search(name))
                if replace and int(user32.GetForegroundWindow() or 0) == int(handle):
                    # the address bar takes the keyboard focus (the user's front window is this one), so the Enter
                    # that follows goes to it and not to the page (measured on Edge, 24 Oct)
                    self._safe(lambda: element.SetFocus())
                current = "" if replace else str(self._safe(lambda: value.Current.Value) or "")
                value.SetValue(current + text)                        # host memory only, never exported
                self._pulse(handle, self._element_center(element), "type")
                return True
        if self.input_mode != "foreground":
            raise NeedsForeground("minimised" if self._iconic(handle) else "no_text_target")
        if not self._take_foreground(handle):
            return False
        for start in range(0, len(units), 16):                        # short chunks, each re-checked
            if not self._still_foreground(handle):
                return False
            chunk: list[INPUT] = []
            for code in units[start:start + 16]:
                chunk += [_key(0, code, KEYEVENTF_UNICODE), _key(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
            if not self._sent(chunk):
                return False
        return True

    BACKGROUND_KEYS = frozenset({"enter", "tab", "escape", "backspace", "delete", "up", "down", "left", "right",
                                 "home", "end", "pageup", "pagedown", "space"})
    REPLACE_FIELDS = re.compile(r"\b(?:address|search|url|location|omnibox)\b", re.I)
    _KEY_CHARS = {"enter": 0x0D, "tab": 0x09, "backspace": 0x08, "space": 0x20}

    def press_key(self, handle: int, key: str) -> bool:
        from services.computer_control import NeedsForeground
        if not self._valid(handle):
            return False
        parts = key.split("+")
        codes = [VK[part] for part in parts if part in VK]
        if len(codes) != len(parts):
            return False
        if self.input_mode != "foreground":
            if key not in self.BACKGROUND_KEYS:
                raise NeedsForeground("key_combination")
            element = self._text_target(handle, None)
            hwnd = self._control_hwnd(handle, element) or self._focused_control(handle)
            if not hwnd:
                raise NeedsForeground("minimised" if self._iconic(handle) else "no_pattern")
            vk = VK[key]
            if not self._class_name(hwnd).startswith(self.TEXT_CONTROL_CLASSES):
                # Marker 212: a browser / modern control reacts to the key itself (Edge's address bar opens the page
                # on Enter's key-down; a posted character did nothing)
                scan = int(user32.MapVirtualKeyW(vk, 0)) & 0xFF
                ok = bool(user32.PostMessageW(hwnd, 0x0100, vk, (scan << 16) | 1))
                ok = ok and bool(user32.PostMessageW(hwnd, 0x0101, vk, (scan << 16) | 0xC0000001))
                if ok:
                    self._pulse(handle, self._element_center(element), "key")
                return ok
            if key in self._KEY_CHARS:
                # Posting KEYDOWN and WM_CHAR together duplicates characters
                # when the app calls TranslateMessage, including Enter/beeps.
                ok = bool(user32.PostMessageW(hwnd, 0x0102, self._KEY_CHARS[key], 1))
                if ok:
                    self._pulse(handle, self._element_center(element), "key")
                return ok
            ok = bool(user32.PostMessageW(hwnd, 0x0100, vk, 1))       # WM_KEYDOWN to that control only
            ok = ok and bool(user32.PostMessageW(hwnd, 0x0101, vk, 0xC0000001))
            if ok:
                self._pulse(handle, self._element_center(element), "key")
            return ok
        if not self._take_foreground(handle) or not self._still_foreground(handle):
            return False
        return self._sent([_key(code) for code in codes] + [_key(code, 0, KEYEVENTF_KEYUP) for code in reversed(codes)])

    def _focused_control(self, handle: int) -> int:
        """Marker 212: the window that has the keyboard focus inside the target, when the target IS the user's front
        window (Edge / Chrome draw their address bar themselves, so it has no window of its own: Enter after typing an
        address needed the mouse). Never a window of another app."""
        if int(user32.GetForegroundWindow() or 0) != int(handle):
            return 0

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                        ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        thread = user32.GetWindowThreadProcessId(handle, None)
        if not thread or not user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
            return 0
        focus = int(info.hwndFocus or 0)
        return focus if focus and self._belongs_to(focus, handle) else 0

    @staticmethod
    def _scroll_amounts() -> tuple[Any, Any, Any]:
        from System.Windows.Automation import ScrollAmount            # loaded with the UIA assemblies in observe()
        return ScrollAmount.NoAmount, ScrollAmount.SmallIncrement, ScrollAmount.SmallDecrement

    def scroll(self, handle: int, amount: int) -> bool:
        from services.computer_control import NeedsForeground
        if not self._valid(handle):
            return False
        for element in self._elements.values():                       # semantic scroll first: no pointer move
            pattern = self._pattern(element, "ScrollPattern")
            if pattern is None or not self._safe(lambda p=pattern: bool(p.Current.VerticallyScrollable)):
                continue
            try:
                none, increment, decrement = self._scroll_amounts()
                for _ in range(abs(int(amount))):
                    pattern.Scroll(none, increment if amount > 0 else decrement)
                self._pulse(handle, self._element_center(element), "scroll")
                return True
            except Exception:
                continue
        if self.input_mode != "foreground":
            raise NeedsForeground("minimised" if self._iconic(handle) else "no_pattern")
        if not self._take_foreground(handle):
            return False
        left, top, right, bottom = self._rect(handle)
        ax, ay = self._absolute((left + right) // 2, (top + bottom) // 2)
        flags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        return self._sent([_mouse(ax, ay, 0, flags | MOUSEEVENTF_MOVE),
                           _mouse(0, 0, ctypes.c_uint32(-120 * int(amount)).value, MOUSEEVENTF_WHEEL)])

    def open_app(self, executable: str) -> bool:
        path = os.path.abspath(executable)
        if not (os.path.isfile(path) and path.casefold().endswith(".exe")):
            return False
        subprocess.Popen([path], shell=False, close_fds=True)         # approved path only, no arguments, no shell
        return True

    def run_script(self, argv: tuple[str, ...], timeout: int) -> dict[str, Any]:
        try:
            result = subprocess.run(list(argv), shell=False, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout"}
        except OSError as error:
            return {"ok": False, "error": type(error).__name__}
        return {"ok": result.returncode == 0, "exit_code": result.returncode,
                "output_tail": (result.stdout or "")[-1500:], "error": "" if result.returncode == 0 else "non_zero_exit"}

    def secure_desktop_active(self) -> bool:
        """True when the input desktop is not the user's normal desktop (lock screen, UAC, credential prompt)."""
        desktop = user32.OpenInputDesktop(0, False, 0x0001)          # DESKTOP_READOBJECTS
        if not desktop:
            return True
        try:
            buffer = ctypes.create_unicode_buffer(64)
            needed = wintypes.DWORD()
            user32.GetUserObjectInformationW(desktop, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed))
            return buffer.value.casefold() != "default"
        finally:
            user32.CloseDesktop(desktop)
