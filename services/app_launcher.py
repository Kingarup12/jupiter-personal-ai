"""Marker 199: "open Google Chrome" / "Notepad kholo" on the Windows desktop host.

Only OPENS an installed app the way the Start menu would; it never passes arguments, runs commands or opens arbitrary
paths. Resolution order: a known app (Windows URI or executable name) -> the app's registered App Paths entry -> a
Start-menu shortcut whose name matches. Nothing found -> "not_found" (Jupiter says so; it never pretends).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

# spoken name -> (label, launch candidates). "uri:" entries are Windows protocol links; others are App Paths names.
KNOWN_APPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "chrome": ("Google Chrome", ("chrome.exe",)), "google chrome": ("Google Chrome", ("chrome.exe",)),
    "edge": ("Microsoft Edge", ("msedge.exe", "uri:microsoft-edge:")),
    "microsoft edge": ("Microsoft Edge", ("msedge.exe", "uri:microsoft-edge:")),
    "firefox": ("Firefox", ("firefox.exe",)), "brave": ("Brave", ("brave.exe",)), "opera": ("Opera", ("opera.exe",)),
    "notepad": ("Notepad", ("notepad.exe",)), "calculator": ("Calculator", ("uri:calculator:", "calc.exe")),
    "calc": ("Calculator", ("uri:calculator:", "calc.exe")), "paint": ("Paint", ("mspaint.exe",)),
    "file explorer": ("File Explorer", ("explorer.exe",)), "explorer": ("File Explorer", ("explorer.exe",)),
    "files": ("File Explorer", ("explorer.exe",)), "my files": ("File Explorer", ("explorer.exe",)),
    "settings": ("Windows Settings", ("uri:ms-settings:",)), "windows settings": ("Windows Settings", ("uri:ms-settings:",)),
    "task manager": ("Task Manager", ("taskmgr.exe",)), "terminal": ("Terminal", ("wt.exe",)),
    "command prompt": ("Command Prompt", ("cmd.exe",)), "powershell": ("PowerShell", ("powershell.exe",)),
    "word": ("Word", ("winword.exe",)), "microsoft word": ("Word", ("winword.exe",)),
    "excel": ("Excel", ("excel.exe",)), "microsoft excel": ("Excel", ("excel.exe",)),
    "powerpoint": ("PowerPoint", ("powerpnt.exe",)), "outlook": ("Outlook", ("outlook.exe", "uri:outlookmail:")),
    "spotify": ("Spotify", ("spotify.exe", "uri:spotify:")), "whatsapp": ("WhatsApp", ("uri:whatsapp:",)),
    "teams": ("Microsoft Teams", ("uri:msteams:", "ms-teams.exe")), "zoom": ("Zoom", ("zoom.exe",)),
    "discord": ("Discord", ("uri:discord:",)), "telegram": ("Telegram", ("uri:tg:",)), "vlc": ("VLC", ("vlc.exe",)),
    "vs code": ("Visual Studio Code", ("code.exe",)), "vscode": ("Visual Studio Code", ("code.exe",)),
    "visual studio code": ("Visual Studio Code", ("code.exe",)), "steam": ("Steam", ("uri:steam:",)),
    "camera": ("Camera", ("uri:microsoft.windows.camera:",)), "camera app": ("Camera", ("uri:microsoft.windows.camera:",)),
    "microsoft store": ("Microsoft Store", ("uri:ms-windows-store:",)), "store": ("Microsoft Store", ("uri:ms-windows-store:",)),
    "clock": ("Clock", ("uri:ms-clock:",)), "alarms": ("Clock", ("uri:ms-clock:",)),
    "photos": ("Photos", ("uri:ms-photos:",)), "snipping tool": ("Snipping Tool", ("uri:ms-screenclip:",)),
    "control panel": ("Control Panel", ("control.exe",)),
    "youtube": ("YouTube", ("uri:https://www.youtube.com/",)), "gmail": ("Gmail", ("uri:https://mail.google.com/",)),
    "google": ("Google", ("uri:https://www.google.com/",)),
}
_URI_OK = re.compile(r"^(?:ms-settings:|calculator:|microsoft-edge:|outlookmail:|spotify:|whatsapp:|msteams:|discord:|tg:|"
                     r"steam:|microsoft\.windows\.camera:|ms-windows-store:|ms-clock:|ms-photos:|ms-screenclip:|"
                     r"https://(?:www\.youtube\.com|mail\.google\.com|www\.google\.com)/)")
_NOISE = re.compile(r"\b(?:uninstall|readme|help|documentation|website|license|release notes|manual)\b", re.I)


def normalize(name: str) -> str:
    value = re.sub(r"[^\w+ ]", " ", str(name or "").casefold().replace(".exe", ""))
    value = re.sub(r"\b(?:the|my|app|application|program|software)\b", " ", value)
    return " ".join(value.split())


def known_app(name: str) -> tuple[str, tuple[str, ...]] | None:
    return KNOWN_APPS.get(normalize(name))


def app_path(exe: str) -> str:
    """The registered full path of ``exe`` (App Paths, per-user then machine), "" if none."""
    if sys.platform != "win32":
        return ""
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                value, _ = winreg.QueryValueEx(key, "")
                value = os.path.expandvars(str(value).strip('"'))
                if value and Path(value).exists():
                    return value
        except OSError:
            continue
    return ""


def start_menu_dirs() -> list[Path]:
    return [Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs"]


def find_shortcut(name: str, roots: list[Path] | None = None) -> tuple[str, str]:
    """Best Start-menu shortcut for ``name``: exact name, then starts-with, then all words contained."""
    wanted = normalize(name)
    if not wanted:
        return "", ""
    best: tuple[int, str, str] | None = None
    for root in roots if roots is not None else start_menu_dirs():
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".lnk", ".url"} or _NOISE.search(path.stem):
                continue
            label = normalize(path.stem)
            if not label:
                continue
            if label == wanted:
                rank = 0
            elif label.startswith(wanted + " ") or label.startswith(wanted):
                rank = 1
            elif all(word in label.split() for word in wanted.split()):
                rank = 2
            else:
                continue
            if best is None or (rank, len(label)) < (best[0], len(normalize(best[2]))):
                best = (rank, str(path), path.stem)
    return (best[1], best[2]) if best else ("", "")


def open_app(name: str, *, opener: Callable[[str], Any] | None = None, resolve_path: Callable[[str], str] = app_path,
             shortcut: Callable[[str], tuple[str, str]] = find_shortcut) -> dict[str, Any]:
    """Open an installed app by its spoken name. Returns {"ok", "app", "how"} or {"ok": False, "error", "app"}."""
    spoken = " ".join(str(name or "").split())[:60]
    if not normalize(spoken):
        return {"ok": False, "error": "empty_app", "app": spoken}
    if opener is None:
        if sys.platform != "win32":
            return {"ok": False, "error": "windows_only", "app": spoken}
        opener = os.startfile                                                       # type: ignore[attr-defined]
    known = known_app(spoken)
    if known:
        label, candidates = known
        for candidate in candidates:
            try:
                if candidate.startswith("uri:"):
                    uri = candidate[4:]
                    if _URI_OK.match(uri):
                        opener(uri)
                        return {"ok": True, "app": label, "how": "uri"}
                    continue
                path = resolve_path(candidate)
                if path:
                    opener(path)
                    return {"ok": True, "app": label, "how": "app_path"}
                if candidate.lower() in {"notepad.exe", "calc.exe", "mspaint.exe", "explorer.exe", "taskmgr.exe",
                                         "cmd.exe", "powershell.exe", "control.exe", "wt.exe"}:
                    opener(candidate)                                                # Windows' own apps are on PATH
                    return {"ok": True, "app": label, "how": "system"}
            except OSError:
                continue
        spoken = label                                                               # try its Start-menu entry next
    path = resolve_path(normalize(spoken).replace(" ", "") + ".exe")
    try:
        if path:
            opener(path)
            return {"ok": True, "app": spoken, "how": "app_path"}
        link, label = shortcut(spoken)
        if link:
            opener(link)
            return {"ok": True, "app": label, "how": "start_menu"}
    except OSError:
        return {"ok": False, "error": "open_failed", "app": spoken}
    return {"ok": False, "error": "not_found", "app": spoken}
