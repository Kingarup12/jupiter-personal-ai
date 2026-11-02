"""Marker 209: everyday browser tasks without touching the keyboard or mouse.

Real WT 23 Oct: "open a new tab" -> "Sorry, main directly browser mein naya tab nahi khol sakti. Aap manually open kar
lijiye." The user: Jupiter should do these things like ChatGPT/Claude agents can. Opening a tab, a website or a Google
search only needs the browser's own command line (the same as clicking a link):
- the browser the user named, else the browser that already has a window open, else the Windows default browser;
- a web address opens as a NEW TAB in that browser's existing window (measured 23 Oct, Chrome and Edge);
- "new tab": Edge/Firefox open their real New Tab page (edge://newtab / about:newtab, measured); Chrome opens its
  internal page in a new WINDOW, so Chrome gets Google in a new tab instead.
Only http(s) addresses are ever passed; nothing is typed, clicked or run. Anything more (close a tab, fill a form,
click a link) goes to computer control, which asks before it uses the mouse and keyboard.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.parse import quote_plus, urlsplit

BROWSERS = {
    "edge": ("Microsoft Edge", "msedge.exe", "edge://newtab"),
    "chrome": ("Google Chrome", "chrome.exe", "https://www.google.com/"),
    "firefox": ("Firefox", "firefox.exe", "about:newtab"),
    "brave": ("Brave", "brave.exe", "https://search.brave.com/"),
    "opera": ("Opera", "opera.exe", "https://www.google.com/"),
}
_ALIASES = {"microsoft edge": "edge", "msedge": "edge", "google chrome": "chrome", "mozilla firefox": "firefox",
            "mozilla": "firefox"}
_PROGIDS = (("msedge", "edge"), ("chrome", "chrome"), ("firefox", "firefox"), ("brave", "brave"), ("opera", "opera"))
SITES = {
    "youtube": "https://www.youtube.com/", "google": "https://www.google.com/", "gmail": "https://mail.google.com/",
    "wikipedia": "https://www.wikipedia.org/", "amazon": "https://www.amazon.in/", "flipkart": "https://www.flipkart.com/",
    "facebook": "https://www.facebook.com/", "instagram": "https://www.instagram.com/", "twitter": "https://x.com/",
    "linkedin": "https://www.linkedin.com/", "github": "https://github.com/", "netflix": "https://www.netflix.com/",
    "chatgpt": "https://chatgpt.com/", "claude": "https://claude.ai/", "gemini": "https://gemini.google.com/",
    "google maps": "https://www.google.com/maps", "maps": "https://www.google.com/maps",
    "google drive": "https://drive.google.com/", "drive": "https://drive.google.com/",
    "whatsapp web": "https://web.whatsapp.com/", "reddit": "https://www.reddit.com/", "stack overflow": "https://stackoverflow.com/",
    "outlook": "https://outlook.live.com/", "spotify": "https://open.spotify.com/", "hotstar": "https://www.hotstar.com/",
    "prime video": "https://www.primevideo.com/", "canva": "https://www.canva.com/", "notion": "https://www.notion.so/",
}
_DOMAIN = re.compile(r"^(?:https?://)?([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]{1,63})+)(/[^\s]*)?$", re.I)


def browser_key(name: str) -> str:
    value = " ".join(str(name or "").casefold().split())
    value = _ALIASES.get(value, value)
    return value if value in BROWSERS else ""


def safe_url(value: str) -> str:
    """An http(s) URL for a site name / domain / URL, or "" (never file:, javascript:, data: or local paths)."""
    raw = " ".join(str(value or "").split()).strip(" .,'\"")
    if not raw:
        return ""
    site = SITES.get(raw.casefold())
    if site:
        return site
    match = _DOMAIN.match(raw)
    if not match:
        return ""
    url = raw if re.match(r"^https?://", raw, re.I) else "https://" + raw
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or "." not in parts.hostname:
        return ""
    return url[:2000]


def search_url(query: str, engine: str = "google") -> str:
    q = " ".join(str(query or "").split())[:300]
    if engine == "youtube":
        return f"https://www.youtube.com/results?search_query={quote_plus(q)}"
    if engine == "bing":
        return f"https://www.bing.com/search?q={quote_plus(q)}"
    return f"https://www.google.com/search?q={quote_plus(q)}"


def _foreground_window() -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def default_browser() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as key:
            progid = str(winreg.QueryValueEx(key, "ProgId")[0]).casefold()
    except OSError:
        return ""
    return next((name for needle, name in _PROGIDS if needle in progid), "")


class BrowserActions:
    """``windows`` = services.window_control.WindowControl (which browsers have a window), ``launcher`` runs
    [exe_path, url]; both injectable for tests."""

    def __init__(self, *, windows: Any = None, launcher: Callable[[list[str]], Any] | None = None,
                 resolve_path: Callable[[str], str] | None = None, default: Callable[[], str] = default_browser,
                 verify: bool | None = None, verify_s: float = 6.0):
        self._windows = windows
        # Marker 218: the real browser is checked after launching; tests with a fake launcher opt in explicitly.
        self._verify = (launcher is None) if verify is None else bool(verify)
        self._verify_s = verify_s
        self._launch = launcher or (lambda argv: subprocess.Popen(argv, close_fds=True))
        if resolve_path is None:
            from services.app_launcher import app_path
            resolve_path = app_path
        self._path = resolve_path
        self._default = default

    def pick(self, named: str = "") -> str:
        key = browser_key(named)
        if key:
            return key
        if self._windows is not None:
            try:
                open_ones = [w for w in self._windows._list() if any(w.exe == BROWSERS[k][1] for k in BROWSERS)]
            except Exception:
                open_ones = []
            for window in open_ones:                                     # z-order: the most recently used first
                key = next((k for k, v in BROWSERS.items() if v[1] == window.exe), "")
                if key:
                    return key
        return self._default() or "edge"

    def _browser_titles(self, exe: str) -> dict[int, str] | None:
        """{hwnd: title} of this browser's windows (None = cannot be checked, e.g. in tests without windows)."""
        if self._windows is None or not self._verify:
            return None
        try:
            return {w.hwnd: w.title for w in self._windows._list() if w.exe == exe}
        except Exception:
            return None

    def _changed(self, exe: str, before: dict[int, str], expected: str = "") -> bool:
        """A new window of this browser, or the browser window IN FRONT with a new title, or a title that newly shows
        what was asked ("New tab", the site). Other windows' own title updates (a playing video's counter, a chat
        tab's unread count) never count."""
        deadline = time.monotonic() + self._verify_s
        pattern = re.compile(expected, re.I) if expected else None
        old_titles = set(before.values())
        while True:
            now = self._browser_titles(exe) or {}
            front = _foreground_window()
            if any(hwnd not in before for hwnd in now) \
                    or (front in now and before.get(front) is not None and before[front] != now[front]) \
                    or (pattern and any(pattern.search(t) and t not in old_titles for t in now.values())):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        request = request if isinstance(request, dict) else {}
        action = str(request.get("action") or "")
        if action == "new_tab":
            url = ""
        elif action == "open_url":
            url = safe_url(str(request.get("url") or ""))
            if not url:
                return {"ok": False, "error": "unsafe_url"}
        elif action == "search":
            query = " ".join(str(request.get("query") or "").split())
            if not query:
                return {"ok": False, "error": "empty_query"}
            url = search_url(query, str(request.get("engine") or "google"))
        else:
            return {"ok": False, "error": "unknown_action"}
        key = self.pick(str(request.get("browser") or ""))
        label, exe, new_tab = BROWSERS[key]
        path = self._path(exe)
        if not path:
            return {"ok": False, "error": "browser_not_found", "browser": label}
        target = url or new_tab
        argv = [path, "-new-tab", target] if key == "firefox" else [path, target]
        before = self._browser_titles(exe)
        try:
            self._launch(argv)
        except OSError:
            return {"ok": False, "error": "open_failed", "browser": label}
        expected = (r"new\s*tab|about:blank|start\s*page" if action == "new_tab"
                    else re.escape((urlsplit(url).hostname or "").removeprefix("www.").split(".")[0]) if url else "")
        if before is not None and not self._changed(exe, before, expected):
            # Marker 218 (WT test report 25 Oct: "Open Microsoft Edge in a new blank tab" -> said done, nothing seen)
            return {"ok": False, "error": "browser_no_change", "browser": label}
        return {"ok": True, "browser": label, "action": action, "url": url,
                "site": (urlsplit(url).hostname or "") if url else ""}
