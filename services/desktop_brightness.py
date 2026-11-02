"""Marker 214: laptop screen brightness by voice / chat (real WT 24 Oct: "can you increase my brightness" got "I can't
adjust your laptop's screen brightness").

Windows' own WMI brightness class (root/WMI WmiMonitorBrightness / WmiMonitorBrightnessMethods) controls the built-in
laptop panel. External monitors do not expose it: then the honest answer is that this screen's brightness is changed with
its own buttons. Only whole numbers 0-100 ever reach the PowerShell command; nothing the user said is passed to it.
"""

from __future__ import annotations

import subprocess
from typing import Callable

MIN_LEVEL = 5                       # never a black screen by voice ("lowest brightness" = 5 %)
STEP = 20                           # "brighter" / "dim the screen" without a number


def _powershell(script: str) -> str:                                    # pragma: no cover - needs Windows
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                          capture_output=True, text=True, timeout=10, creationflags=0x08000000)   # CREATE_NO_WINDOW
    return (done.stdout or "").strip() if done.returncode == 0 else ""


def read_level(runner: Callable[[str], str] | None = None) -> int | None:
    out = (runner or _powershell)("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
                                  "-ErrorAction Stop | Select-Object -First 1).CurrentBrightness")
    try:
        return max(0, min(100, int(str(out).split()[0])))
    except (ValueError, IndexError):
        return None


def write_level(level: int, runner: Callable[[str], str] | None = None) -> bool:
    level = max(0, min(100, int(level)))
    out = (runner or _powershell)("$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
                                  "-ErrorAction Stop | Select-Object -First 1; Invoke-CimMethod -InputObject $m "
                                  f"-MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness=[byte]{level}}} "
                                  "| Out-Null; 'ok'")
    return out.endswith("ok")


def adjust_brightness(delta_percent: float | None = None, set_percent: float | None = None,
                      runner: Callable[[str], str] | None = None) -> dict:
    """{"ok", "before", "after"} in whole percent, or {"ok": False, "error": "no_brightness_control"|"brightness_failed"}."""
    before = read_level(runner)
    if before is None:
        return {"ok": False, "error": "no_brightness_control"}
    if set_percent is not None:
        target = float(set_percent)
    elif delta_percent is not None:
        target = before + float(delta_percent)
    else:
        return {"ok": True, "before": before, "after": before}
    target = int(round(max(MIN_LEVEL, min(100.0, target))))
    if not write_level(target, runner):
        return {"ok": False, "error": "brightness_failed", "before": before}
    after = read_level(runner)
    return {"ok": True, "before": before, "after": target if after is None else after}
