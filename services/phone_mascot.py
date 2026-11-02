"""Marker 197: the Jupiter mascot on the phone follows the phone agent's taps (Jarvis workflow HUD).

The desktop host tells the Jupiter Android app over ADB (the same connection ARTEMIS uses) where the agent just
tapped; the app draws a small floating mascot there (JupiterMascotOverlay.kt). The broadcast receiver is protected
by android.permission.DUMP, which only the ADB shell/system hold, so no other app can move or show it. If Jupiter is
not allowed to draw over other apps on the phone, the app ignores the signal and nothing else changes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

ACTION = "com.arupbairagya.jupiter.MASCOT"
PACKAGES = ("com.arupbairagya.jupiter", "com.arupbairagya.jupiter.test.debug")
STATES = ("working", "tap", "done", "hide")
MIN_TAP_INTERVAL_S = 0.4


def adb_path() -> str:
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(variable, "").strip()
        if root:
            candidate = Path(root) / "platform-tools" / ("adb.exe" if sys.platform == "win32" else "adb")
            if candidate.exists():
                return str(candidate)
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
    if local.exists():
        return str(local)
    return shutil.which("adb") or ""


def broadcast_args(package: str, state: str, point: tuple[int, int] | None, serial: str = "") -> list[str]:
    args = (["-s", serial] if serial else []) + ["shell", "am", "broadcast", "-a", ACTION, "-p", package,
                                                 "--es", "state", state]
    if point:
        args += ["--ei", "x", str(int(point[0])), "--ei", "y", str(int(point[1]))]
    return args


class PhoneMascot:
    def __init__(self, *, serial: str = "", adb: str | None = None,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 clock: Callable[[], float] = time.monotonic):
        self.serial, self._adb, self._run, self._clock = serial, adb, runner, clock
        self._packages: tuple[str, ...] | None = None
        self._last_tap = -1e9
        self._lock = threading.Lock()

    def _adb_exe(self) -> str:
        if self._adb is None:
            self._adb = adb_path()
        return self._adb

    def _call(self, args: list[str], timeout: float = 6.0) -> str:
        exe = self._adb_exe()
        if not exe:
            return ""
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            done = self._run([exe, *args], capture_output=True, text=True, timeout=timeout, creationflags=flags)
            return done.stdout or ""
        except Exception:
            return ""

    def packages(self) -> tuple[str, ...]:
        if self._packages is None:
            listing = self._call((["-s", self.serial] if self.serial else []) + ["shell", "pm", "list", "packages",
                                                                                 "com.arupbairagya.jupiter"])
            installed = {line.split(":", 1)[1].strip() for line in listing.splitlines() if line.startswith("package:")}
            self._packages = tuple(p for p in PACKAGES if p in installed)
        return self._packages

    def signal(self, state: str, point: tuple[int, int] | None = None) -> bool:
        """Show / move / hide the mascot. Tap updates are throttled; never raises."""
        if state not in STATES:
            return False
        with self._lock:
            now = self._clock()
            if state == "tap":
                if now - self._last_tap < MIN_TAP_INTERVAL_S:
                    return False
                self._last_tap = now
        sent = False
        for package in self.packages():
            self._call(broadcast_args(package, state, point, self.serial))
            sent = True
        return sent

    def signal_async(self, state: str, point: tuple[int, int] | None = None) -> None:
        threading.Thread(target=self.signal, args=(state, point), name="jupiter-mascot", daemon=True).start()
