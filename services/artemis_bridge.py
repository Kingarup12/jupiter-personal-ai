"""Marker 197 (phone agent): Jupiter <-> Google ARTEMIS (github.com/google/artemis, Apache-2.0).

ARTEMIS drives a real Android phone or emulator over ADB like a person (observe the screen, tap, type, swipe) and
exposes it as an MCP server (``python -m mcp_server``, stdio). This module is a small, dependency-free MCP stdio
client for its five tools:

    mobile_run_task          start a background task (profile Flash = fast reactive loop, Pro = planner/operator/
                             checker graph with verification) -> trace_id
    mobile_manage_task       status / stop / inject_instruction for a running task
    mobile_get_device_state  screenshot or OCR+XML hierarchy of the phone right now
    mobile_inspect_trace     what the agent did and why (step trace)
    mobile_diagnose          environment doctor (ADB, devices, credentials, toolchain) with a fix list

ARTEMIS lives OUTSIDE Jupiter's code and data: ``scripts/setup_artemis.py`` installs it into
%LOCALAPPDATA%\\Jupiter\\artemis (override: JUPITER_ARTEMIS_DIR) with its own uv virtual environment. Its model key
stays in ARTEMIS's own .env (the user fills it in privately); Jupiter never reads, prints or forwards it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

PROTOCOL_VERSION = "2025-06-18"
CALL_TIMEOUT_S = 60.0
START_TIMEOUT_S = 45.0
TOOLS = ("mobile_run_task", "mobile_manage_task", "mobile_get_device_state", "mobile_inspect_trace", "mobile_diagnose")
TERMINAL = frozenset({"completed", "failed", "cancelled", "stopped", "error"})
KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPEN_ROUTER_API_KEY",
             "XAI_API_KEY")
DAEMON_PORT = "8791"               # ARTEMIS's optional local daemon (its default 8000 is often a dev server)


def artemis_dir() -> Path:
    configured = os.environ.get("JUPITER_ARTEMIS_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Jupiter" / "artemis"


def artemis_python(root: Path | None = None) -> Path:
    root = root or artemis_dir()
    return root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def has_model_key(env_file: Path) -> bool:
    """True if ARTEMIS's .env (or this process) has a non-empty model key. The value is never returned or logged."""
    if any(os.environ.get(name, "").strip() for name in KEY_NAMES):
        return True
    try:
        lines = env_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False
    for line in lines:
        name, _, value = line.partition("=")
        if name.strip() in KEY_NAMES and value.strip().strip('"').strip("'"):
            return True
    return False


def artemis_status() -> dict[str, Any]:
    """What the page may know: installed, and whether a model key is set (no paths, no key values)."""
    root = artemis_dir()
    installed = (root / "mcp_server").is_dir() and artemis_python(root).exists()
    return {"installed": installed, "configured": installed and has_model_key(root / ".env")}


# ── Flash vs Pro routing ─────────────────────────────────────────────────────────────────────────────────────────
_APPS = re.compile(r"\b(whatsapp|gemini|chatgpt|youtube|spotify|maps|google maps|chrome|gmail|settings|camera|"
                   r"gallery|photos|instagram|telegram|messages|phone|calendar|clock|play store|files|contacts|"
                   r"amazon|flipkart|zomato|swiggy|uber|ola|paytm|phonepe|gpay|calculator|notes|keep)\b", re.I)
_PRO_WORDS = re.compile(r"\b(verify|make sure|confirm that|check (?:that|if|whether)|assert|monitor|keep checking|"
                        r"every \d+|until|loop|logcat|logs?|adb|crash|stability|explore|explor|report|compare|"
                        r"generate|create (?:an? )?(?:image|picture|photo)|download|save (?:it|the)|attach|"
                        r"then send|aur (?:phir|fir)|uske baad|phir|fir)\b", re.I)
_STEP_SPLIT = re.compile(r"\b(?:then|and then|after that|next|finally|phir|fir|uske baad|aur phir)\b|[;,]\s", re.I)


def choose_profile(task: str) -> str:
    """"flash" for a short, deterministic UI path (open Settings, read the battery level); "pro" for cross-app,
    multi-step, verify/monitor or generate-then-share work (planner + operator + checker)."""
    text = str(task or "")
    apps = {match.group(1).casefold() for match in _APPS.finditer(text)}
    steps = len([part for part in _STEP_SPLIT.split(text) if part and part.strip()])
    if len(apps) >= 2 or _PRO_WORDS.search(text) or steps >= 4 or len(text) > 260:
        return "pro"
    return "flash"


# ── MCP stdio client ─────────────────────────────────────────────────────────────────────────────────────────────
class McpError(RuntimeError):
    pass


class McpStdioClient:
    """Newline-delimited JSON-RPC 2.0 over a child process's stdin/stdout (the MCP stdio transport)."""

    def __init__(self, command: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None,
                 popen: Callable[..., Any] = subprocess.Popen, log_path: Path | None = None):
        self._command, self._cwd, self._env, self._popen = command, cwd, env, popen
        self._log_path = log_path
        self._process: Any = None
        self._next_id = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.server_info: dict[str, Any] = {}

    @property
    def running(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def start(self, timeout: float = START_TIMEOUT_S) -> dict[str, Any]:
        if self.running:
            return self.server_info
        flags = 0x08000000 if sys.platform == "win32" else 0                     # CREATE_NO_WINDOW
        stderr = open(self._log_path, "ab") if self._log_path else subprocess.DEVNULL  # noqa: SIM115
        self._process = self._popen(self._command, cwd=self._cwd, env=self._env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=stderr, bufsize=0, creationflags=flags)
        threading.Thread(target=self._read, name="artemis-mcp-reader", daemon=True).start()
        result = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                             "clientInfo": {"name": "jupiter", "version": "197"}}, timeout=timeout)
        self.server_info = result.get("serverInfo") or {}
        self.notify("notifications/initialized")
        return self.server_info

    def _send(self, message: dict[str, Any]) -> None:
        if not self.running:
            raise McpError("server_not_running")
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            self._process.stdin.write(data)
            self._process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = CALL_TIMEOUT_S) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            slot = {"event": threading.Event(), "response": None}
            self._pending[request_id] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            if not slot["event"].wait(timeout):
                raise McpError("timeout")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        response = slot["response"] or {}
        if "error" in response:
            error = response.get("error") or {}
            raise McpError(str(error.get("message") or "mcp_error")[:200])
        return response.get("result") or {}

    def _read(self) -> None:
        stream = self._process.stdout
        for raw in iter(stream.readline, b""):
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue                                                      # stray log line on stdout
            if not isinstance(message, dict) or "id" not in message or "method" in message:
                continue                                                      # notifications / server requests
            with self._lock:
                slot = self._pending.get(message.get("id"))
            if slot is not None:
                slot["response"] = message
                slot["event"].set()
        with self._lock:                                                      # server exited: wake every waiter
            for slot in self._pending.values():
                slot["response"] = {"error": {"message": "server_exited"}}
                slot["event"].set()

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = CALL_TIMEOUT_S) -> dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        payload: Any = result.get("structuredContent")
        if isinstance(payload, dict) and set(payload) == {"result"}:
            payload = payload["result"]
        if payload is None:
            texts = [part.get("text", "") for part in result.get("content") or [] if part.get("type") == "text"]
            joined = "\n".join(texts).strip()
            try:
                payload = json.loads(joined) if joined else {}
            except ValueError:
                payload = {"text": joined[:4000]}
        if result.get("isError"):
            raise McpError(str((payload or {}).get("error") or (payload or {}).get("text") or "tool_error")[:300])
        return payload if isinstance(payload, dict) else {"result": payload}

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


class ArtemisBridge:
    """One ARTEMIS MCP server per Jupiter host session, started on first use."""

    def __init__(self, *, root: Path | None = None, client_factory: Callable[..., McpStdioClient] | None = None):
        self.root = root or artemis_dir()
        self._factory = client_factory or McpStdioClient
        self._client: McpStdioClient | None = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return (self.root / "mcp_server").is_dir() and artemis_python(self.root).exists()

    def _ensure(self) -> McpStdioClient:
        with self._lock:
            if self._client is not None and self._client.running:
                return self._client
            if not self.available():
                raise McpError("artemis_not_installed")
            env = {k: v for k, v in os.environ.items() if not k.startswith("JUPITER_")}  # no Jupiter settings leak in
            env.update(PYTHONUNBUFFERED="1", PYTHONPATH=str(self.root), ARTEMIS_DESKTOP_NOTIFY="false",
                       PYTHONIOENCODING="utf-8")
            env.setdefault("ARTEMIS_DAEMON_PORT", DAEMON_PORT)
            logs = self.root / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            client = self._factory([str(artemis_python(self.root)), "-m", "mcp_server"], cwd=str(self.root), env=env,
                                   log_path=logs / "jupiter-mcp-stderr.log")
            client.start()
            self._client = client
            return client

    def call(self, tool: str, arguments: dict[str, Any], timeout: float = CALL_TIMEOUT_S) -> dict[str, Any]:
        if tool not in TOOLS:
            raise McpError("unknown_tool")
        return self._ensure().call_tool(tool, {k: v for k, v in arguments.items() if v not in (None, "")}, timeout)

    def run_task(self, task: str, *, profile: str = "auto", device_serial: str = "", locked_app_package: str = "",
                 expected_output: str = "", verification_level: str = "") -> dict[str, Any]:
        chosen = choose_profile(task) if profile not in {"flash", "pro"} else profile
        result = self.call("mobile_run_task", {
            "task_desc": str(task)[:4000], "model": "Pro" if chosen == "pro" else "Flash",
            "device_serial": device_serial or None, "locked_app_package": locked_app_package or None,
            "expected_output_desc": (expected_output or None) if chosen == "pro" else None,
            "verification_level": (verification_level or None) if chosen == "pro" else None,
        })
        return {**result, "profile": chosen}

    def status(self, trace_id: str) -> dict[str, Any]:
        return self.call("mobile_manage_task", {"action": "status", "trace_id": trace_id})

    def stop(self, trace_id: str) -> dict[str, Any]:
        return self.call("mobile_manage_task", {"action": "stop", "trace_id": trace_id})

    def inject(self, trace_id: str, instruction: str) -> dict[str, Any]:
        return self.call("mobile_manage_task", {"action": "inject_instruction", "trace_id": trace_id,
                                                "instruction": str(instruction)[:1000]})

    def device_state(self, mode: str = "hierarchy", device_serial: str = "") -> dict[str, Any]:
        return self.call("mobile_get_device_state", {"view_type": mode, "device_serial": device_serial or None},
                         timeout=90)

    def inspect_trace(self, trace_id: str, action: str = "view_summary") -> dict[str, Any]:
        return self.call("mobile_inspect_trace", {"action": action, "trace_id": trace_id}, timeout=90)

    def diagnose(self) -> dict[str, Any]:
        return self.call("mobile_diagnose", {}, timeout=120)

    def shutdown(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()


def task_state(status: dict[str, Any]) -> str:
    """Normalised lifecycle of a mobile_manage_task status payload."""
    return str((status or {}).get("status") or "").casefold()


def progress_text(status: dict[str, Any]) -> str:
    """One short line about what the phone agent is doing now (Flash: latest thought/action; Pro: active step)."""
    progress = (status or {}).get("progress")
    if isinstance(progress, dict):
        for key in ("latest_action", "action", "current_step", "latest_thought", "thought", "active_milestone"):
            value = progress.get(key)
            if isinstance(value, dict):
                value = value.get("description") or value.get("name") or value.get("type")
            if value:
                return " ".join(str(value).split())[:120]
    if isinstance(progress, str):
        return " ".join(progress.split())[:120]
    return ""


# ARTEMIS reports Flash's latest action as text, e.g. "click({'x': 540, 'y': 1210})" or "tap(540, 1210)".
_POINT = re.compile(r"['\"]?\bx['\"]?\s*[:=]\s*(\d{1,4})\D{1,12}?['\"]?\by['\"]?\s*[:=]\s*(\d{1,4})|"
                    r"\b(?:tap|click|press|long_press)\w*\(\s*(\d{1,4})\s*,\s*(\d{1,4})", re.I)


def action_point(status: dict[str, Any]) -> tuple[int, int] | None:
    """Screen point of the agent's latest tap, if the status reports one (used to move the mascot)."""
    progress = (status or {}).get("progress")
    if isinstance(progress, dict):
        for key in ("latest_action", "action"):
            action = progress.get(key)
            if isinstance(action, dict):
                x, y = action.get("x"), action.get("y")
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    return int(x), int(y)
                coordinate = action.get("coordinate") or action.get("coordinates")
                if isinstance(coordinate, (list, tuple)) and len(coordinate) == 2:
                    try:
                        return int(coordinate[0]), int(coordinate[1])
                    except (TypeError, ValueError):
                        pass
    text = str(progress.get("latest_action") or "") if isinstance(progress, dict) else progress_text(status)
    match = _POINT.search(text)
    if not match:
        return None
    x, y = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
    return int(x), int(y)


def wait_for(bridge: ArtemisBridge, trace_id: str, *, poll_s: float = 4.0, timeout_s: float = 900.0,
             on_progress: Callable[[dict[str, Any]], Any] | None = None, cancelled: Callable[[], bool] | None = None,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Poll a task until it ends (or ``cancelled()`` / timeout -> stop it). Returns the last status."""
    started = time.monotonic()
    status: dict[str, Any] = {}
    while True:
        if cancelled and cancelled():
            try:
                bridge.stop(trace_id)
            except Exception:
                pass
            return {**status, "status": "cancelled"}
        try:
            status = bridge.status(trace_id)
        except McpError as error:
            status = {"status": "running", "error": str(error)}
        if on_progress:
            try:
                on_progress(status)
            except Exception:
                pass
        if task_state(status) in TERMINAL:
            return status
        if time.monotonic() - started > timeout_s:
            try:
                bridge.stop(trace_id)
            except Exception:
                pass
            return {**status, "status": "failed", "error": "timeout"}
        sleep(poll_s)
