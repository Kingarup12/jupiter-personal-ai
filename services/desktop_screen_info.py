"""Marker 217 (real WT 25 Oct: "can you tell me how many applications are open in my laptop by viewing the taskbar?" got
"I'm working on that, the results will appear on your screen" and nothing): questions ABOUT the desktop are answered
from what Windows reports, at once. Read-only: window titles, app names and which window is in front; nothing is
clicked, captured or stored."""
from __future__ import annotations

import sys
from typing import Any, Callable

# Executable -> the name people use. Anything else: the executable name without ".exe", title-cased.
APP_NAMES = {
    "chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Firefox", "brave.exe": "Brave",
    "opera.exe": "Opera", "explorer.exe": "File Explorer", "spotify.exe": "Spotify", "code.exe": "VS Code",
    "notepad.exe": "Notepad", "winword.exe": "Word", "excel.exe": "Excel", "powerpnt.exe": "PowerPoint",
    "outlook.exe": "Outlook", "olk.exe": "Outlook", "teams.exe": "Teams", "ms-teams.exe": "Teams",
    "whatsapp.exe": "WhatsApp", "telegram.exe": "Telegram", "discord.exe": "Discord", "zoom.exe": "Zoom",
    "vlc.exe": "VLC", "systemsettings.exe": "Settings", "calculatorapp.exe": "Calculator", "cmd.exe": "Command Prompt",
    "windowsterminal.exe": "Terminal", "powershell.exe": "PowerShell", "chatgpt.exe": "ChatGPT", "claude.exe": "Claude",
    "taskmgr.exe": "Task Manager", "mspaint.exe": "Paint", "photos.exe": "Photos", "steam.exe": "Steam",
}
# Windows' own surfaces that are not apps on the taskbar.
_NOT_APPS = {"Program Manager", "Windows Input Experience", "NVIDIA GeForce Overlay", "Jupiter control glow",
             "Jupiter action cursor", "Jupiter control", "Jupiter notch", "Jupiter Astro Buddy"}


def app_name(process: str, title: str = "") -> str:
    process = str(process or "").casefold()
    if process.startswith("pythonw") or process.startswith("python"):
        return "Jupiter" if "jupiter" in str(title).casefold() else "Python"
    if process == "applicationframehost.exe":            # UWP apps (Settings, Calculator): the title is the app
        return str(title or "App")
    return APP_NAMES.get(process) or process.removesuffix(".exe").replace("-", " ").title() or "App"


def taskbar_windows() -> list[dict[str, Any]]:                                # pragma: no cover - needs Windows
    """Top-level windows that have a taskbar button, front-to-back (EnumWindows order), with app name and state."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwm = ctypes.WinDLL("dwmapi")
    from services.windows_automation import WindowsBackend
    backend = WindowsBackend()
    foreground = int(user32.GetForegroundWindow() or 0)
    found: list[dict[str, Any]] = []
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, GW_OWNER, DWMWA_CLOAKED = -20, 0x80, 0x40000, 4, 14
    user32.GetWindowLongW.restype = ctypes.c_long

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(handle, _):
        if not user32.IsWindowVisible(handle) or user32.GetWindowTextLengthW(handle) <= 0:
            return True
        style = user32.GetWindowLongW(handle, GWL_EXSTYLE)
        owned = bool(user32.GetWindow(handle, GW_OWNER))
        if (style & WS_EX_TOOLWINDOW or owned) and not style & WS_EX_APPWINDOW:
            return True
        cloaked = ctypes.c_int(0)
        dwm.DwmGetWindowAttribute(handle, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True                                     # suspended UWP / other virtual desktop: no button here
        title = backend._title(int(handle))[:120]
        if title in _NOT_APPS:
            return True
        process = backend._process_name(int(handle))
        found.append({"handle": int(handle), "title": title, "process": process, "app": app_name(process, title),
                      "minimized": bool(user32.IsIconic(handle)), "foreground": int(handle) == foreground})
        return True

    user32.EnumWindows(collect, 0)
    return found[:60]


def screen_summary(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and a short spoken summary from ``taskbar_windows()`` output."""
    apps: dict[str, list[dict[str, Any]]] = {}
    for window in windows:
        apps.setdefault(window["app"], []).append(window)
    parts = []
    for name, group in apps.items():
        label = name if len(group) == 1 else f"{name} ({len(group)} windows)"
        if len(group) == 1 and group[0]["title"] and group[0]["title"] != name:
            label += f": {group[0]['title'][:60]}"
        if all(w["minimized"] for w in group):
            label += " (minimised)"
        parts.append(label)
    front = next((w for w in windows if w["foreground"]), None)
    summary = (f"{len(apps)} apps are open on the taskbar ({len(windows)} windows): " + "; ".join(parts) + "."
               if windows else "No app windows are open on the taskbar right now.")
    if front is not None:
        summary += f" In front: {front['app']}" + (f" ({front['title'][:60]})." if front["title"] != front["app"] else ".")
    return {"ok": True, "apps": len(apps), "windows": len(windows), "names": list(apps), "summary": summary[:900]}


def screen_info(lister: Callable[[], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    if lister is None and sys.platform != "win32":
        return {"ok": False, "error": "unsupported"}
    try:
        return screen_summary((lister or taskbar_windows)())
    except Exception:
        return {"ok": False, "error": "failed"}
