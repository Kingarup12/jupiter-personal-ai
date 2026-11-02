"""Quiet Jupiter-styled confirmations owned by the desktop host, not the planner page."""
from __future__ import annotations

import html
import threading


class ConfirmationDecision:
    def __init__(self):
        self._event = threading.Event()
        self.result = False
        self._lock = threading.Lock()
        self._window = None

    def _finish(self, allowed: bool) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self.result = allowed
            self._event.set()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
        return True

    def allow(self):
        return self._finish(True)

    def deny(self):
        return self._finish(False)


def confirmation_html(title: str, message: str) -> str:
    return _HTML.replace('__TITLE__', html.escape(str(title))).replace('__MESSAGE__', html.escape(str(message)))


def confirm_action(title: str, message: str) -> bool:
    import webview
    decision = ConfirmationDecision()
    window = webview.create_window('Jupiter · Confirm action', html=confirmation_html(title, message),
                                   js_api=decision, width=500, height=440, min_size=(400, 340),
                                   resizable=True, on_top=True, background_color='#0F111A')
    decision._window = window
    window.events.closed += decision.deny
    if not decision._event.wait(120):
        decision.deny()
    return decision.result


_HTML = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jupiter confirmation</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}html,body{height:100%;margin:0;background:#0f111a;
color:#e8eef8;font:14px/1.55 system-ui,'Segoe UI',sans-serif}main{height:100%;display:flex;flex-direction:column;
gap:16px;padding:24px;background:radial-gradient(circle at 90% 0,rgba(59,130,246,.13),transparent 60%)}
.brand{color:#7bbcff;font-size:13px;letter-spacing:.08em}h1{margin:0;font-size:21px;line-height:1.3}
.details{flex:1;min-height:0;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;color:#bdcadd;
padding:14px;border:1px solid #2a3e5e;border-radius:14px;background:#141c2a}
footer{display:flex;gap:12px;justify-content:flex-end;flex:none}button{min-height:42px;padding:9px 20px;
font:600 14px system-ui;border-radius:12px;border:1px solid #34445d;cursor:pointer;background:#192233;color:#e8eef8}
#allow{background:linear-gradient(135deg,#2563eb,#3186ee);border-color:#579cf5}
button:hover{filter:brightness(1.12)}button:focus-visible{outline:2px solid #93c5fd;outline-offset:3px}
@media(prefers-reduced-motion:no-preference){button{transition:filter .15s}}</style></head>
<body><main role="dialog" aria-labelledby="title" aria-describedby="details"><div class="brand">JUPITER · ACTION REVIEW</div>
<h1 id="title">__TITLE__</h1><div class="details" id="details">__MESSAGE__</div>
<footer><button id="deny" autofocus>Cancel</button><button id="allow">Allow this action</button></footer></main>
<script>const api=()=>window.pywebview&&window.pywebview.api;
document.getElementById('allow').onclick=()=>api()?.allow();document.getElementById('deny').onclick=()=>api()?.deny();
document.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();api()?.deny();}});
</script></body></html>'''
