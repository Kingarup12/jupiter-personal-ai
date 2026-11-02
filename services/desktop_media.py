"""Marker 197 (Pixie desktop controls): system / per-app volume, media keys and "play X on YouTube/Spotify" for the
Windows desktop host. No extra dependency: Windows Core Audio is called through raw ctypes COM vtables (the same
interfaces pycaw wraps), so the packaged app needs nothing new.

Every function returns a small JSON-safe dict that says what really happened; nothing here claims success it did not
observe (e.g. Spotify can only be opened on its search page — pressing play there stays with the user).
"""

from __future__ import annotations

import ctypes
import re
import sys
import urllib.parse
import urllib.request
from ctypes import wintypes
from typing import Any, Callable

MAX_STEP_PERCENT = 100
MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
SUPPORTED_APPS = ("youtube", "spotify", "youtube music")


# ── Core Audio through ctypes COM ────────────────────────────────────────────────────────────────────────────────
class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        value = text.strip("{}").replace("-", "")
        raw = bytes.fromhex(value)
        guid = cls()
        guid.Data1, guid.Data2, guid.Data3 = int(value[0:8], 16), int(value[8:12], 16), int(value[12:16], 16)
        for index, byte in enumerate(raw[8:]):
            guid.Data4[index] = byte
        return guid


CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
IID_IAudioSessionControl2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
IID_ISimpleAudioVolume = "{87CE5498-68D6-44E5-9215-6DA47EF883D8}"
CLSCTX_ALL = 23
E_RENDER, E_MULTIMEDIA = 0, 1


class _Com:
    """A raw COM pointer with typed vtable calls. Released exactly once."""

    def __init__(self, pointer: ctypes.c_void_p):
        self.ptr = pointer

    def call(self, index: int, restype, *args_and_types):
        types = [ctypes.c_void_p] + [kind for kind, _ in args_and_types]
        values = [self.ptr] + [value for _, value in args_and_types]
        vtable = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        function = ctypes.WINFUNCTYPE(restype, *types)(vtable[index])
        return function(*values)

    def hr(self, index: int, *args_and_types) -> None:
        result = self.call(index, ctypes.c_long, *args_and_types)
        if result < 0:
            raise OSError(f"COM call {index} failed: 0x{result & 0xFFFFFFFF:08X}")

    def query(self, iid: str) -> "_Com":
        out = ctypes.c_void_p()
        guid = GUID.parse(iid)
        self.hr(0, (ctypes.POINTER(GUID), ctypes.byref(guid)), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(out)))
        return _Com(out)

    def release(self) -> None:
        if self.ptr:
            self.call(2, ctypes.c_ulong)
            self.ptr = None


class CoreAudio:
    """Default render endpoint (speakers/headphones) and its audio sessions."""

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("windows_only")
        self._ole = ctypes.WinDLL("ole32")
        self._ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self._ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        self._ole.CoInitializeEx(None, 0x2)                       # COINIT_APARTMENTTHREADED; S_FALSE if already done
        self._held: list[_Com] = []
        enumerator = ctypes.c_void_p()
        clsid, iid = GUID.parse(CLSID_MMDeviceEnumerator), GUID.parse(IID_IMMDeviceEnumerator)
        result = self._ole.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL, ctypes.byref(iid),
                                            ctypes.byref(enumerator))
        if result < 0 or not enumerator.value:
            raise OSError("audio_unavailable")
        self.enumerator = self._keep(_Com(enumerator))
        device = ctypes.c_void_p()
        self.enumerator.hr(4, (ctypes.c_int, E_RENDER), (ctypes.c_int, E_MULTIMEDIA),
                           (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(device)))   # GetDefaultAudioEndpoint
        self.device = self._keep(_Com(device))

    def _keep(self, com: _Com) -> _Com:
        self._held.append(com)
        return com

    def _activate(self, iid: str) -> _Com:
        out = ctypes.c_void_p()
        guid = GUID.parse(iid)
        self.device.hr(3, (ctypes.POINTER(GUID), ctypes.byref(guid)), (wintypes.DWORD, CLSCTX_ALL),
                       (ctypes.c_void_p, None), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(out)))  # Activate
        return self._keep(_Com(out))

    # master volume (IAudioEndpointVolume) ------------------------------------------------------------------------
    def master(self) -> tuple[float, bool]:
        endpoint = self._activate(IID_IAudioEndpointVolume)
        level, mute = ctypes.c_float(), wintypes.BOOL()
        endpoint.hr(9, (ctypes.POINTER(ctypes.c_float), ctypes.byref(level)))          # GetMasterVolumeLevelScalar
        endpoint.hr(15, (ctypes.POINTER(wintypes.BOOL), ctypes.byref(mute)))           # GetMute
        return float(level.value), bool(mute.value)

    def set_master(self, level: float | None = None, mute: bool | None = None) -> None:
        endpoint = self._activate(IID_IAudioEndpointVolume)
        if level is not None:
            endpoint.hr(7, (ctypes.c_float, max(0.0, min(1.0, level))), (ctypes.c_void_p, None))
        if mute is not None:
            endpoint.hr(14, (wintypes.BOOL, bool(mute)), (ctypes.c_void_p, None))

    # per-app sessions (IAudioSessionManager2 -> ISimpleAudioVolume) ---------------------------------------------
    def sessions(self) -> list[tuple[int, _Com]]:
        manager = self._activate(IID_IAudioSessionManager2)
        enumerator = ctypes.c_void_p()
        manager.hr(5, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(enumerator)))    # GetSessionEnumerator
        sessions = self._keep(_Com(enumerator))
        count = ctypes.c_int()
        sessions.hr(3, (ctypes.POINTER(ctypes.c_int), ctypes.byref(count)))
        found = []
        for index in range(count.value):
            control = ctypes.c_void_p()
            sessions.hr(4, (ctypes.c_int, index), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(control)))
            base = self._keep(_Com(control))
            control2 = self._keep(base.query(IID_IAudioSessionControl2))
            pid = wintypes.DWORD()
            try:
                control2.hr(14, (ctypes.POINTER(wintypes.DWORD), ctypes.byref(pid)))   # GetProcessId
            except OSError:
                continue                                                               # multi-process session
            if pid.value:
                found.append((int(pid.value), self._keep(base.query(IID_ISimpleAudioVolume))))
        return found

    @staticmethod
    def session_volume(simple: _Com) -> tuple[float, bool]:
        level, mute = ctypes.c_float(), wintypes.BOOL()
        simple.hr(4, (ctypes.POINTER(ctypes.c_float), ctypes.byref(level)))
        simple.hr(6, (ctypes.POINTER(wintypes.BOOL), ctypes.byref(mute)))
        return float(level.value), bool(mute.value)

    @staticmethod
    def set_session(simple: _Com, level: float | None = None, mute: bool | None = None) -> None:
        if level is not None:
            simple.hr(3, (ctypes.c_float, max(0.0, min(1.0, level))), (ctypes.c_void_p, None))
        if mute is not None:
            simple.hr(5, (wintypes.BOOL, bool(mute)), (ctypes.c_void_p, None))

    def close(self) -> None:
        for com in reversed(self._held):
            try:
                com.release()
            except Exception:
                pass
        self._held.clear()


def process_name(pid: int) -> str:
    """Executable name of a process (e.g. "spotify.exe"), "" if it cannot be read."""
    if sys.platform != "win32":
        return ""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))                 # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value.replace("/", "\\").rsplit("\\", 1)[-1].casefold()
        return ""
    finally:
        kernel32.CloseHandle(handle)


_APP_ALIASES = {
    "chrome": ("chrome.exe",), "google chrome": ("chrome.exe",), "edge": ("msedge.exe",),
    "microsoft edge": ("msedge.exe",), "firefox": ("firefox.exe",), "brave": ("brave.exe",),
    "spotify": ("spotify.exe",), "discord": ("discord.exe",), "zoom": ("zoom.exe",), "teams": ("ms-teams.exe", "teams.exe"),
    "vlc": ("vlc.exe",), "whatsapp": ("whatsapp.exe", "whatsapp.root.exe"), "youtube": ("chrome.exe", "msedge.exe",
    "firefox.exe", "brave.exe"), "jupiter": ("jupiter.exe", "jupiterwt.exe"), "apple music": ("applemusic.exe",),
}


def app_process_names(target_app: str) -> tuple[str, ...]:
    key = " ".join(str(target_app or "").casefold().replace(".exe", "").split())
    if not key:
        return ()
    return _APP_ALIASES.get(key, (f"{key.replace(' ', '')}.exe",))


def _apply_change(level: float, delta_percent: float | None, set_percent: float | None) -> float:
    if set_percent is not None:
        return max(0.0, min(1.0, float(set_percent) / 100.0))
    return max(0.0, min(1.0, level + float(delta_percent or 0) / 100.0))


def adjust_system_volume(delta_percent: float | None = None, target_app: str = "", *, set_percent: float | None = None,
                         mute: bool | None = None, audio_factory: Callable[[], Any] | None = None,
                         name_of: Callable[[int], str] = process_name) -> dict[str, Any]:
    """Change the speakers' master volume, or ONE app's own volume in the Windows mixer.

    ``delta_percent`` is relative (+20 / -10), ``set_percent`` absolute (0-100), ``mute`` True/False. Returns
    {"ok", "scope": "system"|"app", "app", "before", "after", "muted"} with whole percentages, or {"ok": False,
    "error": ...} — for an app that is not playing sound right now the error is "app_not_playing"."""
    if delta_percent is not None:
        delta_percent = max(-MAX_STEP_PERCENT, min(MAX_STEP_PERCENT, float(delta_percent)))
    if set_percent is not None:
        set_percent = max(0.0, min(100.0, float(set_percent)))
    try:
        audio = (audio_factory or CoreAudio)()
    except Exception:
        return {"ok": False, "error": "audio_unavailable"}
    try:
        app = str(target_app or "").strip()
        if not app:
            before, muted = audio.master()
            after = _apply_change(before, delta_percent, set_percent)
            unmute = False if (mute is None and after > before and muted) else mute   # "volume up" also unmutes
            audio.set_master(level=after if (delta_percent is not None or set_percent is not None) else None, mute=unmute)
            final, final_muted = audio.master()
            return {"ok": True, "scope": "system", "app": "", "before": round(before * 100), "after": round(final * 100),
                    "muted": final_muted}
        names = set(app_process_names(app))
        matched = [(pid, simple) for pid, simple in audio.sessions() if name_of(pid) in names]
        if not matched:
            return {"ok": False, "error": "app_not_playing", "scope": "app", "app": app}
        before_levels = []
        for _pid, simple in matched:
            level, _muted = audio.session_volume(simple)
            before_levels.append(level)
            new_level = _apply_change(level, delta_percent, set_percent)
            audio.set_session(simple, level=new_level if (delta_percent is not None or set_percent is not None) else None,
                              mute=mute)
        level, muted = audio.session_volume(matched[0][1])
        return {"ok": True, "scope": "app", "app": app, "before": round(before_levels[0] * 100),
                "after": round(level * 100), "muted": muted, "sessions": len(matched)}
    except Exception:
        return {"ok": False, "error": "audio_failed"}
    finally:
        try:
            audio.close()
        except Exception:
            pass


def press_media_key(action: str, *, sender: Callable[[int], None] | None = None) -> dict[str, Any]:
    """Play/pause, next, previous or stop for whatever is playing (the same as the keyboard's media keys)."""
    key = MEDIA_KEYS.get(str(action or ""))
    if key is None:
        return {"ok": False, "error": "unknown_media_action"}
    if sender is None:
        if sys.platform != "win32":
            return {"ok": False, "error": "windows_only"}
        user32 = ctypes.WinDLL("user32")
        user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_size_t]

        def sender(vk: int) -> None:
            user32.keybd_event(vk, 0, 0x1, 0)          # KEYEVENTF_EXTENDEDKEY
            user32.keybd_event(vk, 0, 0x1 | 0x2, 0)    # | KEYEVENTF_KEYUP
    sender(key)
    return {"ok": True, "action": action}


# Marker 215: Windows' own media sessions (the volume flyout's controls) know whether something is playing and can
# pause / play it exactly. A fixed PowerShell script; only "pause" or "play" is passed in.
_MEDIA_SESSION_SCRIPT = r"""
param([string]$Want, [string]$App = '')
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(5000) | Out-Null; $t.Result }
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
$manager = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
$sessions = @($manager.GetSessions())
if ($App) { $named = @($sessions | Where-Object { $_.SourceAppUserModelId -match [regex]::Escape($App) }) } else { $named = @() }
if ($Want -eq 'pause') {
  $targets = @($sessions | Where-Object { "$($_.GetPlaybackInfo().PlaybackStatus)" -eq 'Playing' })
  if ($App) { $targets = @($targets | Where-Object { $_.SourceAppUserModelId -match [regex]::Escape($App) }) }
} else {
  # Marker 218 (WT test report 26 Oct: "resume" told the CURRENT session - a browser tab - to play; Spotify stayed
  # paused and Jupiter said it resumed): the app the user named, else a paused Spotify, else the current session.
  $paused = { param($x) "$($x.GetPlaybackInfo().PlaybackStatus)" -ne 'Playing' }
  if ($named.Count) { $targets = @($named | Where-Object { & $paused $_ } | Select-Object -First 1) }
  else {
    $spotify = @($sessions | Where-Object { $_.SourceAppUserModelId -match 'Spotify' -and (& $paused $_) })
    $current = $manager.GetCurrentSession()
    if ($spotify.Count) { $targets = @($spotify[0]) }
    elseif ($current -and (& $paused $current)) { $targets = @($current) } else { $targets = @() }
  }
}
"SESSIONS|$($sessions.Count)"
foreach ($s in $targets) {
  if ($Want -eq 'pause') { $ok = Await ($s.TryPauseAsync()) ([bool]) } else { $ok = Await ($s.TryPlayAsync()) ([bool]) }
  Start-Sleep -Milliseconds 1200
  "DONE|$($s.SourceAppUserModelId)|$ok|$($s.GetPlaybackInfo().PlaybackStatus)"
}
"""


def control_media_session(want: str, *, runner: Callable[..., str] | None = None, app: str = "") -> dict[str, Any]:
    """Pause everything that is playing, or play the current media session if it is paused. {"ok", "changed", "app",
    "method": "media_session"}; ok False when Windows' media sessions cannot be read (the caller uses the media key)."""
    if want not in {"pause", "play"}:
        return {"ok": False, "error": "unknown_media_action"}
    if runner is None:
        if sys.platform != "win32":
            return {"ok": False, "error": "windows_only"}

        def runner(choice: str, app_hint: str = "") -> str:                    # pragma: no cover - needs Windows
            import subprocess
            hint = re.sub(r"[^A-Za-z]", "", app_hint)[:20]
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                   "-Command", f"& {{{_MEDIA_SESSION_SCRIPT}}} -Want {choice} -App '{hint}'"],
                                  capture_output=True, text=True, timeout=15, creationflags=0x08000000)
            return done.stdout or ""
    try:
        out = runner(want, app) if app else runner(want)
    except Exception:
        return {"ok": False, "error": "media_session_failed"}
    lines = [line.strip() for line in str(out).splitlines() if line.strip()]
    if not any(line.startswith("SESSIONS|") for line in lines):
        return {"ok": False, "error": "media_session_failed"}
    done = [line.split("|") for line in lines if line.startswith("DONE|")]
    # Marker 218: "changed" only when Windows then REPORTS the new state (Playing after play, not Playing after pause)
    expected = "playing" if want == "play" else ""
    changed = [parts for parts in done if len(parts) >= 3 and parts[2].strip().casefold() == "true"
               and (len(parts) < 4 or (parts[3].strip().casefold() == "playing") == (want == "play"))]
    del expected
    app = ""
    if changed:
        source = changed[0][1].casefold()
        app = next((label for key, label in (("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"),
                                             ("firefox", "Firefox"), ("vlc", "VLC")) if key in source), "")
    if not app and done:
        source = done[0][1].casefold()
        app = next((label for key, label in (("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"),
                                             ("firefox", "Firefox"), ("vlc", "VLC")) if key in source), "")
    return {"ok": True, "changed": bool(changed), "tried": len(done), "app": app, "method": "media_session",
            "want": want}


# ── play media ───────────────────────────────────────────────────────────────────────────────────────────────────
_VIDEO_ID = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')


def youtube_first_video(query: str, *, fetch: Callable[[str], str] | None = None) -> str:
    """The first video id on YouTube's public results page for ``query`` ("" if none could be read)."""
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})
    try:
        if fetch is None:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
            with urllib.request.urlopen(request, timeout=6) as response:              # noqa: S310 - fixed https host
                page = response.read(1_500_000).decode("utf-8", "replace")
        else:
            page = fetch(url)
    except Exception:
        return ""
    match = _VIDEO_ID.search(page or "")
    return match.group(1) if match else ""


def media_target(query: str, app: str = "youtube", *, fetch: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Where to send the user for ``query`` on ``app``. YouTube opens the first matching video (it starts playing);
    Spotify opens its search for the song in the desktop app (the user presses play — no account access is used)."""
    query = " ".join(str(query or "").split())[:200]
    choice = " ".join(str(app or "youtube").casefold().split()) or "youtube"
    if not query:
        return {"ok": False, "error": "empty_query"}
    if choice == "spotify":
        return {"ok": True, "app": "spotify", "uri": "spotify:search:" + urllib.parse.quote(query), "autoplay": False,
                "fallback": "https://open.spotify.com/search/" + urllib.parse.quote(query)}
    if choice == "youtube music":
        return {"ok": True, "app": "youtube music", "autoplay": False,
                "uri": "https://music.youtube.com/search?" + urllib.parse.urlencode({"q": query})}
    video = youtube_first_video(query, fetch=fetch)
    if video:
        return {"ok": True, "app": "youtube", "uri": f"https://www.youtube.com/watch?v={video}", "autoplay": True}
    return {"ok": True, "app": "youtube", "autoplay": False,
            "uri": "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})}


SPOTIFY_IDLE_TITLES = ("spotify", "spotify free", "spotify premium")


def spotify_play_button(controls: list[dict], query: str = "", *, matching_only: bool = False) -> str | None:
    """Marker 215: the id of the top search result's own "Play <name>" button on Spotify's search page: the first
    "Play <name>" button that is not in the user's library sidebar (``in_library``) and is not the player bar's plain
    "Play". ``controls`` = the page's controls in order, each {"id", "role", "name", "in_library"}.
    Marker 217: a result whose name IS the requested song ("Play Chogada Tara" for "chogada tara") wins over the top
    result ("Play Chogada (From "Loveyatri")")."""
    def plain(value: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", " ", value.casefold()).split())

    buttons = [(str(c.get("id")), plain(str(c.get("name") or "")[5:])) for c in controls
               if c.get("role") == "Button" and str(c.get("name") or "").startswith("Play ")
               and len(str(c.get("name") or "")) > 5 and not c.get("in_library")]
    wanted = plain(query)
    words = [w for w in wanted.split() if len(w) >= 3]
    if words and not any(w in name for w in words for _, name in buttons):
        return None                     # marker 217: these are an OLD search's results, never pressed for a new one
    if wanted:
        for test in (lambda name: name == wanted, lambda name: name.startswith(wanted + " ")):
            match = next((ident for ident, name in buttons if test(name)), None)
            if match:
                return match
    return buttons[0][0] if buttons and not matching_only else None


def spotify_search_page(page: str, controls: list[dict]) -> bool:
    """Marker 217: Spotify's page name is the playing SONG while music plays ("chagronga • noga m sangma"), not
    "Spotify – Search"; the search field's clear button shows the search page is open either way."""
    return "search" in str(page or "").casefold() or any(
        c.get("role") == "Button" and str(c.get("name") or "") == "Clear search field" for c in controls)


def spotify_playing(title: str) -> bool:
    """Spotify's window title is the song ("ADÉLA - Nicole Kidman") while it plays, "Spotify Free" when paused."""
    value = " ".join(str(title or "").split()).casefold()
    return bool(value) and value not in SPOTIFY_IDLE_TITLES


_SAFE_URI = re.compile(r"^(?:https://(?:www\.|music\.)?youtube\.com/|https://open\.spotify\.com/|spotify:search:)")


def play_media(query: str, app: str = "youtube", *, opener: Callable[[str], Any] | None = None,
               fetch: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Open ``query`` on YouTube (plays the first match) or Spotify (search page in the app)."""
    target = media_target(query, app, fetch=fetch)
    if not target.get("ok"):
        return target
    uri = target["uri"]
    if not _SAFE_URI.match(uri):
        return {"ok": False, "error": "unsafe_target"}
    if opener is None:
        import os
        opener = os.startfile                                                          # type: ignore[attr-defined]
    try:
        opener(uri)
    except Exception:
        fallback = target.get("fallback")
        if not fallback:
            return {"ok": False, "error": "open_failed", "app": target["app"]}
        try:
            opener(fallback)
            target = {**target, "uri": fallback, "app": "spotify web"}
        except Exception:
            return {"ok": False, "error": "open_failed", "app": target["app"]}
    return {"ok": True, "app": target["app"], "autoplay": bool(target.get("autoplay")), "query": " ".join(query.split())[:200]}
