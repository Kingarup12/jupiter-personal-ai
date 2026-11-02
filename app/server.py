import uvicorn
import mimetypes
from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import os
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Load secure defaults from .env without replacing explicit launcher/service
# environment. Jupiter WT relies on its process environment to select the
# isolated test data directory and trusted test-only OTP policy.
load_dotenv(override=False)
# Staging/WT-only secrets (e.g. a Vault-approved TAVILY_API_KEY under test): a git-ignored file beside this tree,
# loaded only when the runtime is not production, so a test key never reaches the production server.
if os.getenv("JUPITER_ENV", "").strip().lower() == "test":
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.staging.local"),
                override=False)

from router.api import router as chat_router

# Snapshot at import: an old process cannot advertise newly edited Live code.
# Development health exposes only an opaque digest, never local paths/secrets.
import hashlib
import re
from pathlib import Path as FilePath
_source_root = FilePath(__file__).resolve().parents[1]
try:
    _frontend_text = (_source_root / 'windows_app/frontend_version.js').read_text(encoding='utf-8')
except OSError:
    _frontend_text = ''  # Packaged hosts need not ship Python source files.
_frontend_match = re.search(r"JUPITER_FRONTEND_VERSION\s*=\s*['\"]([^'\"]+)", _frontend_text)
_frontend_marker = _frontend_match.group(1) if _frontend_match else 'unknown'
_live_runtime_digest = hashlib.sha256()
for _runtime_name in (
        'services/gemini_live_transport.py', 'services/live_voice_router.py',
        'services/voice_pipeline.py', 'windows_app/app.js', 'windows_app/live_voice_policy.js',
):
    try:
        _live_runtime_digest.update(_runtime_name.encode() + b'\0' + (_source_root / _runtime_name).read_bytes())
    except OSError:
        _live_runtime_digest.update(_runtime_name.encode() + b'\0unavailable')
_live_runtime_marker = _live_runtime_digest.hexdigest()[:16]

app = FastAPI(title="Jupiter AI Backend")


@app.middleware("http")
async def prevent_frontend_document_staleness(request: Request, call_next):
    """Always revalidate the shell document while retaining versioned assets.

    Android WebView opens a stable /mobile/ URL.  Without an HTTP cache policy
    on that HTML navigation, WebView may keep an old document (and therefore
    old asset-version references) after the app is closed and reopened.  The
    JavaScript/CSS files remain safely cacheable because their URLs include the
    canonical frontend version.
    """
    response = await call_next(request)
    path = request.url.path.rstrip("/")
    if path in {"/mobile", "/windows"} or path in {"/mobile/index.html", "/windows/index.html"}:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    if path in {"/api/releases/latest", "/api/presence/summary", "/api/server/load", "/api/admin/stats"}:
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

# Allow CORS for local dev (Windows app and Android WebView)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Jupiter-TTS-Provider",
        "X-Jupiter-TTS-Model",
        "X-Jupiter-TTS-Language",
        "X-Jupiter-TTS-Latency-Ms",
        "X-Jupiter-TTS-Streaming",
        "X-Jupiter-TTS-Voice",
        "X-Jupiter-TTS-Voice-Mode",
        "X-Jupiter-TTS-Cache",
        "X-Jupiter-TTS-Cache-Key",
        "X-Jupiter-TTS-Duration-Ms",
        "X-Jupiter-TTS-Leading-Silence-Ms",
        "X-Jupiter-TTS-Trailing-Silence-Ms",
        "X-Jupiter-TTS-Internal-Silence-Ms",
    ],
)

app.include_router(chat_router, prefix="/api")

# Marker 174: one activity counter (distinct accounts with work in progress) spans every AI request's whole lifetime.
from router.api import activity_user_for_scope as _activity_user_for_scope, _running_task_owners
from services.activity_tracker import ActivityMiddleware, tracker as _activity_tracker
_activity_tracker.set_task_source(_running_task_owners)
app.add_middleware(ActivityMiddleware, resolve_user=_activity_user_for_scope)


@app.on_event("startup")
def start_runtime_metrics():
    from services.runtime_metrics import runtime_metrics
    runtime_metrics.start()


@app.on_event("shutdown")
def stop_runtime_metrics():
    from services.runtime_metrics import runtime_metrics
    runtime_metrics.stop()


@app.on_event("startup")
def start_jupiter_call_dispatcher():
    """Marker 172: expire unanswered Jupiter calls, create due reminder calls and send free push (when configured)."""
    if os.getenv("JUPITER_CALL_DISPATCHER", "on").strip().lower() != "off":
        from services.call_push import dispatcher
        dispatcher.start()

# Mount static directories
windows_app_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'windows_app')
# Android WebView is stricter than desktop browsers about font delivery.  Make
# the locally bundled icon fonts explicit rather than relying on a host-wide
# mimetypes database entry.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

app.mount("/windows", StaticFiles(directory=windows_app_path, html=True), name="windows")
# Android opens this route after its lightweight launcher has discovered the public
# server. Keeping one maintained client prevents Windows and Android from drifting.
app.mount("/mobile", StaticFiles(directory=windows_app_path, html=True), name="mobile")
from fastapi.responses import FileResponse

# Absolute so the APK routes do not depend on the process working directory.
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'downloads')

# The single latest Android release. Bump this when a new APK is published; the
# versioned URL for every previous release keeps serving its own immutable binary.
LATEST_ANDROID_VERSION = "1.0.84"

APK_MEDIA_TYPE = "application/vnd.android.package-archive"


def _apk_response(filename: str, download_name: str, cache_headers: dict) -> FileResponse:
    """Serve an APK from the downloads directory with correct install headers."""
    path = os.path.join(DOWNLOADS_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="APK not found")
    headers = {"X-Content-Type-Options": "nosniff"}
    headers.update(cache_headers)
    return FileResponse(
        path=path,
        media_type=APK_MEDIA_TYPE,
        filename=download_name,
        headers=headers,
    )


# One versioned URL == one immutable APK binary, forever.
@app.get('/downloads/Jupiter-{version}.apk')
def download_versioned_apk(version: str = Path(..., pattern=r'^\d+\.\d+\.\d+$')):
    name = f"Jupiter-{version}.apk"
    return _apk_response(name, name, {"Cache-Control": "public, max-age=31536000, immutable"})


@app.get('/downloads/app-release.apk')
def download_release_apk():
    # Latest/compatibility alias only - never cached, always the newest binary.
    return _apk_response(
        "app-release.apk",
        f"Jupiter-{LATEST_ANDROID_VERSION}.apk",
        {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get('/api/version')
def get_version():
    # Retired fake OTA endpoint. Older clients stop polling on a non-OK result;
    # actual frontend cache and native package versions have separate authorities.
    return JSONResponse(status_code=410, content={"status": "retired", "frontend_version_url": "/windows/frontend_version.js"})


@app.get('/api/releases/latest')
def latest_release():
    """Separate the live shared frontend from the actually published Android package."""
    from services.release_updates import published_android_release
    try:
        source = (_source_root / 'windows_app/frontend_version.js').read_text(encoding='utf-8')
        match = re.search(r"JUPITER_FRONTEND_VERSION\s*=\s*['\"]([^'\"]+)", source)
        version = match.group(1) if match else None
    except OSError:
        version = None
    return JSONResponse({
        "frontend_version": version,
        "android": published_android_release(FilePath(DOWNLOADS_DIR), LATEST_ANDROID_VERSION),
    }, headers={"Cache-Control": "no-store, max-age=0"})

@app.get('/health')
def health_check():
    """Small, dependency-free readiness probe used by the desktop launcher."""
    runtime = 'test' if os.getenv('JUPITER_ENV', '').strip().lower() == 'test' else 'production'
    # WT launchers require an explicit trusted identity before reusing a test
    # backend.  A legacy test process without this field is therefore never
    # mistaken for the current WT runtime; production keeps the stable health
    # contract and simply reports no test identity.
    runtime_id = os.getenv('JUPITER_RUNTIME_ID', '').strip()
    result = {'status': 'ok', 'runtime': runtime, 'runtime_id': runtime_id}
    if runtime == 'test' and runtime_id == 'windows-wt':
        result['wt_instance'] = os.getenv('JUPITER_WT_BACKEND_INSTANCE', '')
    if runtime_id == 'windows-source':
        result['source_instance'] = os.getenv('JUPITER_SOURCE_BACKEND_INSTANCE', '')
    if runtime_id in {'windows-source', 'windows-wt'}:
        result.update(frontend_version=_frontend_marker, live_runtime=_live_runtime_marker)
    return result

@app.get('/')
def read_root(request: Request, code: str = "", state: str = "", error: str = ""):
    # If Google OAuth redirected to the root URL (loopback fallback) instead of
    # /api/capabilities/gmail/callback, complete authorization and return to the UI.
    if code or state or error:
        from router.api import gmail_oauth_callback
        return gmail_oauth_callback(request=request, state=state, code=code, error=error)
    from fastapi.responses import HTMLResponse
    return HTMLResponse('<h1>Jupiter Server Online</h1><br><a href="/windows/">Windows App</a><br><a href="/mobile/">Mobile App</a>')

# The packaged desktop client (build_app.py) probes 127.0.0.1:8765-8774 and
# reuses any healthy Jupiter backend it finds there before starting its own
# bundle-local one. A bundled backend resolves its data directory inside the
# PyInstaller extraction folder, so it sees an empty account store and reports
# every registered account as unknown. Serving the maintained backend on that
# port as well keeps the installed client attached to the real account store and
# the live frontend, without needing a new client binary.
PRIMARY_PORT = int(os.environ.get("JUPITER_SERVER_PORT", "8000"))
DESKTOP_CLIENT_PORT = int(os.environ.get("JUPITER_DESKTOP_CLIENT_PORT", "8765"))


def _listening_socket(host: str, port: int):
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(2048)
    listener.set_inheritable(True)
    return listener


def serve() -> None:
    sockets = [_listening_socket("0.0.0.0", PRIMARY_PORT)]
    if DESKTOP_CLIENT_PORT and DESKTOP_CLIENT_PORT != PRIMARY_PORT:
        try:
            sockets.append(_listening_socket("127.0.0.1", DESKTOP_CLIENT_PORT))
        except OSError as error:
            # Another process already owns the desktop discovery port. The public
            # server must still come up, so this is reported and not fatal.
            print(f"[jupiter] desktop client port {DESKTOP_CLIENT_PORT} unavailable: {error}")
    # OAuth callback query parameters can contain one-time codes. Keep access
    # logging disabled so those values are never emitted by the HTTP server.
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", access_log=False))
    server.run(sockets=sockets)


if __name__ == "__main__":
    serve()
