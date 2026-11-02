"""JupiterWT-only Windows process-loopback bridge.

The helper uses the Windows process-loopback exclusion mode, so the root
Jupiter process and its WebView/audio children never enter the captured mix.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


MINIMUM_PROCESS_LOOPBACK_BUILD = 20348
MAX_BUFFER_BYTES = 16_000 * 2 * 3
MAX_AUDIO_DOWNLOAD_BYTES = 64 * 1024 * 1024


class SystemAudioCaptureBridge:
    def __init__(self) -> None:
        self._system_audio_process: subprocess.Popen | None = None
        self._system_audio_reader: threading.Thread | None = None
        self._system_audio_buffer = bytearray()
        self._system_audio_lock = threading.Lock()

    @staticmethod
    def _windows_build() -> int:
        if os.name != "nt":
            return 0
        try:
            return int(sys.getwindowsversion().build)
        except (AttributeError, TypeError, ValueError):
            return 0

    @staticmethod
    def _source_path() -> Path:
        return Path(__file__).resolve().parent / "native" / "JupiterSystemAudioCapture.cs"

    @staticmethod
    def _runtime_helper_path() -> Path:
        base = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "Jupiter" / "Native"
        return base / "JupiterSystemAudioCapture.exe"

    @staticmethod
    def _compiler_path() -> Path | None:
        windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
        candidates = (
            windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
            windows / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
        )
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    def _ensure_helper(self) -> tuple[Path | None, str]:
        source = self._source_path()
        helper = self._runtime_helper_path()
        if not source.is_file():
            return None, "Jupiter's Windows loopback helper source is missing."
        if helper.is_file() and helper.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return helper, ""
        compiler = self._compiler_path()
        if compiler is None:
            return None, "The Windows .NET Framework compiler required for Jupiter's audio helper is unavailable."
        helper.parent.mkdir(parents=True, exist_ok=True)
        temporary = helper.with_suffix(".tmp.exe")
        completed = subprocess.run(
            [
                str(compiler),
                "/nologo",
                "/optimize+",
                "/platform:x64",
                f"/out:{temporary}",
                str(source),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if completed.returncode != 0 or not temporary.is_file():
            temporary.unlink(missing_ok=True)
            return None, "Jupiter's Windows process-loopback helper could not be prepared."
        os.replace(temporary, helper)
        return helper, ""

    def get_system_audio_capability(self):
        build = self._windows_build()
        if build < MINIMUM_PROCESS_LOOPBACK_BUILD:
            return {
                "available": False,
                "windows_build": build,
                "reason": f"Requires Windows build {MINIMUM_PROCESS_LOOPBACK_BUILD} or newer for process-loopback exclusion.",
            }
        helper, reason = self._ensure_helper()
        return {
            "available": helper is not None,
            "windows_build": build,
            "api": "WASAPI ActivateAudioInterfaceAsync process loopback",
            "exclusion": "PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE",
            "reason": reason,
        }

    def _read_system_audio(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            while process.poll() is None:
                chunk = stream.read(4096)
                if not chunk:
                    break
                with self._system_audio_lock:
                    self._system_audio_buffer.extend(chunk)
                    overflow = len(self._system_audio_buffer) - MAX_BUFFER_BYTES
                    if overflow > 0:
                        del self._system_audio_buffer[:overflow]
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def start_system_audio_capture(self):
        active = self._system_audio_process
        if active is not None and active.poll() is None:
            return {"active": True, "root_pid": os.getpid()}
        capability = self.get_system_audio_capability()
        if not capability.get("available"):
            return {"active": False, "reason": capability.get("reason")}
        helper = self._runtime_helper_path()
        with self._system_audio_lock:
            self._system_audio_buffer.clear()
        process = subprocess.Popen(
            [str(helper), str(os.getpid())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        time.sleep(0.18)
        if process.poll() is not None:
            return {"active": False, "reason": "Windows process-loopback activation failed."}
        self._system_audio_process = process
        self._system_audio_reader = threading.Thread(
            target=self._read_system_audio,
            args=(process,),
            name="JupiterSystemAudioReader",
            daemon=True,
        )
        self._system_audio_reader.start()
        return {
            "active": True,
            "root_pid": os.getpid(),
            "format": "pcm_s16le;rate=16000;channels=1",
            "exclusion": "current_jupiter_process_tree",
        }

    def read_system_audio_chunk(self, max_bytes=65_536):
        process = self._system_audio_process
        active = process is not None and process.poll() is None
        limit = max(2, min(int(max_bytes or 65_536), 65_536))
        limit -= limit % 2
        with self._system_audio_lock:
            size = min(limit, len(self._system_audio_buffer))
            chunk = bytes(self._system_audio_buffer[:size])
            if size:
                del self._system_audio_buffer[:size]
        return {
            "active": active,
            "pcm_base64": base64.b64encode(chunk).decode("ascii") if chunk else "",
        }

    def stop_system_audio_capture(self):
        process = self._system_audio_process
        self._system_audio_process = None
        with self._system_audio_lock:
            self._system_audio_buffer.clear()
        if process is None:
            return {"active": False}
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=1.2)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
        return {"active": False}

    def shutdown_system_audio(self) -> None:
        self.stop_system_audio_capture()

    @staticmethod
    def _download_account_key(account_scope) -> str:
        normalized = str(account_scope or "").strip().casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if "@" in normalized else ""

    @staticmethod
    def _download_location_store_path() -> Path:
        configured = (
            os.getenv("JUPITER_WEBVIEW_PROFILE_DIR", "").strip()
            or os.getenv("JUPITER_WEBVIEW_DATA_DIR", "").strip()
            or os.getenv("WEBVIEW2_USER_DATA_FOLDER", "").strip()
            or os.getenv("JUPITER_RUNTIME_DIR", "").strip()
        )
        if configured:
            root = Path(configured)
        elif os.getenv("JUPITER_ENV", "").strip().casefold() == "test":
            root = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "JupiterTest"
        else:
            root = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "Jupiter"
        return root / "jupiter-download-locations.json"

    @classmethod
    def _read_download_locations(cls) -> dict[str, str]:
        try:
            data = json.loads(cls._download_location_store_path().read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in data.items() if isinstance(value, str)} if isinstance(data, dict) else {}
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}

    @classmethod
    def _write_download_locations(cls, locations: dict[str, str]) -> None:
        path = cls._download_location_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(locations, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _default_download_folder() -> Path:
        return Path.home() / "Downloads"

    @classmethod
    def _download_folder_for_account(cls, account_scope) -> Path:
        key = cls._download_account_key(account_scope)
        selected = cls._read_download_locations().get(key, "") if key else ""
        path = Path(selected) if selected else cls._default_download_folder()
        return path if path.is_absolute() else cls._default_download_folder()

    def get_download_location(self, account_scope):
        folder = self._download_folder_for_account(account_scope)
        return {"status": "ready", "label": folder.name or "Downloads", "custom": folder != self._default_download_folder()}

    def choose_download_folder(self, account_scope):
        key = self._download_account_key(account_scope)
        if os.name != "nt" or not key:
            return {"status": "unavailable"}
        try:
            from System.Windows.Forms import DialogResult, FolderBrowserDialog

            dialog = FolderBrowserDialog()
            dialog.Description = "Choose where Jupiter saves downloads"
            current = self._download_folder_for_account(account_scope)
            if current.is_dir():
                dialog.SelectedPath = str(current)
            if dialog.ShowDialog() != DialogResult.OK:
                return {"status": "cancelled"}
            selected = Path(str(dialog.SelectedPath))
            if not selected.is_absolute() or not selected.is_dir():
                return {"status": "error"}
            locations = self._read_download_locations()
            locations[key] = str(selected)
            self._write_download_locations(locations)
            return {"status": "selected", "label": selected.name or str(selected)}
        except Exception:
            return {"status": "error"}

    @staticmethod
    def _available_download_path(folder: Path, filename: str) -> Path:
        candidate = folder / filename
        if not candidate.exists():
            return candidate
        stem, suffix = candidate.stem, candidate.suffix
        for index in range(2, 1000):
            alternate = folder / f"{stem}_{index}{suffix}"
            if not alternate.exists():
                return alternate
        raise OSError("No available download filename")

    def save_audio_base64(self, filename, mime_type, encoded_audio, behavior="ask", account_scope=""):
        """Save an authenticated MP3 through the selected Windows download policy."""

        if os.name != "nt" or str(mime_type or "").casefold() != "audio/mpeg":
            return {"status": "unavailable"}
        safe_name = Path(str(filename or "Jupiter_Audio.mp3")).name
        safe_name = "".join(character for character in safe_name if character not in '<>:"/\\|?*').strip(" .")
        if not safe_name.casefold().endswith(".mp3"):
            safe_name = f"{safe_name or 'Jupiter_Audio'}.mp3"
        try:
            encoded = str(encoded_audio or "")
            if not encoded or len(encoded) > ((MAX_AUDIO_DOWNLOAD_BYTES * 4) // 3) + 8:
                return {"status": "invalid_audio"}
            payload = base64.b64decode(encoded, validate=True)
            if not payload or len(payload) > MAX_AUDIO_DOWNLOAD_BYTES:
                return {"status": "invalid_audio"}

            if str(behavior or "ask").casefold() == "automatic":
                folder = self._download_folder_for_account(account_scope)
                folder.mkdir(parents=True, exist_ok=True)
                self._available_download_path(folder, safe_name).write_bytes(payload)
                return {"status": "saved"}

            from System.Windows.Forms import DialogResult, SaveFileDialog

            dialog = SaveFileDialog()
            dialog.Title = "Save Jupiter MP3"
            dialog.Filter = "MP3 audio (*.mp3)|*.mp3"
            dialog.DefaultExt = "mp3"
            dialog.AddExtension = True
            dialog.OverwritePrompt = True
            dialog.FileName = safe_name
            dialog.InitialDirectory = str(self._download_folder_for_account(account_scope))
            if dialog.ShowDialog() != DialogResult.OK:
                return {"status": "cancelled"}
            Path(str(dialog.FileName)).write_bytes(payload)
            return {"status": "saved"}
        except Exception:
            return {"status": "error"}
