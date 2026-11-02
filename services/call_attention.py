"""Windows WT host hooks for incoming Jupiter calls (marker 172).

- ``call_attention(True)`` flashes the Jupiter taskbar button (FlashWindowEx, until the user switches to Jupiter) and
  NEVER calls SetForegroundWindow: an incoming call must not steal focus from what the user is doing.
- ``call_quiet_state()`` reports Windows' own "do not disturb" state (SHQueryUserNotificationState: Do Not Disturb /
  Focus, presentation mode, full-screen app/game). The page then shows the call silently, the desktop equivalent of a
  phone on silent. Windows cannot inherit the Android phone's ringtone; the page uses its own tone or a custom file.
"""

from __future__ import annotations

import ctypes
import os
import sys

FLASHW_STOP, FLASHW_TRAY, FLASHW_TIMERNOFG = 0, 0x2, 0xC
# QUERY_USER_NOTIFICATION_STATE
QUNS_NOT_PRESENT, QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE = 1, 2, 3, 4
QUNS_ACCEPTS_NOTIFICATIONS, QUNS_QUIET_TIME, QUNS_APP = 5, 6, 7
QUIET_STATES = {QUNS_NOT_PRESENT: "away", QUNS_BUSY: "busy", QUNS_RUNNING_D3D_FULL_SCREEN: "full_screen",
                QUNS_PRESENTATION_MODE: "presentation", QUNS_QUIET_TIME: "quiet_time", QUNS_APP: "do_not_disturb"}


def quiet_from_state(state: int) -> dict:
    """Pure mapping (unit-tested): anything but 'accepts notifications' means ring silently."""
    reason = QUIET_STATES.get(int(state), "")
    return {"quiet": bool(reason), "reason": reason}


class CallAttentionBridgeMixin:
    def _jupiter_hwnds(self) -> list[int]:
        if sys.platform != "win32":
            return []
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        pid = os.getpid()
        found: list[int] = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):   # GW_OWNER
                found.append(int(hwnd))
            return True

        user32.EnumWindows(proc(visit), 0)
        return found

    def call_attention(self, active=True):
        """Flash (or stop flashing) the taskbar button only; never activates the window."""
        if sys.platform != "win32":
            return {"status": "unsupported"}
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
        flags = (FLASHW_TRAY | FLASHW_TIMERNOFG) if active else FLASHW_STOP
        for hwnd in self._jupiter_hwnds():
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, flags, 0 if active else 0, 0)
            user32.FlashWindowEx(ctypes.byref(info))
        return {"status": "flashing" if active else "stopped"}

    def call_quiet_state(self):
        if sys.platform != "win32":
            return {"quiet": False, "reason": ""}
        state = ctypes.c_int(QUNS_ACCEPTS_NOTIFICATIONS)
        try:
            if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
                return {"quiet": False, "reason": ""}
        except (AttributeError, OSError):
            return {"quiet": False, "reason": ""}
        return quiet_from_state(state.value)
