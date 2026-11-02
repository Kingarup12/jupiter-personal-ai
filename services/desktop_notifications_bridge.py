"""Desktop host side of Jupiter notifications, background mode and the incoming-call toast (marker 222).

The page decides WHAT deserves a notification (a finished reply or a question while Jupiter is not in front, a task
or reminder update, an incoming call); this mixin only shows it through ``services.windows_notify`` and routes a click
back to the page (``window.onJupiterNotificationAction``). Closing the window keeps Jupiter running in the
notification area (like WhatsApp), so calls still ring and notifications still arrive; "Quit Jupiter" in the tray
menu really exits. Start at sign-in launches Jupiter hidden in the tray.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

from services import windows_notify


def launch_activation(argv: list[str]) -> str:
    """The toast-click URI Windows passed on the command line ('jupiter-wt:answer/<id>'), if any."""
    for value in argv[1:]:
        text = str(value).strip().strip('"')
        if re.match(r"^jupiter-(?:wt|wt-autotest|ai):", text):
            return text
    return ""


class DesktopNotificationsBridgeMixin:
    """Mixed into the pywebview js_api bridge. Public methods are callable from the page; _methods are host-only."""

    def _notifications_setup(self, *, main_window, display_name: str, scheme: str, instance_key: str, icon_png: str,
                             icon_ico: str, settings_path: str, run_value: str, run_command: str, protocol_command: str,
                             repaint, log, allow_registration: bool) -> None:
        self._notify_window = main_window
        self._notify_display_name = display_name
        self._notify_scheme = scheme
        self._notify_instance_key = instance_key
        self._notify_icon_png = icon_png
        self._notify_settings_path = settings_path
        self._notify_run_value = run_value
        self._notify_run_command = run_command
        self._notify_repaint = repaint
        self._notify_log = log
        self._notify_quitting = False
        self._notify_hidden_note_shown = False
        self._notify_pending: list[dict] = []
        self._notify_page_ready = False
        self._notify_lock = threading.Lock()
        self._notify_aumid = windows_notify.app_user_model_id(display_name)
        self._notify_host = None
        windows_notify.register_app_user_model_id(self._notify_aumid, display_name, icon_png)   # its own toast identity
        if allow_registration:                         # never for the auto-test app: links and sign-in stay the owner's
            windows_notify.register_protocol(scheme, protocol_command)
        settings = self._notify_settings()
        if allow_registration and settings.get("start_at_sign_in") and run_command:
            windows_notify.set_start_at_sign_in(run_value, run_command, True)     # keeps the command current
        self._notify_tray = windows_notify.TrayIcon(display_name, icon_ico, self._notify_show_window, self._notify_quit,
                                                   instance_key=instance_key, on_message=self._notify_on_message)
        self._notify_tray.start()
        threading.Thread(target=self._notify_start_host, name="jupiter-toast-start", daemon=True).start()
        main_window.events.closing += self._notify_on_closing

    # ── settings ────────────────────────────────────────────────────────────────────────────────────────────────
    def _notify_settings(self) -> dict:
        defaults = {"keep_running": True, "start_at_sign_in": True, "notifications": True}
        try:
            with open(self._notify_settings_path, encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                defaults.update({key: bool(stored[key]) for key in defaults if key in stored})
        except (OSError, ValueError):
            pass
        return defaults

    def _notify_save_settings(self, settings: dict) -> None:
        os.makedirs(os.path.dirname(self._notify_settings_path), exist_ok=True)
        temporary = self._notify_settings_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        os.replace(temporary, self._notify_settings_path)

    def desktop_background_settings(self):
        settings = self._notify_settings()
        packaged = bool(windows_notify.package_family_name())
        return {
            **settings,
            "start_at_sign_in": (settings["start_at_sign_in"] if packaged
                                 else windows_notify.start_at_sign_in_enabled(self._notify_run_value)),
            "start_at_sign_in_managed_by_windows": packaged,
            "toasts_available": bool(self._notify_host and self._notify_host.available),
            "tray": True,
        }

    def set_desktop_background_settings(self, values=None):
        values = values if isinstance(values, dict) else {}
        settings = self._notify_settings()
        for key in ("keep_running", "start_at_sign_in", "notifications"):
            if key in values:
                settings[key] = bool(values[key])
        self._notify_save_settings(settings)
        if "start_at_sign_in" in values and self._notify_run_command:
            windows_notify.set_start_at_sign_in(self._notify_run_value, self._notify_run_command,
                                                settings["start_at_sign_in"])
        return self.desktop_background_settings()

    # ── toasts ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _notify_start_host(self) -> None:
        host = windows_notify.ToastHost(self._notify_aumid, log=self._notify_log)
        if host.start():
            self._notify_host = host
            self._notify_log(f"Windows notifications ready ({self._notify_aumid}).")
        else:
            self._notify_log("Windows notifications unavailable; the page keeps its in-app cards.")

    def _notify_toast(self, tag: str, xml: str, expires_s: int | None = None) -> bool:
        host = self._notify_host
        return bool(host and host.show(tag, xml, expires_s=expires_s))

    def desktop_notify(self, payload=None):
        """{kind, title, body, chat_id, tag}: one Windows notification. Never shown while Jupiter is in front."""
        payload = payload if isinstance(payload, dict) else {}
        if not self._notify_settings().get("notifications", True):
            return {"status": "disabled"}
        if self._notify_window_in_front():
            return {"status": "in_front"}
        title = " ".join(str(payload.get("title") or "Jupiter").split())[:120]
        body = " ".join(str(payload.get("body") or "").split())[:300]
        chat_id = str(payload.get("chat_id") or "")
        chat_id = chat_id if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", chat_id) else ""
        tag = re.sub(r"[^A-Za-z0-9_.-]", "", str(payload.get("tag") or payload.get("kind") or "jupiter"))[:60] or "jupiter"
        launch = (windows_notify.activation_uri(self._notify_scheme, "open_chat", chat_id) if chat_id
                  else windows_notify.activation_uri(self._notify_scheme, "open"))
        shown = self._notify_toast(tag, windows_notify.notice_xml(title, body, launch=launch,
                                                                    icon_path=self._notify_icon_png), expires_s=86400)
        return {"status": "shown" if shown else "unavailable"}

    def desktop_call_toast(self, call_id="", reason="", active=True):
        """Incoming call: a toast with Answer / Decline that stays while it rings, even with Jupiter in the tray."""
        call_id = str(call_id or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", call_id):
            return {"status": "invalid"}
        tag = f"call-{call_id[:40]}"
        if not active:
            host = self._notify_host
            return {"status": "removed" if host and host.remove(tag) else "unavailable"}
        if self._notify_window_in_front():
            return {"status": "in_front"}                 # the in-app call card is already in front of the user
        shown = self._notify_toast(tag, windows_notify.call_xml(call_id, str(reason or ""), scheme=self._notify_scheme,
                                                                icon_path=self._notify_icon_png), expires_s=120)
        return {"status": "shown" if shown else "unavailable"}

    def desktop_take_pending_action(self):
        """The page asks once it has loaded: a toast click that started Jupiter is delivered now."""
        with self._notify_lock:
            self._notify_page_ready = True
            pending, self._notify_pending = self._notify_pending, []
        return pending

    # ── window / activation ─────────────────────────────────────────────────────────────────────────────────────
    def _notify_hwnd(self):
        try:
            return int(self._notify_window.native.Handle.ToInt64())
        except Exception:
            return 0

    def _notify_window_in_front(self) -> bool:
        if sys.platform != "win32":
            return False
        import ctypes
        hwnd = self._notify_hwnd()
        user32 = ctypes.windll.user32
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return False
        return int(user32.GetForegroundWindow() or 0) == hwnd

    def _notify_show_window(self) -> None:
        window = self._notify_window
        try:
            window.show()
            window.restore()
        except Exception:
            pass
        if sys.platform == "win32":
            import ctypes
            hwnd = self._notify_hwnd()
            if hwnd:
                user32 = ctypes.windll.user32
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)                            # SW_RESTORE
                user32.SetForegroundWindow(hwnd)                          # the user clicked: bringing it forward is right
        try:
            self._notify_repaint(window)
        except Exception:
            pass

    def _notify_on_message(self, text: str) -> None:
        """A second launch or a toast click (forwarded by the new process through the tray window)."""
        text = str(text or "")
        if text == "background":
            return
        action = windows_notify.parse_activation(text) or {"action": "open"}
        if action.get("action") != "decline":          # declining a call from the toast leaves Jupiter where it was
            self._notify_show_window()
        self._notify_dispatch(action)

    def _notify_dispatch(self, action: dict) -> None:
        if action.get("action") == "open":
            return
        with self._notify_lock:
            if not self._notify_page_ready:
                self._notify_pending.append(action)
                return
        try:
            self._notify_window.evaluate_js(
                f"window.onJupiterNotificationAction && window.onJupiterNotificationAction({json.dumps(action)})")
        except Exception as error:
            self._notify_log(f"notification action not delivered: {type(error).__name__}")

    def _notify_queue_launch_activation(self, text: str) -> None:
        action = windows_notify.parse_activation(text)
        if action and action.get("action") != "open":
            with self._notify_lock:
                self._notify_pending.append(action)

    def _notify_on_closing(self):
        """Closing hides Jupiter in the tray (calls and notifications keep working) unless the user chose Quit."""
        if self._notify_quitting or not self._notify_settings().get("keep_running", True):
            return True
        threading.Thread(target=self._notify_hide_window, daemon=True).start()
        return False

    def _notify_hide_window(self) -> None:
        time.sleep(0.05)
        try:
            self._notify_window.hide()
        except Exception:
            return
        if not self._notify_hidden_note_shown:
            self._notify_hidden_note_shown = True
            self._notify_toast("background", windows_notify.notice_xml(
                f"{self._notify_display_name} is still running",
                "Calls and notifications keep working. Open or quit Jupiter from the icon in the notification area.",
                launch=windows_notify.activation_uri(self._notify_scheme, "open"),
                icon_path=self._notify_icon_png, silent=True), expires_s=600)

    def _notify_quit(self) -> None:
        self._notify_quitting = True
        try:
            self._notify_window.destroy()
        except Exception:
            pass

    def _notify_shutdown(self) -> None:
        try:
            self._notify_tray.stop()
        except Exception:
            pass
        host, self._notify_host = self._notify_host, None
        if host:
            host.stop()
