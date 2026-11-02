"""Marker 197 (Pixie notch): a small always-on-top "notch" at the top centre of the screen that shows Jupiter's
presence while Live voice, a screen annotation or a phone task is running.

* compact   a floating black capsule with Jupiter's two eyes. They follow the mouse, swell with the user's voice,
            bounce with Jupiter's, squint while thinking, look down while working, smile when done, droop on errors,
            blink (sometimes twice), glance around when idle, get curious on hover and wink when clicked
* expanded  the capsule grows down: status pill ("Listening", "Thinking", "Speaking", "Working on your phone"),
            a rolling two-line transcript and, for a multi-step phone task, a live checklist
* hidden    nothing on screen

The window is owned by the Windows desktop host (pywebview, frameless, transparent, topmost, ``focus=False`` =>
WS_EX_NOACTIVATE): it never takes keyboard focus from the user's work. The real window is resized to the visible
capsule in every mode, so no invisible area at the top of the screen swallows clicks (browser tabs stay usable).
Pure helpers (sizes, placement, payload sanitising) are unit-tested; the window itself needs the host GUI.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable

MODES = ("compact", "expanded", "hidden")
STATES = ("idle", "listening", "thinking", "speaking", "acting", "done", "error")
GESTURES = ("nod", "shake", "tilt", "ponder", "surprise", "laugh")
# Logical (96-dpi) sizes of the visible capsule per mode; the expanded height grows with the checklist.
SIZES = {"compact": (204, 38), "expanded": (548, 96)}
CORNER = {"compact": 19, "expanded": 22}       # rounded bottom corners
EAR = 8                                        # small, gentle outward curves where the notch meets the top edge
EAR_HEIGHT = 6                                 # (elliptical, shallower than the rounded bottom corners)
# Marker 204: + the Astro Buddy expressions a two-eye notch can show (globe, book, brain, palette, heart, ?, bulb,
# confetti, shy glance). Full-body poses (coding at a keyboard, holding props) stay Astro-only.
MOODS = ("coding", "math", "scanning", "debugging", "working", "music", "happy", "sleep", "idle",
         "websearch", "reading", "memory", "creative", "love", "confused", "idea", "celebrate", "shy")
STUNTS = ("spin", "jump", "wave", "dance", "celebrate")                 # Juno moves the user can ask for (marker 208)
CHECKLIST_ROW = 24
MAX_CHECKLIST = 8
MAX_LINE_CHARS = 220
TOP_GAP = 0                          # stuck to the top edge of the screen, like a hardware notch

_LIVE_TO_NOTCH = {
    "listening": "listening", "user_speaking": "listening", "endpoint_confirmation": "listening",
    "waiting_for_user": "listening", "transcribing": "thinking", "processing": "thinking", "thinking": "thinking",
    "generating_text": "thinking", "preparing_speech": "thinking", "assistant_speaking": "speaking",
    "speaking": "speaking", "connecting": "idle", "entering": "idle", "idle": "idle", "error": "error",
}


def live_state_to_notch(state: str) -> str:
    return _LIVE_TO_NOTCH.get(str(state or "").casefold(), "idle")


def reply_reaction(text: str) -> str | None:
    """Return a small face reaction only for clear, direct yes/no replies."""
    value = re.sub(r"^[\s>*_`]+", "", str(text or "")).casefold()
    if re.match(r"^(?:yes|yeah|yep|yup|sure|absolutely|certainly|haan|han|bilkul)(?:[\s,!.;:]|$)", value):
        return "nod"
    if re.match(r"^(?:no problem|no worries|no need)(?:[\s,!.;:]|$)", value):
        return None
    if re.match(r"^(?:nope|nah|nahi|nahin|bilkul nahi|no)(?:[\s,!.;:]|$)", value):
        return "shake"
    return None


def notch_size(mode: str, checklist_rows: int = 0) -> tuple[int, int]:
    """Logical size of the visible notch for ``mode`` (hidden => (0, 0))."""
    if mode not in SIZES:
        return (0, 0)
    width, height = SIZES[mode]
    if mode == "expanded" and checklist_rows:
        height += 6 + CHECKLIST_ROW * min(MAX_CHECKLIST, int(checklist_rows))
    return width, height


def notch_position(monitor: tuple[int, int, int, int], size: tuple[int, int], gap: int = 0,
                   offset: int = 0) -> tuple[int, int]:
    """Top edge of ``monitor`` (physical left, top, right, bottom) for a window of physical ``size``: centred, moved
    ``offset`` physical pixels sideways (the user dragged it) and kept fully on the monitor."""
    left, top, right, _bottom = monitor
    x = left + (right - left - size[0]) // 2 + int(offset)
    return max(left, min(right - size[0], x)), top + max(0, int(gap))


def notch_state_path() -> str:
    import os
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Jupiter", "notch.json")


def look_direction(dx: float, dy: float, width: float) -> dict[str, float]:
    """Where the eyes look (-1..1 each axis) for a pointer ``dx``/``dy`` physical pixels from the notch centre."""
    span_x = max(320.0, float(width) * 2.0)
    x = max(-1.0, min(1.0, float(dx) / span_x))
    y = max(-1.0, min(1.0, float(dy) / 520.0))
    return {"x": round(x, 3), "y": round(y, 3)}


def sanitize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Only known fields, bounded lengths: the notch page renders text with textContent, never HTML."""
    payload = payload if isinstance(payload, dict) else {}
    clean: dict[str, Any] = {}
    if payload.get("mode") in MODES:
        clean["mode"] = payload["mode"]
    if payload.get("state") in STATES:
        clean["state"] = payload["state"]
    for key in ("status", "detail"):
        if key in payload:
            clean[key] = " ".join(str(payload.get(key) or "").split())[:80]
    if "user" in payload:
        clean["user"] = " ".join(str(payload.get("user") or "").split())[-MAX_LINE_CHARS:]
    if "jupiter" in payload:
        clean["jupiter"] = " ".join(str(payload.get("jupiter") or "").split())[-MAX_LINE_CHARS:]
    if payload.get("mood") in MOODS or payload.get("mood") == "":
        clean["mood"] = payload["mood"]
    if payload.get("gesture") in GESTURES:
        clean["gesture"] = payload["gesture"]
    if payload.get("reaction") in {"nod", "shake"}:                     # the bridge's verdict on a finished reply
        clean["reaction"] = payload["reaction"]
    if payload.get("stunt") in STUNTS:                                   # marker 208: "Juno, spin around"
        clean["stunt"] = payload["stunt"]
    if "level" in payload:
        try:
            clean["level"] = round(max(0.0, min(1.0, float(payload.get("level") or 0))), 2)
        except (TypeError, ValueError):
            pass
    if "checklist" in payload:
        items = payload.get("checklist") if isinstance(payload.get("checklist"), list) else []
        clean["checklist"] = [
            {"label": " ".join(str(item.get("label") or "").split())[:70],
             "state": item.get("state") if item.get("state") in {"pending", "active", "done", "failed"} else "pending"}
            for item in items[:MAX_CHECKLIST] if isinstance(item, dict) and str(item.get("label") or "").strip()
        ]
    return clean


NOTCH_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Jupiter notch</title><style>
:root{color-scheme:dark;--ink:#f5f7fb;--muted:#9aa4b2;--blue:#4facfe;--green:#3ddc84;--amber:#ffb340;--red:#ff5a5f;
--glow:79,172,254;--ring:0}
html,body{margin:0;height:100%;background:#000;overflow:hidden;user-select:none;
font:13px/1.35 system-ui,'Segoe UI',sans-serif;color:var(--ink)}
#notch{position:absolute;inset:0;background:#000;display:flex;flex-direction:column;padding:0 8px;
overflow:hidden;cursor:grab;touch-action:none}
#notch[data-mode=expanded]{cursor:default}
#notch.dragging{cursor:grabbing}
#ring{position:absolute;left:8px;right:8px;bottom:0;height:2px;border-radius:2px;pointer-events:none;
background:rgba(var(--glow),calc(var(--ring)*.9));box-shadow:0 0 8px rgba(var(--glow),calc(var(--ring)*.8))}
#notch.pop{animation:pop .42s cubic-bezier(.34,1.56,.64,1)}
#notch[data-state=speaking]{--glow:61,220,132}#notch[data-state=acting],#notch[data-state=thinking]{--glow:255,179,64}
#notch[data-state=error]{--glow:255,90,95}
.top{position:relative;display:flex;align-items:center;justify-content:center;gap:12px;height:38px;flex:none;padding:0 16px}
#notch[data-mode=expanded] .top{justify-content:flex-start}
.face{position:relative;display:flex;gap:10px;align-items:center;justify-content:center;width:40px;height:26px;flex:none;
will-change:transform}
#acc{position:absolute;right:16px;top:50%;transform:translateY(-50%);font:600 12px/1 'Cascadia Mono',Consolas,monospace;
color:#cfe3ff;min-width:22px;text-align:center;pointer-events:none;opacity:0;transition:opacity .25s}
#notch[data-mode=expanded] #acc{position:static;transform:none;order:1;margin-left:-6px}#notch[data-mode=expanded] .face{order:0}#notch[data-mode=expanded] .status{order:2}#notch[data-mode=expanded] button{order:3}
#notch[data-mood]:not([data-mood=""]):not([data-mood=idle]) #acc{opacity:1}
#notch[data-mood=working] #acc{animation:spin 1.6s linear infinite;color:#ffd08a}
#notch[data-mood=music] #acc{animation:float 1.4s ease-out infinite;color:#9ef0c0}
#notch[data-mood=sleep] #acc{animation:float 2.4s ease-out infinite;color:#9aa4b2;font-style:italic}
#notch[data-mood=debugging] #acc{animation:wobble .5s ease-in-out infinite}
#notch[data-mood=happy] #acc{animation:sparkle 1s ease-in-out infinite}
#notch[data-mood=coding] #acc::after{content:'_';animation:caret 1s steps(1) infinite}
#notch[data-mood=scanning] .eye{background:#7df9ff;box-shadow:0 0 8px rgba(125,249,255,.8)}
#notch[data-mood=websearch] #acc{animation:spin 3.2s linear infinite;color:#7df9ff}
#notch[data-mood=reading] .eye{transform:translateY(2px) scaleY(.82)}
#notch[data-mood=memory] #acc{animation:sparkle 1.6s ease-in-out infinite;color:#c4a7ff}
#notch[data-mood=creative] #acc{animation:wobble 1.2s ease-in-out infinite}
#notch[data-mood=love] .eye{background:#ff7eb6;box-shadow:0 0 9px rgba(255,126,182,.85);border-radius:45% 45% 60% 60%}
#notch[data-mood=love] #acc{animation:float 1.3s ease-out infinite;color:#ff7eb6}
#notch[data-mood=confused] #acc{animation:wobble .9s ease-in-out infinite;color:#ffcf6b}
#notch[data-mood=confused] .face{transform:rotate(-9deg)}
#notch[data-mood=idea] #acc{animation:pop .6s cubic-bezier(.34,1.56,.64,1),sparkle 1.2s ease-in-out .6s infinite;color:#ffe066}
#notch[data-mood=celebrate] #acc{animation:sparkle .7s ease-in-out infinite}
#notch[data-mood=celebrate] .face{animation:pop .5s cubic-bezier(.34,1.56,.64,1) infinite alternate}
#notch[data-mood=shy] .eye{transform:translate(-2px,3px) scaleY(.7);box-shadow:0 0 10px rgba(255,140,170,.75)}
#notch[data-mood=scanning]::after{content:'';position:absolute;top:0;bottom:0;width:30px;left:-30px;pointer-events:none;
background:linear-gradient(90deg,transparent,rgba(125,249,255,.35),transparent);animation:sweep 1.3s linear infinite}
#notch[data-mood=sleep] .eye,#notch[data-mood=happy] .eye{transition:none}
@keyframes float{0%{transform:translate(0,-30%);opacity:0}25%{opacity:1}100%{transform:translate(6px,-130%);opacity:0}}
@keyframes wobble{50%{transform:translateY(-50%) rotate(14deg)}}
@keyframes sparkle{50%{transform:translateY(-50%) scale(1.35);opacity:.6}}
@keyframes caret{50%{opacity:0}}
@keyframes sweep{to{left:100%}}
#notch.laughing .eye{width:13px;height:7px;background:transparent;border:3px solid #fff;border-bottom:0;
border-radius:13px 13px 0 0;box-shadow:none}
.eye{width:10px;height:18px;border-radius:999px;background:#fff;transform-origin:50% 50%;will-change:transform;
box-shadow:0 0 6px rgba(var(--glow),.55);transition:width .18s ease,height .18s ease,background-color .2s ease,
border-radius .18s ease,box-shadow .2s ease}
#notch[data-state=acting] .eye{background:#ffd08a}
#notch[data-state=done] .eye{width:13px;height:7px;background:transparent;border:3px solid #fff;border-bottom:0;
border-radius:13px 13px 0 0;box-shadow:none}
#notch[data-state=error] .eye{height:9px}
.status{display:none;align-items:center;gap:7px;min-width:0;flex:1}
#notch[data-mode=expanded] .status{display:flex}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
#notch[data-state=listening] .dot{background:var(--blue);animation:pulse 1.4s infinite}
#notch[data-state=thinking] .dot{background:var(--amber);animation:pulse 1s infinite}
#notch[data-state=speaking] .dot{background:var(--green)}
#notch[data-state=acting] .dot{background:var(--amber);animation:pulse .8s infinite}
#notch[data-state=done] .dot{background:var(--green)}
#notch[data-state=error] .dot{background:var(--red)}
#status{font-weight:600;white-space:nowrap}
#detail{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
button{font:inherit;color:var(--ink);background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.16);
border-radius:999px;width:24px;height:24px;padding:0;cursor:pointer;flex:none;display:none;line-height:1}
#notch[data-mode=expanded] button{display:inline-grid;place-items:center}
button:hover{background:rgba(255,255,255,.16)}
button:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
.body{display:none;padding:0 18px 12px;min-height:0;flex:1;flex-direction:column;gap:3px}
#notch[data-mode=expanded] .body{display:flex}
.line{flex:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#user{color:var(--muted)}#user:not(:empty)::before{content:'You: ';color:#6b7380}
#jupiter{color:var(--ink)}#jupiter:not(:empty)::before{content:'Jupiter: ';color:var(--blue)}
#checklist{flex:none;list-style:none;margin:6px 0 0;padding:0;display:flex;flex-direction:column;gap:2px}
#checklist li{display:flex;align-items:center;gap:8px;height:22px;color:var(--muted);white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
#checklist li::before{content:'';width:14px;height:14px;border-radius:50%;border:1.6px solid #5b6472;flex:none;
box-sizing:border-box}
#checklist li[data-state=active]{color:var(--ink)}
#checklist li[data-state=active]::before{border-color:var(--amber);border-top-color:transparent;animation:spin .9s linear infinite}
#checklist li[data-state=done]{color:var(--ink)}
#checklist li[data-state=done]::before{border:none;background:var(--green) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 14 14'%3E%3Cpath d='M3.5 7.2l2.2 2.2 4.8-4.8' fill='none' stroke='%23000' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/14px no-repeat}
#checklist li[data-state=failed]{color:var(--red)}
#checklist li[data-state=failed]::before{border-color:var(--red)}
@keyframes pulse{50%{opacity:.35}}@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pop{0%{transform:scale(.86)}100%{transform:scale(1)}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div id="notch" data-mode="compact" data-state="idle" data-mood="" role="status" aria-live="polite" aria-label="Jupiter">
 <div id="ring" aria-hidden="true"></div>
 <div class="top">
  <span id="acc" aria-hidden="true"></span>
  <div class="face" aria-hidden="true"><span class="eye" id="eyeL"></span><span class="eye" id="eyeR"></span></div>
  <div class="status"><span class="dot" aria-hidden="true"></span><span id="status">Jupiter</span><span id="detail"></span></div>
  <button id="open" type="button" aria-label="Open the Jupiter window" title="Open Jupiter">&#8599;</button>
  <button id="collapse" type="button" aria-label="Make the Jupiter notch small" title="Make small">&#8211;</button>
  <button id="hide" type="button" aria-label="Hide the Jupiter notch" title="Hide">&times;</button>
 </div>
 <div class="body"><div class="line" id="user"></div><div class="line" id="jupiter"></div><ul id="checklist" hidden></ul></div>
</div><script>
const $=(i)=>document.getElementById(i);const api=()=>window.pywebview&&window.pywebview.api;
const notchEl=$('notch'), L=$('eyeL'), R=$('eyeR');
const LABEL={idle:'Jupiter',listening:'Listening',thinking:'Thinking',speaking:'Speaking',acting:'Working',done:'Done',error:'Needs you'};
const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
// ── eye engine: every frame the eyes' look, size and lids come from Jupiter's state, the voice level, the pointer ──
const FACE=document.querySelector('.face');
const E={mood:'',moodUntil:0,lastActivity:performance.now(),gesture:null,gestureAt:0,lastReply:'',state:'idle',level:0,target:0,look:{x:0,y:0},cursor:{x:0,y:0,at:0},hover:null,saccade:{x:0,y:0},
  nextSaccade:0,blinkAt:0,nextBlink:performance.now()+1800,second:false,wink:0,wake:1,wakeAt:0,cursorBusy:false,doneAt:0};
const ease=(t)=>1+2.70158*Math.pow(t-1,3)+1.70158*Math.pow(t-1,2);          // easeOutBack: a little overshoot
const lerp=(a,b,k)=>a+(b-a)*k;
function lids(now){                                                          // 1 = open, ~0.08 = closed
  if(reduce)return 1;
  if(now>E.nextBlink){E.blinkAt=now;E.second=Math.random()<.2;E.nextBlink=now+2200+Math.random()*4300;}
  const t=now-E.blinkAt;let v=1;
  if(t<150)v=1-Math.sin(Math.PI*t/150)*.92;
  else if(E.second&&t>220&&t<370)v=1-Math.sin(Math.PI*(t-220)/150)*.92;
  return v;
}
function frame(now){
  E.level=lerp(E.level,E.target,E.target>E.level?.45:.12);
  const s=E.state, lv=E.level;
  // where to look: the pointer over the notch > the mouse cursor anywhere > idle glances
  let goal={x:0,y:0};
  if(E.hover)goal=E.hover;
  else if(now-E.cursor.at<1500)goal={x:E.cursor.x,y:Math.max(-.4,E.cursor.y)};
  if(!reduce&&s==='idle'&&now>E.nextSaccade){E.saccade={x:(Math.random()*2-1)*.8,y:(Math.random()*2-1)*.35};
    E.nextSaccade=now+2600+Math.random()*4200;setTimeout(()=>{E.saccade={x:0,y:0};},700+Math.random()*600);}
  if(!E.hover&&now-E.cursor.at>=1500)goal={x:E.saccade.x,y:E.saccade.y};
  if(s==='thinking')goal={x:Math.sin(now/650)*.8,y:-.75};                    // eyes up, drifting: mulling it over
  // moods: what Jupiter is doing or talking about (coding, math, scanning, debugging, working, music, happy, sleep)
  if(E.mood&&E.mood!=='sleep'&&E.moodUntil&&now>E.moodUntil)setMood('');
  if(!reduce&&!E.mood&&s==='idle'&&now-E.lastActivity>75000)setMood('sleep',0);
  const md=E.mood;
  if(md==='coding')goal={x:goal.x*.3-.2,y:.65};
  else if(md==='math')goal={x:.75,y:-.7};
  else if(md==='debugging')goal={x:Math.sin(now/230)*.95,y:.3};
  else if(md==='working')goal={x:goal.x*.5,y:.6};
  else if(md==='music')goal={x:Math.sin(now/320)*.7,y:0};
  if(s==='acting')goal={x:goal.x*.6,y:.7};                                    // looking down at the work
  E.look.x=lerp(E.look.x,goal.x,reduce?1:.18);E.look.y=lerp(E.look.y,goal.y,reduce?1:.18);
  let sx=1,sy=1,lift=0,tilt=0;
  if(s==='listening'){sy=1.04+lv*.5;sx=1+lv*.14;}                             // swell with the user's voice
  else if(s==='speaking'){sy=.82+lv*.42;lift=-lv*3.4;}                        // bounce with Jupiter's voice
  else if(s==='thinking'){sy=.58;}
  else if(s==='acting'){sy=.7;}
  else if(s==='error'){tilt=14;}
  else if(s==='done'){lift=reduce?0:-Math.abs(Math.sin((now-E.doneAt)/180))*2*Math.max(0,1-(now-E.doneAt)/1400);}
  if(md==='coding'||md==='working'){sy*=.74;}
  else if(md==='debugging'){sy*=.68;}
  else if(md==='scanning'){sy*=.42;sx*=1.25;}
  else if(md==='music'){sy*=.85;lift-=Math.abs(Math.sin(now/320))*2;}
  else if(md==='sleep'){sy=.06;}
  else if(!md&&s==='idle'&&!reduce){lift+=Math.sin(now/1100)*1.6;}             // idle: floating gently
  if(E.hover&&s==='idle'){sx*=1.12;sy*=1.12;}                                  // curious when you hover
  if(E.wake<1){E.wake=Math.min(1,(now-E.wakeAt)/420);}
  const open=(md==='sleep'?1:lids(now))*(reduce?1:ease(E.wake)), x=E.look.x*4, y=E.look.y*3+lift;
  const closedL=E.wink>now?.1:1;
  // head gesture on the whole face (eyes move together): nod, shake, tilt, ponder, surprise, laugh
  let hx=0,hy=0,hr=0,hs=1;
  if(E.gesture&&!reduce){const g=now-E.gestureAt, dur={nod:900,shake:900,tilt:1300,ponder:1500,surprise:700,laugh:1300}[E.gesture]||900;
    if(g>dur){E.gesture=null;notchEl.classList.remove('laughing');}
    else{const k=1-g/dur;
      if(E.gesture==='nod')hy=Math.sin(g/900*Math.PI*4)*5.5*k;                       // two nods: yes
      else if(E.gesture==='shake')hx=Math.sin(g/900*Math.PI*6)*7*k;                 // three shakes: no
      else if(E.gesture==='tilt')hr=-13*Math.sin(Math.min(1,g/250)*Math.PI/2)*(g>dur-300?(dur-g)/300:1);   // curious head tilt
      else if(E.gesture==='ponder'){hx=-3;hy=-2.5;hr=-6;}                           // looking up and away, thinking
      else if(E.gesture==='surprise')hs=1+.35*Math.sin(Math.min(1,g/700)*Math.PI);
      else if(E.gesture==='laugh')hy=-Math.abs(Math.sin(g/1300*Math.PI*5))*2.5;}}
  FACE.style.transform=(hx||hy||hr||hs!==1)?`translate(${hx}px,${hy}px) rotate(${hr}deg) scale(${hs})`:'';
  if(s==='done'){L.style.transform=`translate(${x}px,${y}px)`;R.style.transform=`translate(${x}px,${y}px)`;}
  else{
    L.style.transform=`translate(${x}px,${y}px) rotate(${-tilt}deg) scale(${sx},${Math.max(.06,sy*open*closedL)})`;
    R.style.transform=`translate(${x}px,${y}px) rotate(${tilt}deg) scale(${sx},${Math.max(.06,sy*open)})`;
  }
  notchEl.style.setProperty('--ring',(s==='listening'||s==='speaking')?Math.min(1,.15+lv*1.3).toFixed(2):'0');
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
// The mouse cursor anywhere on screen, asked from the host ~8 times a second while the notch is shown.
if(!reduce)setInterval(async()=>{
  if(E.cursorBusy||document.hidden||!api()||!api().cursor)return;E.cursorBusy=true;
  try{const c=await api().cursor();if(c&&c.ok&&Math.abs(c.x-E.cursor.x)+Math.abs(c.y-E.cursor.y)>.01){E.cursor={x:c.x,y:c.y,at:performance.now()};activity();}}catch(_){}
  E.cursorBusy=false;},125);
notchEl.addEventListener('pointermove',(e)=>{const r=notchEl.getBoundingClientRect();
  E.hover={x:Math.max(-1,Math.min(1,(e.clientX-(r.left+28))/60)),y:Math.max(-1,Math.min(1,(e.clientY-20)/30))};});
notchEl.addEventListener('pointerleave',()=>{E.hover=null;});
window.setNotch=(p)=>{
  activity();
  if('mood' in p)setMood(p.mood,p.mood==='happy'?2600:20000);
  else if(p.user&&p.user!==E.lastUser){E.lastUser=p.user;const m=moodFor(p.user);if(m)setMood(m,m==='happy'?2600:20000);}
  if(!('mood' in p)&&p.jupiter&&!E.mood){const m=moodFor(p.jupiter);if(m&&m!=='happy')setMood(m);}
  if(p.mode){if(notchEl.dataset.mode!==p.mode){notchEl.classList.remove('pop');void notchEl.offsetWidth;notchEl.classList.add('pop');}
    notchEl.dataset.mode=p.mode;}
  if(p.state&&p.state!==E.state){E.state=p.state;notchEl.dataset.state=p.state;if(p.state==='done')E.doneAt=performance.now();
    if(!('status' in p))$('status').textContent=LABEL[p.state]||'Jupiter';}
  if('level' in p)E.target=Math.max(0,Math.min(1,+p.level||0));
  if(p.state&&p.state!=='listening'&&p.state!=='speaking')E.target=0;
  if('status' in p)$('status').textContent=p.status||LABEL[E.state]||'Jupiter';
  if('detail' in p)$('detail').textContent=p.detail?('· '+p.detail):'';
  if('user' in p)$('user').textContent=p.user||'';
  if('jupiter' in p){$('jupiter').textContent=p.jupiter||'';
    if(p.reaction){E.lastReply=String(p.jupiter||'');window.notchGesture(p.reaction);}else reactToReply(p.jupiter);}
  else if(p.reaction)window.notchGesture(p.reaction);
  if(p.gesture)window.notchGesture(p.gesture);
  if('checklist' in p){const ul=$('checklist');ul.replaceChildren(...p.checklist.map((c)=>{const li=document.createElement('li');
    li.dataset.state=c.state;li.textContent=c.label;return li;}));ul.hidden=!p.checklist.length;}
  notchEl.setAttribute('aria-label','Jupiter, '+($('status').textContent||'')+($('detail').textContent?' '+$('detail').textContent:''));};
// Reactions to what Jupiter says: a nod for yes, a head shake for no, a tilt for a question, and so on.
const REACT=[[/^(?:yes|yeah|yep|yup|sure|of course|absolutely|definitely|certainly|correct|exactly|right|okay|ok|done|got it|haan|han|ji|ji haan|bilkul|zaroor|theek hai|sahi)\b/i,'nod'],
  [/^(?:no problem|no worries|no need|not a problem)\b/i,null],
  [/^(?:no|nope|nah|not really|sorry|unfortunately|i can'?t|i cannot|i couldn'?t|i'?m afraid|nahi|nahin|na\b|mujhe nahi)/i,'shake'],
  [/^(?:hmm+|let me (?:think|check|see)|good question|well[,.]|thinking)/i,'ponder'],
  [/^(?:wow|whoa|woah|oh wow|oh!|really\?|seriously)/i,'surprise'],
  [/^(?:haha|hehe|lol|ha ha|😂|😄|😆)/i,'laugh']];
function gestureFor(text,complete){const t=String(text||'').trim().replace(/^[^\p{L}\p{N}😂😄😆]+/u,'');
  for(const [re,g] of REACT)if(re.test(t))return g;return complete&&/\?\s*$/.test(t)?'tilt':null;}  // first match wins (null = none)
const ACC={coding:'</>',math:'∑',debugging:'🐞',working:'⚙',music:'♪',happy:'✨',sleep:'z',
  websearch:'🌐',reading:'📖',memory:'🧠',creative:'🎨',love:'♥',confused:'?',idea:'💡',celebrate:'🎉',shy:'☺'};
const MATH_GLYPHS=['∑','π','÷','√','∫'];let mathTick=null;
function setMood(m,ms=12000){
  if(m===E.mood){E.moodUntil=ms?performance.now()+ms:0;return;}
  E.mood=m;E.moodUntil=m&&ms?performance.now()+ms:0;notchEl.dataset.mood=m;
  $('acc').textContent=ACC[m]||'';notchEl.classList.toggle('laughing',m==='happy');
  if(mathTick){clearInterval(mathTick);mathTick=null;}
  if(m==='math')mathTick=setInterval(()=>{const i=(MATH_GLYPHS.indexOf($('acc').textContent)+1)%MATH_GLYPHS.length;$('acc').textContent=MATH_GLYPHS[i];},650);}
// what a turn is about -> mood (the bridge/page can also set one explicitly)
const MOOD_RULES=[[/\b(?:debug(?:ging)?|bug|traceback|stack ?trace|exception|crash(?:es|ed)?|not working|error in)\b/i,'debugging'],
  [/```|\b(?:code|coding|python|javascript|typescript|java|c\+\+|html|css|sql|react|function|compile|script|program(?:ming)?|api)\b/i,'coding'],
  [/\b(?:math|maths|calculate|solve|equation|integral|derivative|algebra|geometry|square root|multiply|divide)\b|\d\s*[-+*/×÷^]\s*\d/i,'math'],
  [/\b(?:song|songs|music|playlist|spotify|gaana|gaane|lyrics|beat|play(?:ing)? (?:a |some |the )?\w+ (?:song|music))\b/i,'music'],
  [/\b(?:screen|camera|photo|picture|image|look at (?:this|my)|see (?:this|my)|scan)\b/i,'scanning'],
  [/\b(?:search(?:ing)? the web|look(?:ing)? (?:it )?up|latest news|according to)\b/i,'websearch'],
  [/\b(?:pdf|document|article|reading|read (?:this|the|my))\b/i,'reading'],
  [/\b(?:remember|memory|memories|forgot|forget|recall)\b/i,'memory'],
  [/\b(?:draw|paint|image of|picture of|generate an? (?:image|picture)|design)\b/i,'creative'],
  [/\b(?:love (?:it|you)|you(?:'re| are) (?:the best|amazing|cute|sweet))\b/i,'love'],
  [/\b(?:congrats|congratulations|we did it|it worked|passed|success(?:ful)?)\b/i,'celebrate'],
  [/\b(?:thanks|thank you|awesome|amazing|great job|yay|haha)\b/i,'happy']];
function moodFor(text){const t=String(text||'');for(const [re,m] of MOOD_RULES)if(re.test(t))return m;return '';}
function activity(){E.lastActivity=performance.now();if(E.mood==='sleep'){setMood('');window.wakeNotch&&window.wakeNotch();}}
window.notchGesture=(g)=>{if(!g)return;E.gesture=g;E.gestureAt=performance.now();if(g==='laugh')notchEl.classList.add('laughing');};
function reactToReply(text){const t=String(text||'');
  const fresh=!E.lastReply||!t.startsWith(E.lastReply.slice(0,Math.min(12,E.lastReply.length)));
  if(fresh&&t.trim().length>=2){const g=gestureFor(t.slice(0,60),false);if(g)window.notchGesture(g);E.questioned=false;}
  else if(!E.questioned&&/\?\s*$/.test(t)&&t.length>E.lastReply.length){E.questioned=true;window.notchGesture('tilt');}
  E.lastReply=t;}
window.wakeNotch=()=>{E.wake=0;E.wakeAt=performance.now();notchEl.classList.remove('pop');void notchEl.offsetWidth;notchEl.classList.add('pop');};
let drag=null;
notchEl.addEventListener('pointerdown',(e)=>{if(e.button!==0||e.target.closest('button'))return;
  drag={x:e.screenX,sent:0,moved:false};try{notchEl.setPointerCapture(e.pointerId);}catch(_){}});
notchEl.addEventListener('pointermove',(e)=>{if(!drag)return;const dx=e.screenX-drag.x;
  if(!drag.moved&&Math.abs(dx)<5)return;if(!drag.moved){drag.moved=true;notchEl.classList.add('dragging');}
  const step=Math.round((dx-drag.sent)*(window.devicePixelRatio||1));
  if(step&&api()&&api().drag){drag.sent=dx;api().drag(step);}});
const endDrag=(e)=>{const d=drag;drag=null;notchEl.classList.remove('dragging');if(!d)return;activity();
  if(d.moved){api()&&api().drag_end&&api().drag_end();return;}
  if(e&&e.type==='pointerup'&&!e.target.closest('button')){E.wink=performance.now()+260;
    if(notchEl.dataset.mode==='compact')setTimeout(()=>api()&&api().expand(),160);}};
notchEl.addEventListener('pointerup',endDrag);notchEl.addEventListener('pointercancel',endDrag);
$('collapse').onclick=()=>api()&&api().collapse();$('hide').onclick=()=>api()&&api().hide();
$('open').onclick=()=>api()&&api().open();
document.addEventListener('keydown',(e)=>{if(e.key==='Escape'){e.preventDefault();api()&&api().hide();}});
</script></body></html>"""


class NotchApi:
    """js_api of the notch window: expand / collapse / hide only (it cannot reach any other host feature)."""

    def __init__(self, owner: "NotchOverlay"):
        self._owner = owner

    def expand(self):
        self._owner.set_mode("expanded", user=True)
        return True

    def collapse(self):
        self._owner.set_mode("compact", user=True)
        return True

    def hide(self):
        self._owner.set_mode("hidden", user=True)
        return True

    def cursor(self):
        return self._owner.cursor_direction()

    def drag(self, dx, dy=0):
        self._owner.move_by(int(dx or 0), int(dy or 0))
        return True

    def recenter(self):
        self._owner.recenter()
        return True

    def shape(self, payload=None):
        self._owner.set_shape(payload)
        return True

    def drag_end(self):
        self._owner.end_drag()
        self._owner.save_position()
        return True

    def drag_begin(self, fx=None, fy=None, kind="mouse", page_width=0, page_height=0):
        self._owner.begin_drag(fx, fy, kind, page_width, page_height)
        return True

    def drag_follow(self, total_dx=None, total_dy=None):
        self._owner.follow_drag(total_dx, total_dy)
        return True

    def open(self):
        if self._owner.on_open:
            try:
                self._owner.on_open()
            except Exception:
                pass
        return True


class NotchOverlay:
    """Host-side owner of the notch window. Thread-safe; every method is safe to call repeatedly."""

    def __init__(self, *, window_factory: Callable[["NotchOverlay"], Any] | None = None,
                 placer: Callable[[Any, tuple[int, int]], None] | None = None):
        self._factory = window_factory or _default_window_factory
        self._placer = placer or _place_window
        self._lock = threading.RLock()
        self.window: Any = None
        self.mode = "hidden"
        self.state: dict[str, Any] = {"state": "idle", "checklist": []}
        self.user_hidden = False          # the user closed it: automatic updates do not bring it back this session
        self.on_mode: Callable[[str], Any] | None = None
        self.on_open: Callable[[], Any] | None = None
        self.on_user_mode: Callable[[str], Any] | None = None    # the bridge keeps a user's expand/collapse
        self.offset = self.load_position()                        # logical px from the centre of the top edge

    def _ensure(self) -> Any:
        if self.window is None:
            try:
                self.window = self._factory(self)
            except Exception:
                self.window = None
        return self.window

    def _push(self, payload: dict[str, Any]) -> None:
        window = self.window
        if window is None or not payload:
            return
        try:
            window.evaluate_js("window.setNotch&&window.setNotch(" + json.dumps(payload) + ")")
        except Exception:
            pass

    def _resize(self) -> None:
        window = self.window
        if window is None:
            return
        size = self._size()
        try:
            if self.mode == "hidden":
                window.hide()
                return
            self._placer(window, size, self.offset)
            window.show()
        except Exception:
            pass

    def _size(self) -> tuple[int, int]:
        return notch_size(self.mode, len(self.state.get("checklist") or []))

    def set_mode(self, mode: str, *, user: bool = False) -> str:
        if mode not in MODES:
            return self.mode
        with self._lock:
            if user:
                self.user_hidden = mode == "hidden"
                if self.on_user_mode:
                    try:
                        self.on_user_mode(mode)
                    except Exception:
                        pass
            elif mode != "hidden" and self.user_hidden:
                return self.mode
            if mode != "hidden":
                self._ensure()
            woke = self.mode == "hidden" and mode != "hidden"
            self.mode = mode
            self._push({"mode": mode})
            self._resize()
            if woke and self.window is not None:
                try:
                    self.window.evaluate_js("window.wakeNotch&&window.wakeNotch()")
                except Exception:
                    pass
        if self.on_mode:
            try:
                self.on_mode(mode)
            except Exception:
                pass
        return self.mode

    def cursor_direction(self) -> dict[str, Any]:
        """Where the mouse is relative to the notch, for the eyes to follow (-1..1 per axis)."""
        window = self.window
        if window is None or self.mode == "hidden":
            return {"ok": False, "x": 0, "y": 0}
        try:
            import ctypes
            from ctypes import wintypes

            from services.control_indicator import _win32
            user32 = _win32()["user32"]
            rect = wintypes.RECT()
            if not user32.GetWindowRect(int(window.native.Handle.ToInt64()), ctypes.byref(rect)):
                return {"ok": False, "x": 0, "y": 0}
            point = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
            look = look_direction(point.x - (rect.left + rect.right) / 2, point.y - (rect.top + rect.bottom) / 2,
                                  rect.right - rect.left)
            return {"ok": True, **look}
        except Exception:
            return {"ok": False, "x": 0, "y": 0}

    def update(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        clean = sanitize_payload(payload)
        with self._lock:
            rows_before = len(self.state.get("checklist") or [])
            self.state.update({k: v for k, v in clean.items() if k != "mode"})
            if "mode" in clean:
                self.set_mode(clean["mode"])
            elif self.mode == "hidden" and not self.user_hidden and clean.get("state") not in (None, "idle"):
                self.set_mode("compact")
            self._push({k: v for k, v in clean.items() if k != "mode"})
            if len(self.state.get("checklist") or []) != rows_before and self.mode == "expanded":
                self._resize()
        return {"mode": self.mode, **{k: self.state.get(k) for k in ("state", "status")}}

    @staticmethod
    def load_position() -> int:
        try:
            with open(notch_state_path(), encoding="utf-8") as handle:
                return int(json.load(handle).get("offset", 0))
        except Exception:
            return 0

    def save_position(self) -> None:
        try:
            import os
            os.makedirs(os.path.dirname(notch_state_path()), exist_ok=True)
            with open(notch_state_path(), "w", encoding="utf-8") as handle:
                json.dump({"offset": int(self.offset)}, handle)
        except Exception:
            pass

    def move_by(self, dx: int, dy: int = 0) -> None:
        """Slide the notch along the top edge by ``dx`` physical pixels (kept on its monitor; ``dy`` is ignored: the
        notch belongs to the top edge)."""
        window = self.window
        if window is None or not dx:
            return
        try:
            self.offset = _move_window(window, int(dx))
        except Exception:
            pass

    def recenter(self) -> None:
        """Back to the centre of the top edge (double-click)."""
        with self._lock:
            self.offset = 0
            self.save_position()
            if self.mode != "hidden":
                self._resize()

    def set_shape(self, payload: Any) -> None:
        """Only the Astro Buddy window takes its shape from its page (the notch has a fixed region)."""

    def begin_drag(self, fx: Any = None, fy: Any = None, kind: Any = "mouse", page_width: Any = 0,
                   page_height: Any = 0) -> None:
        """Host-followed dragging is the Astro Buddy's (the notch page sends sideways steps with drag(dx))."""

    def follow_drag(self, total_dx: Any = None, total_dy: Any = None) -> None:
        pass

    def end_drag(self) -> None:
        pass

    def reset_user_choice(self) -> None:
        self.user_hidden = False

    def destroy(self) -> None:
        with self._lock:
            window, self.window, self.mode = self.window, None, "hidden"
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass


def _notch_monitor(window: Any, user32) -> tuple[int, int, int, int]:   # pragma: no cover - needs the host GUI
    import ctypes
    from ctypes import wintypes

    import webview

    from services.control_indicator import desktop_frame_geometry
    _bounds, monitors = desktop_frame_geometry(user32)
    monitor = monitors[0]
    owner = webview.windows[0] if webview.windows else None
    if owner is not None and owner is not window and owner.native:
        rect = wintypes.RECT()
        if user32.GetWindowRect(int(owner.native.Handle.ToInt64()), ctypes.byref(rect)):
            cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
            monitor = next((m for m in monitors if m[0] <= cx < m[2] and m[1] <= cy < m[3]), monitor)
    return tuple(monitor)


def _move_window(window: Any, dx: int) -> int:                        # pragma: no cover - needs the host GUI
    """Move the notch window sideways (top edge kept); return the new offset from centre in logical px."""
    import ctypes
    from ctypes import wintypes

    from services.control_indicator import _win32
    user32 = _win32()["user32"]
    hwnd = int(window.native.Handle.ToInt64())
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    monitor = _notch_monitor(window, user32)
    x = max(monitor[0], min(monitor[2] - width, rect.left + dx))
    user32.SetWindowPos(hwnd, -1, x, rect.top, 0, 0, 0x0001 | 0x0010)          # NOSIZE | NOACTIVATE, topmost
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(wintypes.HWND(hwnd)) or 96
    except Exception:
        dpi = 96
    centre = monitor[0] + (monitor[2] - monitor[0]) // 2
    return round(((x + width / 2) - centre) * 96 / dpi)


def _notch_region(width: int, height: int, ear: int, radius: int, ear_height: int = 0):   # pragma: no cover - needs the host GUI
    """Window region: the body (square top, rounded bottom) plus two outward-curved ears along the top edge."""
    import ctypes
    from ctypes import wintypes
    gdi32 = ctypes.windll.gdi32
    for name, args in (("CreateRectRgn", 4), ("CreateRoundRectRgn", 6), ("CreateEllipticRgn", 4)):
        getattr(gdi32, name).restype = wintypes.HRGN
        getattr(gdi32, name).argtypes = [ctypes.c_int] * args
    gdi32.CombineRgn.argtypes = [wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, ctypes.c_int]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    OR, DIFF = 2, 4
    region = gdi32.CreateRoundRectRgn(ear, 0, width - ear + 1, height + 1, 2 * radius, 2 * radius)
    parts = [gdi32.CreateRectRgn(ear, 0, width - ear, max(1, height - radius))]          # square top of the body
    ear_height = ear_height or ear
    for left, centre_x in ((0, 0), (width - ear, width)):
        corner = gdi32.CreateRectRgn(left, 0, left + ear, ear_height)
        circle = gdi32.CreateEllipticRgn(centre_x - ear, 0, centre_x + ear + 1, 2 * ear_height + 1)
        gdi32.CombineRgn(corner, corner, circle, DIFF)                                  # concave = outward curve
        gdi32.DeleteObject(circle)
        parts.append(corner)
    for part in parts:
        gdi32.CombineRgn(region, region, part, OR)
        gdi32.DeleteObject(part)
    return region


def _place_window(window: Any, size: tuple[int, int], offset: int = 0) -> None:   # pragma: no cover - needs the host GUI
    """Resize/move OUR notch window in physical pixels on the monitor that shows the main Jupiter window, without
    activating it (SetWindowPos + SWP_NOACTIVATE)."""
    import ctypes
    from ctypes import wintypes

    import webview

    from services.control_indicator import _win32, desktop_frame_geometry
    user32 = _win32()["user32"]
    hwnd = int(window.native.Handle.ToInt64())
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(wintypes.HWND(hwnd)) or 96
    except Exception:
        dpi = 96
    width, height = int(size[0] * dpi / 96), int(size[1] * dpi / 96)
    monitor = _notch_monitor(window, user32)
    x, y = notch_position(monitor, (width, height), gap=round(TOP_GAP * dpi / 96), offset=round(offset * dpi / 96))
    user32.SetWindowPos(hwnd, -1, x, y, width, height, 0x0010 | 0x0040)          # NOACTIVATE | SHOWWINDOW, topmost
    # Windows 11 painted its frame/backdrop as a grey square under the clipped capsule (user screenshot, 17 Oct):
    # no DWM non-client rendering, no rounding, no border, no backdrop for this window.
    try:
        dwm = ctypes.windll.dwmapi
        for attribute, value in ((2, 2), (33, 1), (34, 0xFFFFFFFE), (38, 1)):
            setting = ctypes.c_uint(value)
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attribute, ctypes.byref(setting), 4)
    except Exception:
        pass
    # Clip the window to the notch shape (rounded bottom, outward curves at the top edge): the area outside it is not
    # part of the window at all.
    radius = int(CORNER["expanded" if height > SIZES["compact"][1] * dpi / 96 + 4 else "compact"] * dpi / 96)
    ctypes.windll.user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
    region = _notch_region(width, height, max(3, round(EAR * dpi / 96)), radius, max(2, round(EAR_HEIGHT * dpi / 96)))
    if region:
        ctypes.windll.user32.SetWindowRgn(wintypes.HWND(hwnd), region, True)    # the system owns the region now


def _default_window_factory(owner: NotchOverlay) -> Any:              # pragma: no cover - needs the host GUI
    from services.control_indicator import native_enabled
    if not native_enabled():
        return None
    import webview
    width, height = SIZES["compact"]
    window = webview.create_window("Pixie - Jupiter Buddy", html=NOTCH_HTML, js_api=NotchApi(owner), width=width,
                                   height=height, x=0, y=0, min_size=(60, 24), resizable=False, frameless=True,
                                   easy_drag=False,
                                   on_top=True, focus=False, transparent=False, shadow=False, hidden=True,
                                   background_color="#000000")

    def ready():
        owner._push({"mode": owner.mode, **{k: v for k, v in owner.state.items() if v not in (None, "")}})
        owner._resize()
    window.events.loaded += ready
    window.events.closed += lambda: setattr(owner, "window", None)
    return window


# ── Astro Buddy (marker 204): Juno, the 3D robot desktop buddy ────────────────────────────────────────────────────────
# Same host behaviour as the notch (follow loop, click to expand, open / small / hide), with a different page
# (windows_app/buddy/astro.html?mode=desk, served by Jupiter's own backend). Marker 205:
# - only Juno is visible: the window is clipped to the silhouette the page measures after each render (WebView2 cannot
#   make a window see-through; pywebview's transparent=True showed the form's grey-white background);
# - it starts centred at the top of the screen and can be dragged anywhere on the monitor (double-click = centre again);
#   the position lasts for this app session only and is separate from Pixie's saved top-edge offset.
# Marker 211 (user, 24 Oct: "Juno is not dragged to the top of the screen, and when clicked its size increases; it should
# pop out the transcription and buttons like Pixie"):
# - the window is one fixed frame that never resizes, with Juno's own box (the stage) at a fixed spot inside it: Juno
#   keeps exactly his size and place when the panel opens (the old resize re-framed the model ~30% bigger and moved it);
# - expanded = a Pixie-style panel (status, the live transcript, the task checklist, open / small / hide) under Juno, or
#   above him near the bottom of the screen, slid sideways to stay on the monitor;
# - the stage's empty margins may hang off the screen, so Juno himself reaches every edge (his head sits 46-50 px down
#   the stage, which kept him that far from the top).
ASTRO_STAGE = (132, 156)            # logical px
ASTRO_ROOM = (24, 46, 18, 16)       # empty stage margins around Juno (left, top, right, bottom), measured over his poses
ASTRO_HOME_LIFT = 44                # home: his head ~8-12 px under the top edge (his idle float moves it ~4 px)
ASTRO_TOP_GAP = 6
ASTRO_PANEL_WIDTH = 340
ASTRO_PANEL_BASE = 84               # status row + the two transcript lines
ASTRO_PANEL_ROWS = 5                # checklist rows shown at once (the page keeps the current step in view)
ASTRO_PANEL_GAP = 4
ASTRO_PANEL_TUCK = (12, 20)         # the panel tucks into the stage's empty bottom (below) / top (above) margin
SHAPE_MAX_RUNS = 12000


def astro_panel_height(checklist_rows: int = 0) -> int:
    rows = min(ASTRO_PANEL_ROWS, max(0, int(checklist_rows or 0)))
    return ASTRO_PANEL_BASE + (6 + CHECKLIST_ROW * rows if rows else 0)


_PANEL_MAX = astro_panel_height(ASTRO_PANEL_ROWS)
# wide enough for the panel to slide fully onto the screen even when Juno hangs off an edge by his side margin
ASTRO_STAGE_AT = (ASTRO_PANEL_WIDTH - ASTRO_STAGE[0] + max(ASTRO_ROOM[0], ASTRO_ROOM[2]),
                  _PANEL_MAX + ASTRO_PANEL_GAP - ASTRO_PANEL_TUCK[1])
ASTRO_FRAME = (2 * ASTRO_STAGE_AT[0] + ASTRO_STAGE[0],
               ASTRO_STAGE_AT[1] + ASTRO_STAGE[1] - ASTRO_PANEL_TUCK[0] + ASTRO_PANEL_GAP + _PANEL_MAX)
ASTRO_SIZES = {"compact": ASTRO_FRAME, "expanded": ASTRO_FRAME}


def _px(value: float, dpi: int) -> int:
    return int(round(value * (dpi or 96) / 96.0))


def astro_layout(monitor: tuple[int, int, int, int], dpi: int, offset: tuple[int, int],
                 panel_rows: int | None = None) -> dict[str, Any]:
    """Physical px. Juno's stage at its home (top centre) + ``offset``, kept on ``monitor`` (its empty margins may hang
    off), the fixed frame (the window) around it and, when expanded (``panel_rows`` = checklist rows), where the panel
    goes inside the frame: under Juno, above him when it does not fit below, always fully on the monitor."""
    left, top, right, bottom = (int(v) for v in monitor)
    sw, sh = _px(ASTRO_STAGE[0], dpi), _px(ASTRO_STAGE[1], dpi)
    rl, rt, rr, rb = (_px(v, dpi) for v in ASTRO_ROOM)
    home_x, home_y = left + (right - left - sw) // 2, top + _px(ASTRO_TOP_GAP, dpi) - _px(ASTRO_HOME_LIFT, dpi)
    x = max(left - rl, min(right - sw + rr, home_x + int(offset[0])))
    y = max(top - rt, min(bottom - sh + rb, home_y + int(offset[1])))
    fx, fy = x - _px(ASTRO_STAGE_AT[0], dpi), y - _px(ASTRO_STAGE_AT[1], dpi)
    fw, fh = _px(ASTRO_FRAME[0], dpi), _px(ASTRO_FRAME[1], dpi)
    layout: dict[str, Any] = {"window": (fx, fy, fw, fh), "stage": (x, y), "home": (home_x, home_y),
                              "offset": (x - home_x, y - home_y), "panel": None, "side": ""}
    if panel_rows is not None:
        pw, ph = _px(ASTRO_PANEL_WIDTH, dpi), _px(astro_panel_height(panel_rows), dpi)
        below = y + sh - _px(ASTRO_PANEL_TUCK[0], dpi) + _px(ASTRO_PANEL_GAP, dpi)
        above = y + _px(ASTRO_PANEL_TUCK[1], dpi) - _px(ASTRO_PANEL_GAP, dpi) - ph
        side = "below" if below + ph <= bottom or above < top else "above"
        px = max(fx, min(fx + fw - pw, max(left, min(right - pw, x + (sw - pw) // 2))))
        layout["panel"] = (px - fx, (below if side == "below" else above) - fy, pw, ph)
        layout["side"] = side
    return layout


def astro_page_layout(layout: dict[str, Any], dpi: int) -> dict[str, Any]:
    """What the page needs (logical px): its stage box and the panel's box inside the frame."""
    scale = (dpi or 96) / 96.0
    panel = layout.get("panel")
    return {"stage": [*ASTRO_STAGE_AT, *ASTRO_STAGE], "side": layout.get("side") or "",
            "panel": [int(round(v / scale)) for v in panel] if panel else None}


def astro_region_rects(shape: Any, width: int, height: int) -> list[tuple[int, int, int, int]]:
    """The page's silhouette grid (``cols`` x ``rows`` cells, ``runs`` = [row, first_col, last_col]) plus UI ``rects``
    (fractions of the page) -> window-client rectangles in physical pixels. Invalid input -> no rectangles."""
    if not isinstance(shape, dict) or width <= 0 or height <= 0:
        return []
    try:
        cols, rows = int(shape.get("cols") or 0), int(shape.get("rows") or 0)
    except (TypeError, ValueError):
        return []
    out: list[tuple[int, int, int, int]] = []
    runs = shape.get("runs") if isinstance(shape.get("runs"), list) else []
    if runs and not isinstance(runs[0], (list, tuple)):                 # flat [row, first, last, row, ...]
        runs = [runs[i:i + 3] for i in range(0, len(runs) - 2, 3)]
    if 0 < cols <= 4000 and 0 < rows <= 4000:
        for run in runs[:SHAPE_MAX_RUNS]:
            try:
                row, first, last = (int(v) for v in run)
            except (TypeError, ValueError):
                continue
            if not (0 <= row < rows and 0 <= first <= last < cols):
                continue
            out.append((first * width // cols, row * height // rows, (last + 1) * width // cols,
                        (row + 1) * height // rows))
    rects = shape.get("rects") if isinstance(shape.get("rects"), list) else []
    for rect in rects[:16]:
        try:
            left, top, right, bottom = (min(1.0, max(0.0, float(v))) for v in rect)
        except (TypeError, ValueError):
            continue
        if right > left and bottom > top:
            out.append((int(left * width), int(top * height), int(right * width) + 1, int(bottom * height) + 1))
    return out


def astro_figure_only(shape: Any) -> Any:
    """Juno's own part of a page shape (its first ``figure_runs`` runs, no panel): the region right after "make small",
    before the page has re-measured."""
    if not isinstance(shape, dict) or not isinstance(shape.get("runs"), list):
        return shape
    try:
        count = int(shape.get("figure_runs"))
    except (TypeError, ValueError):
        return shape
    runs = shape["runs"]
    flat = bool(runs) and not isinstance(runs[0], (list, tuple))
    return {**shape, "runs": runs[:max(0, count) * 3] if flat else runs[:max(0, count)], "rects": []}


def astro_page_url(main_url: str) -> str:
    from urllib.parse import urlsplit
    parts = urlsplit(str(main_url or ""))
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}/windows/buddy/astro.html?mode=desk"


class AstroOverlay(NotchOverlay):
    variant = "astro"

    def __init__(self, *, page_url: Callable[[], str], window_factory=None, placer=None, mover=None, shaper=None,
                 hider=None, shower=None, pointer=None, button_down=None, metrics=None):
        self._page_url = page_url
        self._pointer = pointer or _astro_pointer
        self._button_down = button_down or _left_button_down
        self._metrics = metrics or _astro_metrics
        self._drag_thread: threading.Thread | None = None
        self._drag_grab: tuple[float, float] | None = None       # where on the window it was grabbed (0..1)
        self._touch_drag: tuple[int, int, float, float] | None = None   # touch / pen: left, top, px per page px
        self._mover = mover or self._host_move
        self._shaper = shaper or _shape_astro_window
        self._hider = hider or _hide_astro_window
        self._shower = shower or _show_astro_window
        self._layout: dict[str, Any] | None = None                # the last placement (physical px) + what the page got
        self.shape: Any = None
        super().__init__(window_factory=window_factory or _astro_window_factory, placer=placer or self._host_place)
        self.offset_y = 0                                          # logical px from Juno's home spot (top centre)

    def _size(self) -> tuple[int, int]:
        return ASTRO_SIZES.get(self.mode, (0, 0))

    def _panel_rows(self) -> int | None:
        return len(self.state.get("checklist") or []) if self.mode == "expanded" else None

    @staticmethod
    def load_position() -> int:                                    # session only: always starts centred
        return 0

    def save_position(self) -> None:
        pass

    def _resize(self) -> None:
        window = self.window
        if window is None:
            return
        # Not pywebview's show() (nor the form's Show()): both activate the window, and the Astro stays up for the
        # whole Live session, so it took the keyboard focus away from Jupiter every time it appeared. ShowWindow with
        # SW_SHOWNOACTIVATE sends WM_SHOWWINDOW, so WinForms / WebView2 know the page is visible (SetWindowPos alone
        # left document.hidden = true: rendering paused, one frozen frame) and nothing is activated. The window is
        # placed and clipped first, while still hidden.
        try:
            if self.mode == "hidden":
                self._hider(window)
                return
            self._placer(window, self._size(), (self.offset, self.offset_y), self.shape)
            self._shower(window)
        except Exception:
            pass

    def _apply_layout(self, layout: dict[str, Any], dpi: int) -> dict[str, Any]:
        """Remember a placement and tell the page where its stage and panel are (only when that changed)."""
        page = astro_page_layout(layout, dpi)
        if self._layout is None or self._layout.get("page") != page:
            self._push({"layout": page})
        self._layout = {**layout, "dpi": dpi, "page": page}
        return layout

    def _host_place(self, window: Any, size: tuple[int, int], offset: tuple[int, int], shape: Any) -> None:
        dpi, monitor, _rect = self._metrics(window)
        layout = self._apply_layout(astro_layout(monitor, dpi, (_px(offset[0], dpi), _px(offset[1], dpi)),
                                                 self._panel_rows()), dpi)
        scale = dpi / 96.0
        self.offset, self.offset_y = (int(round(v / scale)) for v in layout["offset"])     # clamped spot kept
        # "make small": straight to Juno's own pixels (the page's next shape has no panel either)
        region = astro_region_rects(astro_figure_only(shape) if self.mode == "compact" else shape,
                                    *layout["window"][2:])
        _place_astro_window(window, layout["window"], region)

    def _host_move(self, window: Any, dx: int, dy: int) -> tuple[int, int]:
        """Move Juno by (dx, dy) physical px (kept on the monitor; the panel follows, flips above / below and slides to
        stay on screen); return his new logical offset from home."""
        dpi, monitor, rect = self._metrics(window)
        home_x, home_y = astro_layout(monitor, dpi, (0, 0))["home"]
        stage_x, stage_y = rect[0] + _px(ASTRO_STAGE_AT[0], dpi), rect[1] + _px(ASTRO_STAGE_AT[1], dpi)
        layout = self._apply_layout(astro_layout(monitor, dpi, (stage_x - home_x + int(dx), stage_y - home_y + int(dy)),
                                                 self._panel_rows()), dpi)
        _set_astro_window_pos(window, *layout["window"][:2])
        scale = dpi / 96.0
        return int(round(layout["offset"][0] / scale)), int(round(layout["offset"][1] / scale))

    def move_by(self, dx: int, dy: int = 0) -> None:
        window = self.window
        if window is None or not (dx or dy):
            return
        # pywebview runs each js_api call on its own thread: moves are serialised (read rect + move is not atomic)
        with self._lock:
            try:
                self.offset, self.offset_y = self._mover(window, int(dx), int(dy))
            except Exception:
                pass

    def begin_drag(self, fx: Any = None, fy: Any = None, kind: Any = "mouse", page_width: Any = 0,
                   page_height: Any = 0) -> None:
        """Pointer down on the astronaut at (fx, fy) = fractions of the page: keep that point under the cursor until
        the button is released (a fast pointer leaves the silhouette region, so the page stops getting moves; and
        this call arrives a moment after the press, when the cursor may already have moved)."""
        window = self.window
        try:
            grab = (min(1.0, max(0.0, float(fx))), min(1.0, max(0.0, float(fy))))
        except (TypeError, ValueError):
            return
        if window is None:
            return
        if str(kind or "mouse") != "mouse":
            # A finger or pen does not hold the mouse button: follow the page's pointer moves instead (scaled from
            # page pixels to this window's own screen pixels).
            try:
                _cx, _cy, left, top, width, height = (int(v) for v in self._pointer(window))
                sx = width / float(page_width) if float(page_width or 0) > 0 else 1.0
                sy = height / float(page_height) if float(page_height or 0) > 0 else 1.0
            except Exception:
                return
            self._touch_drag = (left, top, sx, sy)
            return
        self._touch_drag = None
        self._drag_grab = grab
        if self._drag_thread is None or not self._drag_thread.is_alive():
            self._drag_thread = threading.Thread(target=self._drag_loop, name="jupiter-astro-drag", daemon=True)
            self._drag_thread.start()

    def _drag_loop(self) -> None:
        deadline = time.monotonic() + 120
        while self._drag_grab is not None and time.monotonic() < deadline:
            try:
                held = self._button_down()
            except Exception:
                held = False
            self.follow_drag()
            if not held:
                break
            time.sleep(0.015)
        self._drag_grab = None

    def follow_drag(self, total_dx: Any = None, total_dy: Any = None) -> None:
        """While dragging. Mouse: the grabbed point of the window goes to the cursor (summing the page's own small
        steps lost about a quarter of a real drag and depended on DPI; the host's cursor and window positions share one
        coordinate space). Touch / pen: start position + the page's total movement since the press."""
        touch = self._touch_drag
        if touch is not None:
            if total_dx is None or total_dy is None:
                return
            with self._lock:
                window = self.window
                if window is None:
                    return
                try:
                    _cx, _cy, left, top, _w, _h = (int(v) for v in self._pointer(window))
                    dx = round(touch[0] + float(total_dx) * touch[2]) - left
                    dy = round(touch[1] + float(total_dy) * touch[3]) - top
                    if dx or dy:
                        self.offset, self.offset_y = self._mover(window, dx, dy)
                except Exception:
                    pass
            return
        with self._lock:
            window, grab = self.window, self._drag_grab
            if window is None or grab is None:
                return
            try:
                cx, cy, left, top, width, height = (int(v) for v in self._pointer(window))
                dx = round(cx - grab[0] * width) - left
                dy = round(cy - grab[1] * height) - top
                if abs(dx) > 1 or abs(dy) > 1:
                    self.offset, self.offset_y = self._mover(window, dx, dy)
            except Exception:
                pass

    def end_drag(self) -> None:
        self._touch_drag = None

    def recenter(self) -> None:
        with self._lock:
            self._drag_grab = None                                 # a finishing drag must not pull it back
            self._touch_drag = None
            self.offset = self.offset_y = 0
            if self.mode != "hidden":
                self._resize()

    def set_shape(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        self.shape = payload
        window = self.window
        if window is None or self.mode == "hidden":
            return
        try:
            self._shaper(window, payload)
        except Exception:
            pass


def _astro_dpi(hwnd: int) -> int:                                       # pragma: no cover - host GUI
    import ctypes
    from ctypes import wintypes
    try:
        return ctypes.windll.user32.GetDpiForWindow(wintypes.HWND(hwnd)) or 96
    except Exception:
        return 96


def _astro_metrics(window: Any) -> tuple[int, tuple[int, int, int, int], tuple[int, int, int, int]]:  # pragma: no cover
    """(dpi, monitor rect, window rect) in this process's physical screen pixels."""
    import ctypes
    from ctypes import wintypes

    from services.control_indicator import _win32
    user32 = _win32()["user32"]
    hwnd = int(window.native.Handle.ToInt64())
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return _astro_dpi(hwnd), _notch_monitor(window, user32), (rect.left, rect.top, rect.right, rect.bottom)


def _apply_astro_region(hwnd: int, rects: list[tuple[int, int, int, int]], width: int, height: int,
                        redraw: bool = True) -> None:                      # pragma: no cover - host GUI
    """Clip the window to ``rects``. Before the page has measured the model, a 2x2 dot in Juno's stage keeps the window
    "visible" to WebView2 (so it keeps rendering) without showing any background."""
    import ctypes
    from ctypes import wintypes
    gdi32 = ctypes.windll.gdi32
    gdi32.ExtCreateRegion.restype = wintypes.HRGN
    gdi32.ExtCreateRegion.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    if not rects:
        cx = width * (ASTRO_STAGE_AT[0] + ASTRO_STAGE[0] // 2) // ASTRO_FRAME[0]
        cy = height * (ASTRO_STAGE_AT[1] + ASTRO_STAGE[1] // 2) // ASTRO_FRAME[1]
        rects = [(cx - 1, cy - 1, cx + 1, cy + 1)]
    # one RGNDATA (header + rectangles) -> one region: hundreds of CombineRgn calls ~30 times a second were too slow
    count = len(rects)
    data = (ctypes.c_long * (8 + 4 * count))()
    data[0], data[1], data[2], data[3] = 32, 1, count, count * 16             # dwSize, RDH_RECTANGLES, nCount, nRgnSize
    data[4], data[5], data[6], data[7] = 0, 0, width, height                 # rcBound
    for i, (left, top, right, bottom) in enumerate(rects):
        data[8 + 4 * i:12 + 4 * i] = [left, top, right, bottom]
    region = gdi32.ExtCreateRegion(None, ctypes.sizeof(data), data)
    if not region:
        return
    ctypes.windll.user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
    if not ctypes.windll.user32.SetWindowRgn(wintypes.HWND(hwnd), region, bool(redraw)):
        gdi32.DeleteObject(region)                                         # on success the system owns it


def _shape_astro_window(window: Any, shape: Any) -> None:                # pragma: no cover - host GUI
    import ctypes
    from ctypes import wintypes
    hwnd = int(window.native.Handle.ToInt64())
    rect = wintypes.RECT()
    ctypes.windll.user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    _apply_astro_region(hwnd, astro_region_rects(shape, width, height), width, height)


def _place_astro_window(window: Any, rect: tuple[int, int, int, int],
                        region: list[tuple[int, int, int, int]]) -> None:  # pragma: no cover - host GUI
    import ctypes
    from ctypes import wintypes

    from services.control_indicator import _win32
    user32 = _win32()["user32"]
    hwnd = int(window.native.Handle.ToInt64())
    x, y, width, height = (int(v) for v in rect)
    # the region first (no redraw), then the move: both land in the same composed frame
    _apply_astro_region(hwnd, region, width, height, redraw=False)
    user32.SetWindowPos(hwnd, -1, x, y, width, height, 0x0010)                  # NOACTIVATE, topmost (shown after)
    try:
        dwm = ctypes.windll.dwmapi
        for attribute, value in ((2, 2), (33, 1), (34, 0xFFFFFFFE), (38, 1)):
            setting = ctypes.c_uint(value)
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attribute, ctypes.byref(setting), 4)
    except Exception:
        pass


def _set_astro_window_pos(window: Any, x: int, y: int) -> None:          # pragma: no cover - host GUI
    from services.control_indicator import _win32
    user32 = _win32()["user32"]
    user32.SetWindowPos(int(window.native.Handle.ToInt64()), -1, int(x), int(y), 0, 0,
                        0x0001 | 0x0010)                                      # NOSIZE | NOACTIVATE, topmost


def _show_astro_window(window: Any) -> None:                            # pragma: no cover - host GUI
    import ctypes
    ctypes.windll.user32.ShowWindow(int(window.native.Handle.ToInt64()), 4)          # SW_SHOWNOACTIVATE


def _astro_pointer(window: Any) -> tuple[int, int, int, int, int, int]:   # pragma: no cover - host GUI
    """(cursor x, cursor y, window left, top, width, height) in this process's own screen coordinates."""
    import ctypes
    from ctypes import wintypes
    point, rect = wintypes.POINT(), wintypes.RECT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    ctypes.windll.user32.GetWindowRect(int(window.native.Handle.ToInt64()), ctypes.byref(rect))
    return point.x, point.y, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _left_button_down() -> bool:                                        # pragma: no cover - host GUI
    import ctypes
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)          # VK_LBUTTON


def _hide_astro_window(window: Any) -> None:                            # pragma: no cover - host GUI
    import ctypes
    ctypes.windll.user32.ShowWindow(int(window.native.Handle.ToInt64()), 0)          # SW_HIDE


def _astro_window_factory(owner: "AstroOverlay") -> Any:              # pragma: no cover - needs the host GUI
    from services.control_indicator import native_enabled
    if not native_enabled():
        return None
    url = owner._page_url()
    if not url:
        return None
    import webview
    width, height = ASTRO_FRAME
    # Opaque dark window (any edge pixel blends into dark, never white); the silhouette region hides the rest.
    window = webview.create_window("Juno - Jupiter Buddy", url=url, js_api=NotchApi(owner), width=width, height=height,
                                   x=0, y=0, min_size=(60, 24), resizable=False, frameless=True, easy_drag=False,
                                   on_top=True, focus=False, transparent=False, shadow=False, hidden=True,
                                   background_color="#050913")

    def ready():
        owner._layout = None                                       # a (re)loaded page needs its layout again
        owner._push({"mode": owner.mode, **{k: v for k, v in owner.state.items() if v not in (None, "")}})
        owner._resize()
    window.events.loaded += ready
    window.events.closed += lambda: setattr(owner, "window", None)
    return window
