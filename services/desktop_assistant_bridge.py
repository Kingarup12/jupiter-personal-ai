"""Marker 197: desktop-host bridge for the Pixie features (notch, volume, media, on-screen annotation) and the
phone agent (ARTEMIS bridge + Jarvis workflow).

Mixed into the pywebview ``js_api`` of the Windows host next to ``ComputerControlBridgeMixin``. Like it, every method
first checks that the caller is the trusted Jupiter page (``_cc_trusted``); there is no network listener. Anything
that reads the screen or drives the phone asks the user in a host-owned native panel the page cannot approve.
"""

from __future__ import annotations

import base64
import secrets
import threading
import time
from typing import Any, Callable

UI_MODES = ("compact", "expanded", "hidden", "minimize", "restore", "expand", "maximize")
SCREEN_CONSENT_TEXT = (
    "Jupiter will take one screenshot of the screen your pointer is on and send it privately to its vision model to "
    "find and mark what you asked about. The screenshot is not saved. Windows that look private (passwords, banking, "
    "vaults, sign-in) are never captured. Allow this for the rest of this Jupiter session?")
CAPTURE_TTL_S = 120.0
FOLLOW_POLL_S = 0.4


class DesktopAssistantBridgeMixin:
    """js_api methods: assistant_*. Requires ``_cc_trusted()``, ``_confirm(title, message)`` and ``_main_window``."""

    _da_notch_factory: Callable[[], Any] | None = None           # tests inject fakes
    _da_overlay_factory: Callable[[], Any] | None = None
    _da_media: Any = None                                        # tests inject a fake services.desktop_media
    _da_capture: Callable[..., Any] | None = None

    def _da(self) -> dict[str, Any]:
        if not hasattr(self, "_da_state"):
            self._da_state = {"lock": threading.RLock(), "notch": None, "overlay": None, "screen_consent": False,
                              "captures": {}, "live": False, "tasks": 0, "follow": None, "explicit_mode": "", "parked": False,
                              "mobile": None, "buddy": "jupiter", "astro": None}
        return self._da_state

    def _da_ok(self) -> bool:
        try:
            return bool(self._cc_trusted())
        except Exception:
            return False

    def _da_media_module(self):
        if self._da_media is None:
            from services import desktop_media
            return desktop_media
        return self._da_media

    # ── notch ───────────────────────────────────────────────────────────────────────────────────────────────────
    def _da_notch(self):
        """The desktop buddy the user chose (Settings > Personalization > Buddy): the Jupiter Buddy notch (default) or
        the 3D Astro Buddy. Both expose the same overlay API, so every caller below works with either."""
        state = self._da()
        if state.get("buddy") == "astro":
            if state["astro"] is None:
                from services.notch_overlay import AstroOverlay, astro_page_url
                state["astro"] = AstroOverlay(page_url=lambda: astro_page_url(self._da_main_url()))
                state["astro"].on_open = self._da_open_jupiter
                state["astro"].on_user_mode = self._da_user_mode
            return state["astro"]
        if state["notch"] is None:
            if self._da_notch_factory is not None:
                state["notch"] = self._da_notch_factory()
            else:
                from services.notch_overlay import NotchOverlay
                state["notch"] = NotchOverlay()
            state["notch"].on_open = self._da_open_jupiter
            state["notch"].on_user_mode = self._da_user_mode
        return state["notch"]

    def _da_main_url(self) -> str:
        try:
            return str(self._main_window.get_current_url() or "")
        except Exception:
            return ""

    def assistant_buddy(self, kind: str = "jupiter") -> dict:
        """Page -> host: which desktop buddy to use ("jupiter" = eyes notch, default; "astro" = 3D astronaut)."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        kind = "astro" if str(kind or "").casefold() == "astro" else "jupiter"
        state = self._da()
        with state["lock"]:
            previous = state.get("buddy", "jupiter")
            if previous == kind:
                return {"ok": True, "buddy": kind}
            old = state["astro"] if previous == "astro" else state["notch"]
            state["buddy"] = kind
        if old is not None:
            try:
                old.set_mode("hidden")
            except Exception:
                pass
        if state["live"] or state["tasks"]:
            self._da_start_follow()
        return {"ok": True, "buddy": kind}

    def _da_user_mode(self, mode: str) -> None:
        """A click/button on the notch: the follow loop must keep that choice (it used to snap an expanded notch
        straight back to compact, which looked like a glitch)."""
        state = self._da()
        with state["lock"]:
            state["explicit_mode"] = "" if mode == "hidden" else mode
        if mode != "hidden" and (state["live"] or state["tasks"] or state["parked"]):
            self._da_start_follow()

    def _da_open_jupiter(self) -> None:
        """The notch's "Open Jupiter" button: the full window comes back (the notch then follows the usual rules)."""
        state = self._da()
        with state["lock"]:
            state["parked"], state["explicit_mode"] = False, ""
        try:
            self._main_window.restore()
            self._main_window.show()
        except Exception:
            pass

    def _da_main_handle(self) -> int:
        try:
            return int(self._main_window.native.Handle.ToInt64())
        except Exception:
            return 0

    def _da_jupiter_in_front(self) -> bool:
        """True while the main Jupiter window is the active, non-minimised window (the notch is not needed then)."""
        handle = self._da_main_handle()
        if not handle:
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            return bool(user32.GetForegroundWindow() == handle and not user32.IsIconic(handle))
        except Exception:
            return False

    def _da_follow(self) -> None:
        """While Live voice / an annotation / a phone task is active: show the notch whenever the user is away from
        the Jupiter window (another app in front or Jupiter minimised), hide it when they come back."""
        state = self._da()
        while True:
            in_front = self._da_jupiter_in_front()
            with state["lock"]:
                if state["parked"] and in_front:
                    state["parked"], state["explicit_mode"] = False, ""          # the user came back to Jupiter
                busy = state["live"] or state["tasks"] > 0 or state["parked"]
                if not busy:
                    state["follow"] = None
                    break
                explicit = state["explicit_mode"]
                # Marker 205: the Astro Buddy is the only buddy on Windows (the Live screen has no second, in-app
                # one there), so it stays at the top of the screen for the whole Live session, Jupiter in front or not.
                always = state.get("buddy") == "astro" and state["live"]
            notch = self._da_notch()
            if explicit:
                wanted = explicit
            else:
                wanted = "hidden" if in_front and not always else ("expanded" if state["tasks"] else "compact")
            if notch.mode != wanted and not (notch.user_hidden and wanted != "hidden"):
                notch.set_mode(wanted)
            time.sleep(FOLLOW_POLL_S)
        try:
            notch = self._da_notch()
            notch.set_mode("hidden")
            notch.reset_user_choice()
        except Exception:
            pass

    def _da_start_follow(self) -> None:
        state = self._da()
        with state["lock"]:
            if state["follow"] is not None:
                return
            state["follow"] = threading.Thread(target=self._da_follow, name="jupiter-notch-follow", daemon=True)
            state["follow"].start()

    def _da_task(self, delta: int) -> None:
        state = self._da()
        with state["lock"]:
            state["tasks"] = max(0, state["tasks"] + delta)
        if delta > 0:
            self._da_start_follow()

    def assistant_notch(self, payload: dict | None = None) -> dict:
        """Page -> notch: Live state, transcript lines, status, checklist. ``live: true/false`` marks Live voice."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        payload = payload if isinstance(payload, dict) else {}
        state = self._da()
        activity = payload.get("activity")
        if activity == "start":
            self._da_task(+1)
        elif activity == "finish":
            # Keep the done/reaction face visible briefly, then let the existing follow loop hide it.
            timer = threading.Timer(1.5, lambda: self._da_task(-1))
            timer.daemon = True
            timer.start()
        if "live" in payload:
            with state["lock"]:
                state["live"] = bool(payload["live"])
                if not state["live"]:
                    state["explicit_mode"] = ""
            if state["live"]:
                self._da_start_follow()
        notch = self._da_notch()
        from services.notch_overlay import reply_reaction
        update = {k: v for k, v in payload.items() if k not in {"live", "mode", "activity"}}
        if activity == "finish" or "jupiter" in payload:
            update["reaction"] = reply_reaction(str(payload.get("jupiter") or ""))
        result = notch.update(update)
        return {"ok": True, **result}

    def assistant_ui_mode(self, mode: str = "compact") -> dict:
        """resize_assistant_ui: compact / expanded / hidden notch; minimize = Jupiter goes to the notch;
        restore = the full Jupiter window comes back."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        mode = str(mode or "").casefold()
        if mode not in UI_MODES:
            return {"ok": False, "error": "unknown_mode"}
        state, notch = self._da(), self._da_notch()
        if mode == "expand":
            # "Expand yourself": the notch grows (even while Jupiter's own window is in front) and stays expanded.
            notch.reset_user_choice()
            notch.set_mode("expanded", user=True)
            with state["lock"]:
                state["explicit_mode"] = "expanded"
            self._da_start_follow()
            return {"ok": True, "mode": "expanded"}
        if mode == "maximize":
            with state["lock"]:
                state["explicit_mode"], state["parked"] = "", False
            try:
                self._main_window.restore()
                self._main_window.maximize()
            except Exception:
                pass
            return {"ok": True, "mode": "maximized"}
        if mode == "restore":
            with state["lock"]:
                state["explicit_mode"], state["parked"] = "", False
            try:
                self._main_window.restore()
            except Exception:
                pass
            return {"ok": True, "mode": "restore"}
        if mode == "minimize":
            with state["lock"]:
                state["explicit_mode"], state["parked"] = "expanded", True
            notch.reset_user_choice()
            try:
                self._main_window.minimize()
            except Exception:
                pass
            notch.set_mode("expanded", user=True)
            self._da_start_follow()
            return {"ok": True, "mode": "minimize"}
        with state["lock"]:
            state["explicit_mode"] = "" if mode == "hidden" else mode
        notch.set_mode(mode, user=True)
        return {"ok": True, "mode": notch.mode}

    # ── volume / media ──────────────────────────────────────────────────────────────────────────────────────────
    def assistant_brightness(self, request: dict | None = None) -> dict:
        """Marker 214: the laptop screen's brightness (Windows' own WMI method; whole percentages only)."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        request = request if isinstance(request, dict) else {}

        def number(key):
            try:
                return None if request.get(key) is None else float(request[key])
            except (TypeError, ValueError):
                return None
        from services.desktop_brightness import adjust_brightness
        return adjust_brightness(number("delta_percent"), number("set_percent"))

    def assistant_volume(self, request: dict | None = None) -> dict:
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        request = request if isinstance(request, dict) else {}

        def number(key):
            try:
                return None if request.get(key) is None else float(request[key])
            except (TypeError, ValueError):
                return None
        mute = request.get("mute")
        return self._da_media_module().adjust_system_volume(
            number("delta_percent"), str(request.get("target_app") or "")[:40], set_percent=number("set_percent"),
            mute=mute if isinstance(mute, bool) else None)

    def assistant_media_key(self, action: str = "play_pause", want: str = "", app: str = "") -> dict:
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        media = self._da_media_module()
        app = str(app or "")[:20]
        if str(want or "") in {"pause", "play"} and hasattr(media, "control_media_session"):
            exact = media.control_media_session(str(want), app=app)   # marker 215: pause / play exactly, never a toggle
            if exact.get("ok"):
                if str(want) == "play" and not exact.get("changed") and (app in {"", "spotify"}) and self._da_spotify_open():
                    # Marker 218: Spotify did not start from Windows' media controls: press Spotify's own Play button
                    if self._da_spotify_resume():
                        return {**exact, "changed": True, "app": "Spotify", "method": "spotify_play_button"}
                return exact
        return media.press_media_key(str(action or ""))

    def _da_spotify_resume(self, wait_s: float = 3.0) -> bool:                  # pragma: no cover - needs Spotify
        import time

        from services.desktop_media import spotify_playing
        from services.windows_automation import WindowsBackend
        backend = WindowsBackend()
        for window in [w for w in backend.list_windows() if w.get("process") == "spotify.exe"]:
            handle = int(window["handle"])
            controls, elements = self._da_spotify_controls(handle)
            button = next((str(c.get("id")) for c in controls if c.get("role") == "Button"
                           and str(c.get("name") or "") == "Play" and not c.get("in_library")), "")
            if button and backend._uia_activate(elements[button]):
                deadline = time.monotonic() + wait_s
                while time.monotonic() < deadline:
                    time.sleep(0.3)
                    if spotify_playing(backend._title(handle)):
                        return True
        return False

    def assistant_screen_info(self) -> dict:
        """Marker 217: which apps / windows are open on the taskbar and which is in front (read-only)."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        from services.desktop_screen_info import screen_info
        return screen_info()

    def assistant_play_media(self, request: dict | None = None) -> dict:
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        request = request if isinstance(request, dict) else {}
        app = str(request.get("app") or "youtube")[:20]
        spotify_was_open = self._da_spotify_open()
        if request.get("app_default") is True and app == "youtube" and spotify_was_open:
            app = "spotify"           # marker 217: "play chogada tara" while Spotify is open plays it IN Spotify
        result = self._da_media_module().play_media(str(request.get("query") or "")[:200], app)
        if result.get("ok") and result.get("app") == "spotify":
            # Marker 215 (real WT 25 Oct: "play any random music on Spotify" only opened Spotify's search and Live said
            # "your music is now playing"): press the top result's own Play button, then check it really plays.
            try:
                # marker 217: a cold-started Spotify needs several seconds before its search results exist
                result = {**result, **self._da_spotify_autoplay(wait_s=9.0 if spotify_was_open else 20.0,
                                                                 query=str(request.get("query") or "")[:200])}
            except Exception:
                result = {**result, "playing": False}
        return result

    @staticmethod
    def _da_spotify_open() -> bool:                                         # pragma: no cover - needs Windows
        try:
            from services.windows_automation import WindowsBackend
            return any(w.get("process") == "spotify.exe" for w in WindowsBackend().list_windows())
        except Exception:
            return False

    def _da_spotify_autoplay(self, wait_s: float = 9.0, query: str = "") -> dict:  # pragma: no cover - needs Spotify
        import time

        from services.desktop_media import spotify_play_button, spotify_playing, spotify_search_page
        from services.windows_automation import WindowsBackend
        backend = WindowsBackend()
        # Marker 217: an old song still playing is not the new one starting: the title must CHANGE to a song
        before = {backend._title(int(w["handle"])) for w in backend.list_windows() if w.get("process") == "spotify.exe"}
        started = time.monotonic()
        deadline = started + wait_s
        clicked = False
        while time.monotonic() < deadline:
            # Spotify may have more than one top-level window ("Spotify" + "Spotify Free"): the one showing the search
            # page is the player (real WT 25 Oct: the first one was checked and nothing was pressed)
            windows = [w for w in backend.list_windows() if w.get("process") == "spotify.exe"]
            window, controls, elements = None, [], {}
            for candidate in windows:
                # marker 217: while a song plays the page is named after the SONG, not "Search"
                found, by_id = self._da_spotify_controls(int(candidate["handle"]))
                if spotify_search_page(self._da_spotify_page(int(candidate["handle"])), found):
                    window, controls, elements = candidate, found, by_id
                    break
            if window is not None:
                # the requested song by name; the top result only once the new results have had time to load
                button = spotify_play_button(controls, query, matching_only=time.monotonic() - started < 2.5)
                song = next((str(c.get("name") or "")[5:] for c in controls if str(c.get("id")) == button), "")
                if button and not backend._uia_activate(elements[button]):
                    button = button if self._da_spotify_click(int(window["handle"]), elements, controls, button) else ""
                if button:
                    for _ in range(10):                                # it plays within a second or two
                        time.sleep(0.4)
                        # Marker 217 (real WT 25 Oct): Spotify Free played ads first and Jupiter said "Samsung Galaxy
                        # is now playing": during an ad the title is the advertiser and the page says "Advertisement"
                        if "advertisement" in self._da_spotify_page(int(window["handle"])):
                            return {"playing": True, "ad": True, "now_playing": song[:120], "autoplay": True}
                        titles = [backend._title(int(w["handle"])) for w in windows]
                        playing = next((t for t in titles if spotify_playing(t) and t not in before), "")
                        if playing:
                            return {"playing": True, "now_playing": playing[:120], "autoplay": True}
                        if _ == 2 and not clicked:
                            # Marker 218 (WT test report 25 Oct): Spotify's React buttons sometimes ignore UI
                            # Automation's Invoke: after 1.2 s without a song, a real double-click on the song's row.
                            clicked = self._da_spotify_click(int(window["handle"]), elements, controls, button)
                    return {"playing": False}
            time.sleep(0.6)
        return {"playing": False}

    @staticmethod
    def _da_spotify_click(handle: int, elements: dict, controls: list, button: str) -> bool:   # pragma: no cover
        """Double-click the middle of the song's row (else the Play button) with one real, tagged click."""
        import ctypes

        from services.input_guard import momentary_click
        index = next((i for i, c in enumerate(controls) if str(c.get("id")) == button), -1)
        song = str(controls[index].get("name") or "")[5:] if index >= 0 else ""
        row = next((str(c.get("id")) for c in reversed(controls[:index]) if c.get("role") in {"Group", "Row", "DataItem"}
                    and str(c.get("name") or "") == song), "") if index >= 0 else ""
        target = elements.get(row) or elements.get(button)
        if target is None:
            return False
        try:
            rect = target.Current.BoundingRectangle
            if rect.Width <= 1 or rect.Height <= 1:
                return False
            ctypes.windll.user32.ShowWindow(handle, 9)                     # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(handle)
            x, y = int(rect.X + rect.Width / 2), int(rect.Y + rect.Height / 2)
            return momentary_click(x, y, double=bool(row))
        except Exception:
            return False

    @staticmethod
    def _da_spotify_page(handle: int) -> str:                       # pragma: no cover - needs Spotify
        """Spotify's current page ("Spotify – Search" while the search results show), lower-case."""
        import clr
        from services.windows_automation import _UIA_STRONG_NAME
        clr.AddReference(f"UIAutomationClient, {_UIA_STRONG_NAME}")
        clr.AddReference(f"UIAutomationTypes, {_UIA_STRONG_NAME}")
        from System import IntPtr
        from System.Windows.Automation import AutomationElement, ControlType, PropertyCondition, TreeScope
        root = AutomationElement.FromHandle(IntPtr(handle))
        document = root.FindFirst(TreeScope.Descendants, PropertyCondition(AutomationElement.ControlTypeProperty,
                                                                           ControlType.Document))
        return str(document.Current.Name or "").casefold() if document is not None else ""

    @staticmethod
    def _da_spotify_controls(handle: int) -> tuple[list[dict], dict]:   # pragma: no cover - needs Spotify
        """Every control of the Spotify window in order (no 250 cap: a long library sidebar pushed the search results
        out of the computer-control view), marking the ones inside the user's library."""
        import clr
        from services.windows_automation import _UIA_STRONG_NAME
        clr.AddReference(f"UIAutomationClient, {_UIA_STRONG_NAME}")
        clr.AddReference(f"UIAutomationTypes, {_UIA_STRONG_NAME}")
        from System import IntPtr
        from System.Windows.Automation import AutomationElement, Condition, TreeScope
        root = AutomationElement.FromHandle(IntPtr(handle))
        library = set()
        found = root.FindAll(TreeScope.Descendants, Condition.TrueCondition)
        for i in range(found.Count):
            info = found[i].Current
            if str(info.Name or "") == "Your Library" and str(info.ControlType.ProgrammaticName).endswith(("DataGrid", "List", "Tree")):
                inside = found[i].FindAll(TreeScope.Descendants, Condition.TrueCondition)
                library = {tuple(inside[j].GetRuntimeId()) for j in range(inside.Count)}
                break
        controls, elements = [], {}
        for i in range(min(found.Count, 3000)):
            element = found[i]
            info = element.Current
            key = f"s{i}"
            elements[key] = element
            controls.append({"id": key, "name": str(info.Name or "")[:120],
                             "role": str(info.ControlType.ProgrammaticName or "").replace("ControlType.", ""),
                             "in_library": tuple(element.GetRuntimeId()) in library})
        return controls, elements

    _da_window_control: Any = None                                # tests inject a fake services.window_control

    def _da_windows(self):
        state = self._da()
        if state.get("windows") is None:
            if self._da_window_control is not None:
                state["windows"] = self._da_window_control
            else:
                from services.window_control import WindowControl
                state["windows"] = WindowControl()
        return state["windows"]

    def assistant_open_app(self, name: str = "", options: dict | None = None) -> dict:
        """"open Google Chrome": opens an installed app like the Start menu would (no arguments, no commands).
        Marker 208: an app that is already open (e.g. minimised) is brought back instead of opening a second window,
        unless the user asked for a new one; a window Jupiter opens is remembered for "close the window you opened"."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        from services.app_launcher import open_app
        options = options if isinstance(options, dict) else {}
        name = str(name or "")[:60]
        windows = self._da_windows()
        if not options.get("new_window"):
            try:
                existing = windows.bring_existing(name, maximize=bool(options.get("maximize")))
            except Exception:
                existing = None
            if existing is not None:
                return existing
        try:
            before = windows.snapshot(name)
        except Exception:
            before = set()
        result = open_app(name)
        if result.get("ok"):
            try:
                hwnd = windows.remember_new(name, before)
                if hwnd and options.get("maximize"):
                    windows.act("maximize", target="last")
                # Marker 218 (WT test report 25 Oct: "Open Calculator" -> "Opened Calculator" and nothing on screen):
                # opened = a visible window of that app exists now (a slow app gets a few more seconds).
                if not hwnd and not self._da_wait_window(windows, name, 6.0):
                    return {"ok": False, "error": "no_window_appeared", "app": result.get("app") or name}
            except Exception:
                pass
        return result

    @staticmethod
    def _da_wait_window(windows, name: str, wait_s: float) -> bool:
        import time
        deadline = time.monotonic() + wait_s
        while True:
            if windows.windows_of(name):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def assistant_calculate(self, request: dict | None = None) -> dict:
        """Marker 218: "open Calculator and calculate 125 times 8" -> Calculator's own buttons, answer from its display."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        from services.desktop_calculator import calculate
        return calculate(str((request or {}).get("expression") or "")[:80])

    def assistant_browser(self, request: dict | None = None) -> dict:
        """Marker 209: a new tab, a website or a search in the user's browser (only an http(s) address is passed to
        the browser's own command line; nothing is typed or clicked)."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        from services.browser_actions import BrowserActions
        return BrowserActions(windows=self._da_windows()).run(request if isinstance(request, dict) else {})

    def assistant_front_window(self, handle: Any = 0) -> dict:
        """Marker 212: the window a computer task runs in comes to the front once, when the task starts."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        try:
            return {"ok": self._da_windows().front(int(handle or 0))}
        except Exception:
            return {"ok": False, "error": "window_failed"}

    def assistant_window(self, request: dict | None = None) -> dict:
        """Marker 208: maximize / minimize / restore / close an app's window or the one Jupiter opened last.
        ``request`` = {"steps": [{"action", "app", "target"}, ...]} (one sentence may ask for two things)."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        request = request if isinstance(request, dict) else {}
        steps = request.get("steps") if isinstance(request.get("steps"), list) else [request]
        windows = self._da_windows()
        results = []
        for step in steps[:3]:
            if not isinstance(step, dict):
                continue
            try:
                results.append(windows.act(str(step.get("action") or ""), str(step.get("app") or "")[:60],
                                           str(step.get("target") or "")))
            except Exception:
                results.append({"ok": False, "error": "window_failed", "action": step.get("action")})
        failed = next((r for r in results if not r.get("ok")), None)
        summary = {"ok": bool(results) and failed is None, "steps": results}
        if failed is not None:                       # the reason the user hears ("I haven't opened a window ...")
            summary.update({k: failed[k] for k in ("error", "app", "count") if failed.get(k) is not None})
        return summary

    # ── screen annotation ───────────────────────────────────────────────────────────────────────────────────────
    def _da_overlay(self):
        state = self._da()
        if state["overlay"] is None:
            if self._da_overlay_factory is not None:
                state["overlay"] = self._da_overlay_factory()
            else:
                from services.screen_annotator import AnnotationOverlay
                state["overlay"] = AnnotationOverlay()
        return state["overlay"]

    def _da_capture_screen(self) -> dict:
        """Privacy check + one capture of the monitor under the pointer. Jupiter steps aside first if it is in
        front, so the screenshot shows the user's work rather than Jupiter itself."""
        if self._da_capture is not None:
            return self._da_capture()
        from services.computer_control import classify_window
        from services.screen_annotator import capture_jpeg, foreground_window, monitor_under_pointer
        overlay = self._da()["overlay"]
        if overlay is not None and overlay.active:
            overlay.clear(fade=False)                              # never "see" Jupiter's own earlier marks
            time.sleep(0.08)
        stepped_aside = False
        if self._da_jupiter_in_front():
            try:
                self._main_window.minimize()
                stepped_aside = True
                time.sleep(0.45)                                   # let the minimise animation finish
            except Exception:
                pass
        _hwnd, title, process = foreground_window()
        blocked = classify_window(title, process)
        if blocked:
            return {"ok": False, "error": blocked, "stepped_aside": stepped_aside}
        monitor = monitor_under_pointer()
        image, size = capture_jpeg(monitor)
        return {"ok": True, "image": image, "size": size, "monitor": monitor, "stepped_aside": stepped_aside}

    def assistant_screen_capture(self, request: str = "") -> dict:
        """Returns {ok, token, image_b64, mime} for the page to send to /api/screen/annotate (PRIVATE route)."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        state = self._da()
        if not state["screen_consent"]:
            if not self._confirm("Let Jupiter look at your screen?", SCREEN_CONSENT_TEXT):
                return {"ok": False, "error": "declined"}
            state["screen_consent"] = True
        try:
            captured = self._da_capture_screen()
        except Exception as error:
            return {"ok": False, "error": "capture_failed", "category": type(error).__name__[:40]}
        if not captured.get("ok"):
            return {k: v for k, v in captured.items() if k in {"ok", "error", "stepped_aside"}}
        token = secrets.token_urlsafe(12)
        now = time.monotonic()
        with state["lock"]:
            state["captures"] = {k: v for k, v in state["captures"].items() if now - v["at"] < CAPTURE_TTL_S}
            state["captures"][token] = {"monitor": tuple(captured["monitor"]), "at": now}
        self._da_task(+1)
        try:
            self._da_notch().update({"state": "thinking", "status": "Looking at your screen"})
        except Exception:
            pass
        return {"ok": True, "token": token, "mime": "image/jpeg", "stepped_aside": bool(captured.get("stepped_aside")),
                "image_b64": base64.b64encode(captured["image"]).decode("ascii"), "size": list(captured["size"])}

    def assistant_screen_draw(self, token: str = "", annotations: Any = None) -> dict:
        """Draw the model's annotations on the monitor that ``token``'s screenshot came from."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        from services.screen_annotator import parse_annotations
        state = self._da()
        with state["lock"]:
            capture = state["captures"].pop(str(token or ""), None)
        if capture is None:
            self._da_task(-1)
            return {"ok": False, "error": "unknown_capture"}
        shapes = parse_annotations(annotations if isinstance(annotations, (dict, list)) else [])
        try:
            notch = self._da_notch()
            notch.update({"state": "done" if shapes else "idle", "status": "Marked on your screen" if shapes else "Nothing to mark"})
        except Exception:
            pass
        if not shapes:
            self._da_task(-1)
            return {"ok": True, "shown": 0}
        try:
            import ctypes
            scale = max(1.0, (ctypes.windll.user32.GetDpiForSystem() or 96) / 96)
        except Exception:
            scale = 1.0
        result = self._da_overlay().show(shapes, capture["monitor"], scale=scale)
        threading.Timer(4.0, lambda: self._da_task(-1)).start()          # the notch lingers briefly, then follows Live
        return {"ok": True, "shown": int(result.get("shown") or 0)}

    def assistant_screen_clear(self) -> dict:
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        overlay = self._da()["overlay"]
        cleared = bool(overlay is not None and overlay.clear())
        return {"ok": True, "cleared": cleared}

    # ── phone agent (ARTEMIS + Jarvis workflow) ─────────────────────────────────────────────────────────────────
    _da_workflow_factory: Callable[..., Any] | None = None

    def _da_mobile(self):
        state = self._da()
        if state["mobile"] is None:
            if self._da_workflow_factory is not None:
                state["mobile"] = self._da_workflow_factory(emit=self._da_mobile_event)
            else:
                from services.artemis_bridge import ArtemisBridge
                from services.jarvis_workflow import JarvisWorkflow
                from services.phone_mascot import PhoneMascot
                mascot = PhoneMascot()
                state["mobile"] = JarvisWorkflow(ArtemisBridge(), emit=self._da_mobile_event,
                                                 mascot=lambda kind, point: mascot.signal_async(kind, point))
        return state["mobile"]

    def _da_mobile_event(self, event: dict) -> None:
        """Workflow progress -> notch (status, detail, checklist) and the Jupiter page (chat line / Live narration)."""
        kind = str(event.get("type") or "")
        notch_payload: dict[str, Any] = {}
        if "checklist" in event:
            notch_payload["checklist"] = event["checklist"]
        if kind in {"workflow_started", "step_started", "step_progress", "step_retry"}:
            notch_payload.update(state="acting", status="Working on your phone")
            if event.get("say"):
                notch_payload["jupiter"] = event["say"]
            if kind == "step_progress":
                notch_payload["detail"] = event.get("detail", "")
        elif kind == "workflow_done":
            notch_payload.update(state="done", status="Done on your phone", detail="")
        elif kind in {"workflow_failed", "workflow_cancelled"}:
            notch_payload.update(state="error" if kind == "workflow_failed" else "idle",
                                 status="Phone task stopped", detail="")
        if notch_payload:
            try:
                self._da_notch().update(notch_payload)
            except Exception:
                pass
        if kind in {"workflow_done", "workflow_failed", "workflow_cancelled"}:
            threading.Timer(6.0, lambda: self._da_task(-1)).start()
        try:
            import json
            self._main_window.evaluate_js("window.__jupiterMobileTaskEvent&&window.__jupiterMobileTaskEvent("
                                          + json.dumps(event) + ")")
        except Exception:
            pass

    def assistant_mobile_start(self, request: dict | None = None) -> dict:
        """Start a phone workflow. ``plan`` comes from /api/mobile/plan (the page fetched it); the user approves the
        exact steps in a host-owned panel before anything touches the phone."""
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        request = request if isinstance(request, dict) else {}
        goal = " ".join(str(request.get("task") or "").split())[:1500]
        from services.artemis_bridge import artemis_status
        from services.jarvis_workflow import normalize_plan, refused_reason
        if not goal:
            return {"ok": False, "error": "empty_task"}
        if refused_reason(goal):
            return {"ok": False, "error": "money_or_secrets"}
        if self._da_workflow_factory is None:
            status = artemis_status()
            if not status["installed"]:
                return {"ok": False, "error": "artemis_not_installed"}
            if not status["configured"]:
                return {"ok": False, "error": "artemis_no_key"}
        workflow = self._da_mobile()
        if workflow.active:
            return {"ok": False, "error": "busy"}
        steps = normalize_plan(request.get("plan"), goal)
        listing = "\n".join(f"{i + 1}. {s.title}" for i, s in enumerate(steps))
        message = (f"Jupiter will use your connected Android phone to do this:\n\n{listing}\n\nIt taps and types on the "
                   "phone like you would. It never enters passwords, OTPs or payment details. Say \"stop\" or press "
                   "Stop at any time.")
        if not self._confirm("Let Jupiter use your phone?", message):
            return {"ok": False, "error": "declined"}
        started = workflow.start(goal, request.get("plan"))
        if not started.get("ok"):
            return started
        self._da_task(+1)
        try:
            self._da_notch().update({"state": "acting", "status": "Working on your phone",
                                     "checklist": started.get("steps") or []})
        except Exception:
            pass
        first = steps[0].say or steps[0].title
        return {"ok": True, "steps": started.get("steps") or [],
                "summary": f"Starting on your phone ({len(steps)} step{'s' if len(steps) != 1 else ''}): {first}"}

    def assistant_mobile_stop(self) -> dict:
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        workflow = self._da()["mobile"]
        return {"ok": True, "stopped": bool(workflow is not None and workflow.stop())}

    def assistant_mobile_status(self) -> dict:
        if not self._da_ok():
            return {"ok": False, "error": "untrusted"}
        workflow = self._da()["mobile"]
        run = getattr(workflow, "run", None)
        if run is None:
            return {"ok": True, "active": False}
        from services.jarvis_workflow import checklist
        return {"ok": True, "active": run.state == "running", "state": run.state, "checklist": checklist(run.steps)}

    def assistant_capabilities(self) -> dict:
        if not self._da_ok():
            return {"ok": False}
        import sys
        from services.artemis_bridge import artemis_status
        return {"ok": True, "notch": True, "volume": sys.platform == "win32", "media": sys.platform == "win32",
                "open_app": sys.platform == "win32", "window": sys.platform == "win32", "buddy": True,
                "browser": sys.platform == "win32", "computer_use": sys.platform == "win32",
                "brightness": sys.platform == "win32",                                        # marker 214
                "annotate": sys.platform == "win32", "mobile": artemis_status()}

    def shutdown_desktop_assistant(self) -> None:
        state = self._da()
        with state["lock"]:
            state["live"], state["tasks"] = False, 0
            notch, overlay, mobile, astro = state["notch"], state["overlay"], state.get("mobile"), state.get("astro")
        for item, method in ((overlay, "close"), (notch, "destroy"), (astro, "destroy"), (mobile, "shutdown")):
            if item is not None:
                try:
                    getattr(item, method)()
                except Exception:
                    pass
