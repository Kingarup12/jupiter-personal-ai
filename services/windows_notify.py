"""Real Windows notifications, a tray icon and start-at-sign-in for the Jupiter desktop host (marker 222).

Like ChatGPT / Claude Code / WhatsApp on Windows:
- ``ToastHost``: Windows toast notifications (Action Center) for "Jupiter replied", "Jupiter has a question", task and
  reminder updates, and an incoming-call toast with Answer / Decline. One hidden PowerShell process owns every toast
  for the whole session (no extra Python packages; the Store build stays the same size) and reports clicks back on
  stdout. Unpackaged builds register their own AppUserModelID under HKCU so Windows shows "Jupiter", not PowerShell.
- ``TrayIcon``: a notification-area icon (Open Jupiter / Quit Jupiter) so closing the window keeps Jupiter running in
  the background, where it can still notify and ring.
- ``set_start_at_sign_in``: HKCU Run entry (unpackaged builds); the Store package uses its manifest StartupTask.

Nothing here reads chats or sends anything off the machine: the page decides what to show, this module only draws it.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape, quoteattr

TOAST_GROUP = "jupiter"
CREATE_NO_WINDOW = 0x08000000

_HOST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$aumid = $args[0]
try {
  $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($aumid)
  [Console]::Out.WriteLine('READY|' + $notifier.Setting)
} catch { [Console]::Out.WriteLine('FAILED|' + $_.Exception.HResult); exit 1 }
[Console]::Out.Flush()
$history = [Windows.UI.Notifications.ToastNotificationManager]::History
while ($null -ne ($line = [Console]::In.ReadLine())) {
  try {
    $message = $line | ConvertFrom-Json
    if ($message.op -eq 'show') {
      $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
      $xml.LoadXml([string]$message.xml)
      $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
      $toast.Tag = [string]$message.tag
      $toast.Group = 'jupiter'
      if ($message.expires_s) { $toast.ExpirationTime = [DateTimeOffset]::Now.AddSeconds([double]$message.expires_s) }
      $notifier.Show($toast)
      [Console]::Out.WriteLine('SHOWN|' + $message.tag)
    } elseif ($message.op -eq 'remove') {
      try { $history.Remove([string]$message.tag, 'jupiter', $aumid) } catch {}
      [Console]::Out.WriteLine('REMOVED|' + $message.tag)
    } elseif ($message.op -eq 'count') {
      [Console]::Out.WriteLine('COUNT|' + @($history.GetHistory($aumid)).Count)
    }
  } catch { [Console]::Out.WriteLine('ERROR|' + $_.Exception.Message.Replace("`n", ' ')) }
  [Console]::Out.Flush()
}
"""


# ── identity ───────────────────────────────────────────────────────────────────────────────────────────────────────
def package_family_name() -> str:
    """The MSIX package family name when this process runs inside the Store package, else ''."""
    if sys.platform != "win32":
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        length = ctypes.c_uint32(0)
        if kernel32.GetCurrentPackageFamilyName(ctypes.byref(length), None) != 122:   # ERROR_INSUFFICIENT_BUFFER
            return ""
        buffer = ctypes.create_unicode_buffer(length.value)
        if kernel32.GetCurrentPackageFamilyName(ctypes.byref(length), buffer) != 0:
            return ""
        # Only Jupiter's own Store package counts: the WT runs on the Microsoft Store Python, whose package identity
        # (PythonSoftwareFoundation.Python...) would otherwise label every toast "Python" and skip our registration.
        return buffer.value if buffer.value.startswith("ArupBairagya.") else ""
    except Exception:
        return ""


def app_user_model_id(display_name: str) -> str:
    family = package_family_name()
    if family:
        return f"{family}!Jupiter"                      # AppxManifest <Application Id="Jupiter">
    if "auto-test" in display_name.lower():
        return "ArupBairagya.JupiterWT.AutoTest"
    return "ArupBairagya.JupiterWT" if "WT" in display_name else "ArupBairagya.JupiterAI.Desktop"


def register_app_user_model_id(aumid: str, display_name: str, icon_path: str) -> bool:
    """Unpackaged apps: HKCU\\Software\\Classes\\AppUserModelId\\<id> gives toasts Jupiter's name and icon."""
    if sys.platform != "win32" or "!" in aumid:          # the Store package already has its identity
        return True
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{aumid}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
            if icon_path and os.path.isfile(icon_path):
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon_path)
            winreg.SetValueEx(key, "ShowInSettings", 0, winreg.REG_DWORD, 1)
        return True
    except OSError:
        return False


# ── toast XML ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _args(**values: str) -> str:
    from urllib.parse import urlencode
    return urlencode({key: str(value) for key, value in values.items() if value not in (None, "")})


ACTIONS = {"open": "", "open_chat": "chat", "open_call": "call", "answer": "call", "decline": "call"}


def activation_uri(scheme: str, action: str, target: str = "") -> str:
    """Toast clicks open ``<scheme>:<action>/<id>`` (no '&' or quotes, so it survives cmd/wscript launchers);
    Windows starts Jupiter with it and the running instance receives it (forward_to_running_instance). Unpackaged apps
    cannot receive toast click events directly from PowerShell."""
    return f"{scheme}:{action}/{target}" if target else f"{scheme}:{action}"


def notice_xml(title: str, body: str, *, launch: str, icon_path: str = "", silent: bool = False) -> str:
    logo = (f'<image placement="appLogoOverride" hint-crop="circle" src={quoteattr(Path(icon_path).as_uri())}/>'
            if icon_path and os.path.isfile(icon_path) else "")
    audio = '<audio silent="true"/>' if silent else '<audio src="ms-winsoundevent:Notification.IM"/>'
    return (f'<toast launch={quoteattr(launch)} activationType="protocol">'
            f'<visual><binding template="ToastGeneric"><text>{escape(title[:120])}</text>'
            f'<text>{escape(body[:300])}</text>{logo}</binding></visual>{audio}</toast>')


def call_xml(call_id: str, reason: str, *, scheme: str, icon_path: str = "") -> str:
    """incomingCall scenario: stays on screen while ringing, Answer / Decline buttons. Silent - the page plays
    Jupiter's own ringtone (and stays quiet in Windows Do Not Disturb), so the toast never doubles the sound."""
    logo = (f'<image placement="appLogoOverride" hint-crop="circle" src={quoteattr(Path(icon_path).as_uri())}/>'
            if icon_path and os.path.isfile(icon_path) else "")
    uri = lambda action: quoteattr(activation_uri(scheme, action, call_id))   # noqa: E731
    return (f'<toast scenario="incomingCall" launch={uri("open_call")} '
            f'activationType="protocol"><visual><binding template="ToastGeneric">'
            f'<text>Jupiter is calling</text><text>{escape((reason or "Incoming Jupiter call")[:200])}</text>{logo}'
            f'</binding></visual><actions>'
            f'<action content="Answer" arguments={uri("answer")} activationType="protocol"/>'
            f'<action content="Decline" arguments={uri("decline")} activationType="protocol"/>'
            f'</actions><audio silent="true"/></toast>')


def parse_activation(arguments: str) -> dict[str, str]:
    """'jupiter-wt:answer/<call id>' -> {'action': 'answer', 'call': '<call id>'}. Only known actions and plain ids
    survive, so a crafted link can never do more than a toast button (answering still needs a ringing call)."""
    import re
    text = str(arguments or "").strip().strip('"')
    if ":" in text:
        text = text.split(":", 1)[1]
    action, _, target = text.strip("/").partition("/")
    if action not in ACTIONS:
        return {}
    key = ACTIONS[action]
    target = target.strip("/")
    if key and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", target):
        return {}
    return {"action": action, **({key: target} if key else {})}


def register_protocol(scheme: str, command: str) -> bool:
    """HKCU URL protocol for toast clicks (unpackaged builds; the Store package declares it in its manifest)."""
    if sys.platform != "win32" or package_family_name():
        return True
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{scheme}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Jupiter notification")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{scheme}\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
        return True
    except OSError:
        return False


TRAY_CLASS = "JupiterTrayWindow"
WM_COPYDATA = 0x004A


def forward_to_running_instance(instance_key: str, text: str) -> bool:
    """Hand a toast click / "show yourself" request to the Jupiter instance already running (its tray window)."""
    if sys.platform != "win32":
        return False
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    hwnd = user32.FindWindowW(TRAY_CLASS, f"Jupiter tray {instance_key}")
    if not hwnd:
        return False

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]

    payload = ctypes.create_unicode_buffer(str(text)[:1000])
    data = COPYDATASTRUCT(1, ctypes.sizeof(payload), ctypes.cast(payload, ctypes.c_void_p))
    result = ctypes.c_size_t(0)
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p,
                                           wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    sent = user32.SendMessageTimeoutW(hwnd, WM_COPYDATA, 0, ctypes.byref(data), 0x0002, 5000, ctypes.byref(result))
    return bool(sent)


# ── toast host process ─────────────────────────────────────────────────────────────────────────────────────────────
class ToastHost:
    def __init__(self, aumid: str, *, log=None):
        self.aumid = aumid
        self.log = log or (lambda message: None)
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.ready = threading.Event()
        self.available = False
        self.setting = ""

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        # Inline (-EncodedCommand), never a script file: the Store-Python WT redirects AppData writes into its package
        # cache, where a separately started PowerShell cannot see them.
        import base64
        script = _HOST_SCRIPT.replace("$aumid = $args[0]", "$aumid = " + "'" + self.aumid.replace("'", "''") + "'")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            self._process = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", encoded],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as error:
            self.log(f"toast host unavailable: {type(error).__name__}")
            return False
        threading.Thread(target=self._read, name="jupiter-toast-host", daemon=True).start()
        self.ready.wait(15)
        return self.available

    def _read(self) -> None:
        process = self._process
        for raw in process.stdout:                           # type: ignore[union-attr]
            line = raw.strip()
            kind, _, rest = line.partition("|")
            if kind == "READY":
                self.available, self.setting = True, rest
                self.ready.set()
            elif kind == "FAILED":
                self.ready.set()
            elif kind == "ERROR":
                self.log(f"toast host error: {rest[:160]}")
        self.available = False
        self.ready.set()

    def _send(self, message: dict) -> bool:
        with self._lock:
            process = self._process
            if not self.available or process is None or process.poll() is not None or process.stdin is None:
                return False
            try:
                process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                process.stdin.flush()
                return True
            except OSError:
                return False

    def show(self, tag: str, xml: str, *, expires_s: int | None = None) -> bool:
        return self._send({"op": "show", "tag": tag[:60], "xml": xml, **({"expires_s": expires_s} if expires_s else {})})

    def remove(self, tag: str) -> bool:
        return self._send({"op": "remove", "tag": tag[:60]})

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            try:
                process.stdin.close()                       # type: ignore[union-attr]
                process.wait(3)
            except Exception:
                process.kill()


# ── tray icon ──────────────────────────────────────────────────────────────────────────────────────────────────────
class TrayIcon:
    """Notification-area icon on its own message-loop thread. Left click opens Jupiter; right click shows a menu."""

    WM_USER, WM_COMMAND, WM_DESTROY, WM_CLOSE = 0x0400, 0x0111, 0x0002, 0x0010
    WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
    NIM_ADD, NIM_DELETE, NIF_MESSAGE, NIF_ICON, NIF_TIP = 0, 2, 1, 2, 4
    ID_OPEN, ID_QUIT = 1001, 1002

    def __init__(self, tooltip: str, icon_path: str, on_open: Callable[[], None], on_quit: Callable[[], None], *,
                 instance_key: str = "", on_message: Callable[[str], None] | None = None):
        self.tooltip = tooltip[:120]
        self.icon_path = icon_path
        self.on_open = on_open
        self.on_quit = on_quit
        self.instance_key = instance_key
        self.on_message = on_message or (lambda text: None)
        self._hwnd = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if sys.platform == "win32" and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="jupiter-tray", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, self.WM_CLOSE, 0, 0)

    def _run(self) -> None:
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT]
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                          wintypes.HWND, wintypes.LPVOID]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
                        ("hBalloonIcon", wintypes.HICON)]

        callback_message = self.WM_USER + 20

        def show_menu(hwnd):
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, 0, self.ID_OPEN, "Open Jupiter")
            user32.AppendMenuW(menu, 0x800, 0, None)                     # MF_SEPARATOR
            user32.AppendMenuW(menu, 0, self.ID_QUIT, "Quit Jupiter")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)                              # required so the menu closes on outside click
            user32.TrackPopupMenu(menu, 0x0020, point.x, point.y, 0, hwnd, None)   # TPM_RIGHTBUTTON
            user32.DestroyMenu(menu)

        def wndproc(hwnd, message, wparam, lparam):
            if message == callback_message:
                if lparam in (self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK):
                    threading.Thread(target=self.on_open, daemon=True).start()
                elif lparam == self.WM_RBUTTONUP:
                    show_menu(hwnd)
                return 0
            if message == self.WM_COMMAND:
                command = wparam & 0xFFFF
                if command == self.ID_OPEN:
                    threading.Thread(target=self.on_open, daemon=True).start()
                elif command == self.ID_QUIT:
                    threading.Thread(target=self.on_quit, daemon=True).start()
                return 0
            if message == WM_COPYDATA and lparam:
                class COPYDATASTRUCT(ctypes.Structure):
                    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]
                copied = COPYDATASTRUCT.from_address(lparam)
                text = ctypes.wstring_at(copied.lpData, copied.cbData // 2).rstrip("\x00") if copied.lpData else ""
                threading.Thread(target=self.on_message, args=(text,), daemon=True).start()
                return 1
            if message == self.WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if message == self.WM_DESTROY:
                shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(data))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)                                  # keep a reference for the thread's life
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=instance, lpszClassName=TRAY_CLASS)
        user32.RegisterClassW(ctypes.byref(window_class))
        # A hidden top-level window (not message-only) so FindWindow from a second launch can reach it.
        hwnd = user32.CreateWindowExW(0, TRAY_CLASS, f"Jupiter tray {self.instance_key}", 0, 0, 0, 0, 0, None, None,
                                      instance, None)
        if not hwnd:
            return
        self._hwnd = hwnd
        icon = user32.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x40) if os.path.isfile(self.icon_path) else None
        if not icon:
            icon = user32.LoadImageW(None, ctypes.c_wchar_p(32512), 1, 0, 0, 0x8000)   # IDI_APPLICATION, LR_SHARED
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = 1
        data.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        data.uCallbackMessage = callback_message
        data.hIcon = icon
        data.szTip = self.tooltip
        shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(data))
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        self._hwnd = None


# ── start at sign-in ───────────────────────────────────────────────────────────────────────────────────────────────
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def start_at_sign_in_enabled(value_name: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, value_name)
            return True
    except OSError:
        return False


def set_start_at_sign_in(value_name: str, command: str, enabled: bool) -> bool:
    """HKCU Run entry for unpackaged builds only (the Store package's StartupTask is managed by Windows Settings)."""
    if sys.platform != "win32" or package_family_name():
        return False
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, value_name)
                except FileNotFoundError:
                    pass
        return start_at_sign_in_enabled(value_name) == enabled
    except OSError:
        return False


def launched_at_sign_in() -> bool:
    """Store StartupTask launches carry no arguments: treat a launch within 3 minutes of Windows start as one."""
    try:
        return ctypes.windll.kernel32.GetTickCount64() < 180_000
    except Exception:
        return False
