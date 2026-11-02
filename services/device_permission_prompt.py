"""Host-owned, quiet device consent. The requesting page cannot approve itself."""
from __future__ import annotations

import html
from urllib.parse import urlsplit

from services.computer_control_confirmation import ConfirmationDecision


PERMISSIONS = {
    'Microphone': ('Microphone access', 'Use your microphone',
                   'Jupiter needs your microphone for speech input and Live Conversation.', 'mic'),
    'Camera': ('Camera access', 'Use your camera',
               'Jupiter can see camera frames only while you have Live video turned on.', 'camera'),
    'Notifications': ('Notification access', 'Show notifications',
                      'Jupiter can notify you when a reminder is due.', 'bell'),
}


class DevicePermissionDecision(ConfirmationDecision):
    def __init__(self):
        super().__init__()
        self.remember = False

    def allow(self, remember=False):
        self.remember = remember is True
        return super().allow()


def origin_key(url):
    try:
        parsed = urlsplit(str(url or ''))
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            return None
        return (parsed.scheme, parsed.hostname.casefold(), parsed.port or (443 if parsed.scheme == 'https' else 80))
    except ValueError:
        return None


def trusted_request(request_url, trusted_url, current_url):
    expected = origin_key(trusted_url)
    return expected is not None and origin_key(request_url) == expected == origin_key(current_url)


def permission_html(kind, url):
    title, action, detail, icon = PERMISSIONS[kind]
    origin = origin_key(url)
    source = f'{origin[0]}://{origin[1]}:{origin[2]}' if origin else 'Unknown origin'
    paths = {
        'mic': '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/>',
        'camera': '<rect x="3" y="6" width="18" height="15" rx="3"/><path d="m8 6 2-3h4l2 3"/><circle cx="12" cy="13" r="4"/>',
        'bell': '<path d="M5 16V9a7 7 0 0 1 14 0v7l2 2H3l2-2M9 21h6"/>',
    }
    return (_HTML.replace('__TITLE__', html.escape(title)).replace('__ACTION__', html.escape(action))
            .replace('__DETAIL__', html.escape(detail)).replace('__SOURCE__', html.escape(source))
            .replace('__ICON__', paths[icon]))


def show_permission(decision, kind, url):
    """Runs on a worker while WebView2 holds a deferral; close/timeout denies."""
    import webview
    if decision._event.is_set():
        return False
    window = webview.create_window('Jupiter · Device permission', html=permission_html(kind, url),
                                  js_api=decision, width=510, height=490, min_size=(340, 420),
                                  resizable=True, on_top=True, background_color='#0f111a')
    decision._window = window
    window.events.closed += decision.deny
    if decision._event.is_set():
        decision.deny()
        try:
            window.destroy()
        except Exception:
            pass
    if not decision._event.wait(120):
        decision.deny()
    return decision.result


_HTML = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jupiter device permission</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:#0f111a;
color:#edf5ff;font:15px/1.55 system-ui,'Segoe UI',sans-serif}main{padding:28px;min-height:100vh;
background:radial-gradient(circle at 85% 0,rgba(59,130,246,.16),transparent 60%)}
.brand{font-size:11px;letter-spacing:.12em;color:#9ab8e1}.symbol{margin:20px 0 16px;display:grid;place-items:center;
width:58px;height:58px;background:#182c49;color:#8fc3ff;border:1px solid #36557d;border-radius:18px}
svg{width:28px;height:28px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
h1{font-size:24px;line-height:1.25;margin:0 0 12px}p{color:#bbcbe0;margin:0 0 18px;overflow-wrap:anywhere}
.source{font-size:12px;padding:10px 12px;border:1px solid #2b3d56;border-radius:10px;background:#141e2e}
.note{font-size:12px;color:#9eafc7}.remember{display:flex;align-items:center;gap:9px;color:#bbcbe0;font-size:13px}
input{accent-color:#378aeb;width:18px;height:18px}.actions{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:22px}
button{min-height:46px;border:1px solid #3b4c65;border-radius:12px;background:#1b2536;color:#eef5ff;
font:600 14px system-ui;cursor:pointer}#allow{background:linear-gradient(135deg,#205cbe,#378aeb,#205cbe);border-color:#579af0}
button:hover{box-shadow:inset 0 0 0 1px #94bdf3}button:focus-visible{outline:2px solid #a7ceff;outline-offset:3px}
@media(max-width:380px){main{padding:20px}.actions{gap:8px}}
</style></head><body><main role="dialog" aria-modal="true" aria-labelledby="title" aria-describedby="detail">
<div class="brand">JUPITER · DEVICE PERMISSION</div><div class="symbol" aria-hidden="true"><svg viewBox="0 0 24 24">__ICON__</svg></div>
<h1 id="title">__ACTION__?</h1><p id="detail">__DETAIL__</p><p class="source">Requested by: __SOURCE__</p>
<p class="note">Your choice applies to this Jupiter session. Device privacy settings still apply. You can continue without granting access.</p>
<label class="remember"><input id="remember" type="checkbox">Remember Allow for this Jupiter origin</label>
<div class="actions"><button id="deny" autofocus>Block</button><button id="allow">Allow</button></div></main>
<script>const api=()=>window.pywebview?.api;
document.getElementById('allow').onclick=()=>api()?.allow(document.getElementById('remember').checked);document.getElementById('deny').onclick=()=>api()?.deny();
document.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();api()?.deny();}});
</script></body></html>'''
