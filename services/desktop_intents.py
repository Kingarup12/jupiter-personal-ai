"""Marker 197: deterministic classifier for desktop/phone device requests (English + Hinglish).

    volume       "volume up by 20", "set the volume to 40%", "Spotify ki awaaz kam karo", "mute Chrome"
    media_key    "pause the music", "next song", "agla gaana"
    play         "play Billie Jean on Spotify", "Arijit Singh ke gaane YouTube pe chalao"
    ui_mode      "minimise yourself" (Jupiter goes to the notch), "come back", "expand/hide the notch"
    annotate     "circle the double bond on my screen", "explain what's on my screen", "screen pe error mark karo"
    clear        "clear the drawings", "marks hata do"
    phone_task   "on my phone, open Settings and tell me the battery level", "phone pe WhatsApp kholo"
    phone_stop   "stop the phone task"

Only explicit requests match; questions ("what is the volume of a cylinder?", "how do I mute Teams?") never do.
windows_app/desktop_assistant_intents.js is the same classifier for typed chat; tests/fixtures/desktop_intents.json
holds the shared examples both test suites run, so the two never drift apart.
"""

from __future__ import annotations

import re
from typing import Any

_FLAGS = re.I
_POLITE = re.compile(r"^(?:(?:hey|hi|hii+|hello|helo|ok(?:ay)?|arre|acha|achha|namaste|so|now|and|then)[\s,.!]+)*"
                     r"(?:jupiter[\s,.!]+)?"
                     r"(?:(?:please|pls|plz|can you|could you|would you|will you|kindly|zara|jaldi|just|"
                     r"i want you to|i need you to)\s+)*", _FLAGS)
_TRAIL = re.compile(r"(?:[\s,.!?]+(?:please|pls|plz|jupiter|yaar|na|now|abhi|for me))*[\s.!?।]*$", _FLAGS)
_QUESTION = re.compile(r"^(?:how|why|what is the volume|what's the volume|when|is it|are you|do you|does|did|should|"
                       r"can i|how do|how to|kaise)\b", _FLAGS)

# ── volume: a small parser instead of fixed sentences ("decrease my volume to 10%", "Spotify ki awaaz kam karo") ─────
_VOL = re.compile(r"\b(?:volume|vol|sound|audio|awaaz|aawaz|awaz|awaj|aawaaz|aawaj)\b", _FLAGS)
_LOUDNESS = re.compile(r"\b(?:louder|quieter|softer)\b|^(?:turn|crank)\s+(?:it|the\s+(?:music|song|video|tv))\s+(?:up|down)\b",
                       _FLAGS)
_UP_WORD = re.compile(r"\b(?:increase|raise|boost|up|louder|higher|badha\w*|tez|tej|zyada|jyada|upar|high)\b", _FLAGS)
_DOWN_WORD = re.compile(r"\b(?:decrease|reduce|lower|drop|down|quieter|softer|less|kam|dheere|dhire|dheem\w*|dhim\w*|"
                        r"neeche|niche|ghata\w*|low)\b", _FLAGS)
_MUTE_WORD = re.compile(r"\b(?P<un>un)?mute\b|\b(?:awaaz|aawaz|awaz|sound|volume)\s+(?P<sw>band|chalu|on|off)\b", _FLAGS)
_NUMBER = re.compile(r"(?<![\d.])(\d{1,3})\s*(?:%|percent|per\s*cent|pratishat)?(?![\d.])", _FLAGS)
_TO_NUMBER = re.compile(r"\b(?:to|at|till|until|upto|up\s+to|around|about|pe|par|tak)\s+(?:about\s+|around\s+)?\d", _FLAGS)
_SET_HINDI = re.compile(r"\d{1,3}\s*(?:%|percent|per\s*cent|pratishat)?\s+(?:pe\s+|par\s+)?(?:kar|karo|kardo|kar\s*do|"
                        r"kar\s*de|rakho|rakh\s*do|set)\b", _FLAGS)
_BY_NUMBER = re.compile(r"\bby\s+(?:about\s+|around\s+)?\d", _FLAGS)
_MAX = re.compile(r"\b(?:full|max|maximum|poora|puri|pura)\b", _FLAGS)
_MIN = re.compile(r"\b(?:min|minimum|zero)\b", _FLAGS)
_NOT_AUDIO = re.compile(r"\b(?:cylinder|cube|cuboid|sphere|cone|prism|pyramid|tank|container|bottle|cm|mm|litres?|liters?|"
                        r"ml|cubic|m3|formula|calculate|area|density|mass|gas|liquid|book|series|edition|trading)\b", _FLAGS)
_STATEMENT = re.compile(r"\b(?:is|was|are|were|seems|shows?|showing|stays?|stuck|still|already)\b", _FLAGS)
_IMPERATIVE = re.compile(r"^(?:set|make|put|turn|keep|change|increase|decrease|raise|lower|reduce|boost|drop|bring|crank|"
                         r"mute|unmute|volume|sound|awaaz|aawaz|awaz)\b", _FLAGS)
KNOWN_APPS = ("youtube music", "apple music", "google chrome", "microsoft edge", "chrome", "edge", "firefox", "brave",
              "opera", "spotify", "discord", "zoom", "teams", "vlc", "whatsapp", "telegram", "skype", "youtube",
              "netflix", "prime video", "jupiter")
_KNOWN_APP = re.compile(r"\b(" + "|".join(re.escape(a) for a in KNOWN_APPS) + r")\b", _FLAGS)
_NOT_APP = frozenset("""my the this that it its your our pc computer laptop system speaker speakers headphone headphones
device windows everything all me volume sound audio awaaz master overall main total full max maximum min minimum
a an set make put turn keep change increase decrease raise lower reduce boost drop bring crank mute unmute up down
please thoda thodi aur jara zara""".split())
_APP_OF = re.compile(r"\b(?:of|for|in|on)\s+(?:the\s+)?([a-z][\w.+-]{1,20})\b", _FLAGS)
_APP_KI = re.compile(r"^(?:the\s+)?([a-z][\w.+-]{1,20})\s+(?:ki|ka|ke)\s+(?:awaaz|aawaz|awaz|volume|sound)\b", _FLAGS)
_APP_BEFORE = re.compile(r"\b([a-z][\w.+-]{1,20})(?:'s)?\s+(?:volume|sound|awaaz)\b", _FLAGS)
_APP_MUTE = re.compile(r"^(?:un)?mute\s+(?:the\s+)?([a-z][\w.+-]{1,20})$", _FLAGS)
_APP_LOUD = re.compile(r"^make\s+(?:the\s+)?([a-z][\w.+-]{1,20})\s+(?:louder|quieter|softer)$", _FLAGS)


def _volume_app(said: str) -> str:
    known = _KNOWN_APP.search(said)
    if known:
        return known.group(1)
    for pattern in (_APP_KI, _APP_MUTE, _APP_LOUD, _APP_OF, _APP_BEFORE):
        match = pattern.search(said)
        if match and match.group(1).casefold() not in _NOT_APP and not match.group(1).isdigit():
            return match.group(1)
    return ""


# Marker 214 (real WT 24 Oct: "can you increase my brightness" got "I can't"): the laptop screen's brightness.
_BRIGHT = re.compile(r"\b(?:brightness|brighter|screen\s+light|roshni|chamak)\b|\b(?:dim|darken)\s+(?:the\s+|my\s+)?screen\b"
                     r"|ब्राइटनेस|रोशनी", _FLAGS)
_BRIGHT_UP = re.compile(r"\b(?:increase|raise|up|more|higher|brighter|max(?:imum)?|full|badha\w*|zyada|tez)\b|बढ़ा", _FLAGS)
_BRIGHT_DOWN = re.compile(r"\b(?:decrease|lower|reduce|down|less|dim|darken|min(?:imum)?|kam|ghata\w*)\b|कम|घटा", _FLAGS)


def _brightness(said: str) -> dict[str, Any] | None:
    if not _BRIGHT.search(said) or (_STATEMENT.search(said) and not _IMPERATIVE.match(said)):
        return None
    up, down = _BRIGHT_UP.search(said), _BRIGHT_DOWN.search(said)
    sign = (1 if (up and (not down or up.start() < down.start())) else -1) if (up or down) else 0
    number = re.search(r"(\d{1,3})\s*(?:%|percent|per\s*cent|pratishat)?", said, _FLAGS)
    if number:
        value = max(0, min(100, int(number.group(1))))
        if re.search(r"\bby\s+\d", said, _FLAGS) and sign:
            return {"kind": "brightness", "delta": sign * max(1, value)}
        return {"kind": "brightness", "set": value}
    if re.search(r"\b(?:max(?:imum)?|full|100)\b", said, _FLAGS):
        return {"kind": "brightness", "set": 100}
    if re.search(r"\b(?:min(?:imum)?|lowest)\b", said, _FLAGS):
        return {"kind": "brightness", "set": 5}
    return {"kind": "brightness", "delta": sign * 20} if sign else None


def _volume(said: str) -> dict[str, Any] | None:
    """Volume change the user asked for, or None. "to N" sets N, "by N" changes by N, a direction alone moves 10."""
    if not (_VOL.search(said) or _LOUDNESS.search(said) or _MUTE_WORD.search(said)) or _NOT_AUDIO.search(said):
        return None
    if _STATEMENT.search(said) and not _IMPERATIVE.match(said):
        return None                                   # "my volume is at 52" reports, it does not ask
    app = _volume_app(said)
    up, down = _UP_WORD.search(said), _DOWN_WORD.search(said)
    sign = 0
    if up or down:
        sign = 1 if (up and (not down or up.start() < down.start())) else -1
    number = _NUMBER.search(said)
    mute = _MUTE_WORD.search(said)
    if mute and not number and not sign:
        muted = not mute.group("un") and (mute.group("sw") or "band").casefold() in {"band", "off"}
        return {"kind": "volume", "mute": muted, "app": app}
    if number:
        value = max(0, min(100, int(number.group(1))))
        if _BY_NUMBER.search(said):
            return {"kind": "volume", "delta": sign * max(1, value), "app": app} if sign else None
        if _TO_NUMBER.search(said) or _SET_HINDI.search(said) or not sign:
            return {"kind": "volume", "set": value, "app": app}
        return {"kind": "volume", "delta": sign * max(1, value), "app": app}
    if _MAX.search(said):
        return {"kind": "volume", "set": 100, "app": app}
    if _MIN.search(said):
        return {"kind": "volume", "set": 0, "app": app}
    if sign:
        return {"kind": "volume", "delta": sign * 10, "app": app}
    return None


_MEDIA = [
    (re.compile(r"^(?:pause|resume|unpause|stop|continue)\s+(?:the\s+)?(?:music|song|video|playback|audio|track|it|"
                r"spotify|youtube)$", _FLAGS), "play_pause"),
    (re.compile(r"^(?:play|resume)\s+(?:the\s+)?(?:music|song)(?:\s+again)?$|^play(?: it)?(?: again)?$", _FLAGS), "play_pause"),
    (re.compile(r"^(?:(?:play\s+)?(?:the\s+)?next|skip(?:\s+(?:this|it|the))?)(?:\s+(?:song|track|video))?$|"
                r"^(?:agla|agli|next)\s+(?:gaana|gana|song|video)(?:\s+(?:chalao|lagao|chala\s*do|laga\s*do|bajao))?$", _FLAGS), "next"),
    (re.compile(r"^(?:(?:play\s+)?(?:the\s+)?previous|go back to the (?:last|previous))\s+(?:song|track|video)$|"
                r"^(?:pichla|pichhla|pichli|previous)\s+(?:gaana|gana|song|video)(?:\s+(?:chalao|lagao|chala\s*do|laga\s*do|bajao))?$", _FLAGS), "previous"),
    (re.compile(r"^(?:gaana|gana|music|song|video)\s+(?:roko|rok\s*do|band\s+karo|band\s+kar\s*do|pause\s+karo|"
                r"pause\s+kar\s*do|chalao|chala\s*do|resume\s+karo|phir\s+se\s+chalao)$", _FLAGS), "play_pause"),
]

_PLAY_APP = r"(?P<app>youtube music|youtube|yt|spotify)"
_PLAY = [
    re.compile(rf"^(?:play|put on)\s+(?P<q>.+?)\s+(?:on|in|from|using)\s+{_PLAY_APP}$", _FLAGS),
    re.compile(rf"^(?:open\s+|play\s+|start\s+)?{_PLAY_APP}\s+(?:and\s+)?(?:then\s+)?play\s+(?P<q>.+)$", _FLAGS),
    # marker 214: "okay, like can you open Spotify and play something for me"
    re.compile(rf"^.*?\bopen\s+(?:the\s+|my\s+)?{_PLAY_APP}(?:\s+app)?\s*,?\s+and\s+(?:then\s+)?play\s+(?P<q>.+)$", _FLAGS),
    re.compile(rf"^(?P<q>.+?)\s+{_PLAY_APP}\s+(?:pe|par|mein|me|par se|pe se)\s+(?:bajao|baja\s*do|chalao|chala\s*do|"
               rf"laga\s*do|lagao|play\s+karo|play\s+kar\s*do|sunao)$", _FLAGS),
    # marker 217: "Spotify mein chogada tara chalao"
    re.compile(rf"^{_PLAY_APP}\s+(?:pe|par|mein|me)\s+(?P<q>.+?)\s+(?:bajao|baja\s*do|chalao|chala\s*do|laga\s*do|"
               rf"lagao|play\s+karo|play\s+kar\s*do|sunao)$", _FLAGS),
    re.compile(r"^(?:play|put on)\s+(?P<q>.+)$", _FLAGS),
    re.compile(r"^(?P<q>.+?)\s+(?:bajao|baja\s*do|play\s+karo|play\s+kar\s*do|sunao|laga\s*do)$", _FLAGS),
]
_NOT_MEDIA = re.compile(r"^(?:a\s+|some\s+)?(?:game|games|chess|ludo|cards|quiz|with me|along|role|pretend|trivia|"
                        r"a role|20 questions|antakshari|the fool|dumb|hide and seek|taali|tali|bell|ghanti|"
                        r"siti|seeti|whistle|dhol)\b", _FLAGS)
_GENERIC_MUSIC = re.compile(r"^(?:some\s+)?(?:music|songs?|gaana|gaane|gana)$", _FLAGS)
# Marker 215 (real WT 25 Oct: "pause the music" STARTED Spotify - the play/pause key is a toggle): the request says
# which one it wants, and the Windows app pauses / plays exactly that.
_MEDIA_PAUSE = re.compile(r"\b(?:pause|stop|roko|rok\s*do|band)\b|रोको|बंद", _FLAGS)


def _media_key(action: str, said: str) -> dict[str, Any]:
    if action != "play_pause":
        return {"kind": "media_key", "action": action}
    intent = {"kind": "media_key", "action": action, "want": "pause" if _MEDIA_PAUSE.search(said) else "play"}
    named = re.search(r"\b(spotify|youtube)\b", said, re.I)       # marker 218: "resume Spotify" means Spotify
    return {**intent, "app": named.group(1).casefold()} if named else intent
# Marker 214 (real WT 24 Oct: "play any song, I'm fine with that" got "I couldn't start the music without a specific
# artist"): "anything" means Jupiter picks - today's top hits.
_ANY_MUSIC = re.compile(r"^(?:any|some|something|anything|whatever|kuch|kuch\s+bhi|koi\s+bhi|a)"
                        r"(?:\s+(?:good|nice|random|other))?(?:\s+(?:song|songs|music|gaana|gaane|track|tracks))?$", _FLAGS)
ANY_MUSIC_QUERY = "today's top hits"
# Marker 217 (real WT 25 Oct: "Spotify mein dikh raha hoga. Chhoda chhod, C H O G A D A space bar T A R A ye laga rakha
# hai us par click karke play" was dropped, and Jupiter said the song was playing): a title spelled out letter by letter
# is the song's name, and with a play word the turn is a play request. "Play play play" is not a song called "play play".
_PLAY_WORD = re.compile(r"\b(?:play|bajao|baja\s*do|chalao|chala\s*do|lagao|laga\s*do|sunao)\b", _FLAGS)
_JUNK_QUERY = re.compile(r"^(?:(?:play|it|now|please|again|na|yaar|jaldi|karo|do|kar|abhi)[\s,.!]*)+$", _FLAGS)


def spelled_title(text: str) -> str:
    """"C H O G A D A space bar T A R A" -> "chogada tara"; "" when nothing is spelled out (3+ single letters)."""
    words, current, best = [], "", ""
    tokens = re.findall(r"[^\s,.;:!?-]+", str(text or ""))
    for index, token in enumerate(tokens + [""]):
        lowered = token.casefold().strip("{}[]()<>\"'")              # the transcript may write "{spacebar}"
        if len(lowered) == 1 and lowered.isalpha():
            current += lowered
            continue
        spacer = lowered in {"space", "spacebar"} or (lowered == "bar" and index
                                                       and tokens[index - 1].casefold().strip("{}[]()") == "space")
        if spacer and current:
            words.append(current)
            current = ""
            continue
        if current:
            words.append(current)
            current = ""
        if not spacer:
            title = " ".join(w for w in words if w)
            if sum(len(w) for w in words) >= 3 and len(title) > len(best):
                best = title
            words = []
    return best

_UI = [
    (re.compile(r"^(?:maximi[sz]e)\s+(?:yourself|jupiter|the\s+(?:app|window))$|^make\s+yourself\s+full\s*screen$|"
                r"^(?:go|become)\s+full\s*screen$|^full\s*screen\s+(?:ho\s+jao|karo)$", _FLAGS), "maximize"),
    (re.compile(r"^(?:expand|enlarge|grow)\s+(?:yourself|jupiter|the\s+(?:app|window))$|"
                r"^make\s+yourself\s+(?:bigger|larger|big)$|^(?:go|become)\s+(?:big|bigger)$|"
                r"^(?:bade|bada|badi)\s+ho\s+jao$", _FLAGS), "expand"),
    (re.compile(r"^(?:minimi[sz]e|shrink|hide)\s+(?:yourself|jupiter|the\s+(?:app|window))$|"
                r"^make\s+yourself\s+(?:smaller|small|tiny)$|"
                r"^(?:go|switch|move)\s+(?:to\s+)?(?:the\s+)?notch(?:\s+mode)?$|^notch\s+mode$|^go\s+small$|"
                r"^(?:chhot[ae]|chot[ae])\s+ho\s+jao$|^notch\s+(?:mein|me)\s+(?:jao|chale\s+jao|aa\s+jao)$", _FLAGS), "minimize"),
    (re.compile(r"^(?:come\s+back|restore\s+(?:yourself|the\s+(?:app|window))|"
                r"open\s+(?:the\s+)?full\s+(?:app|window|jupiter)|show\s+(?:the\s+)?full\s+(?:app|window)|"
                r"wapas\s+aao|vapas\s+aao|full\s+screen\s+(?:mein|me)\s+aao)$", _FLAGS), "restore"),
    (re.compile(r"^(?:expand|open|enlarge|grow)\s+(?:the\s+)?notch$|^show\s+(?:the\s+)?transcript\s+in\s+(?:the\s+)?notch$|"
                r"^notch\s+(?:bada|badi)\s+karo$", _FLAGS), "expanded"),
    (re.compile(r"^(?:collapse|shrink|minimi[sz]e)\s+(?:the\s+)?notch$|^notch\s+(?:chhota|chota|choti)\s+karo$", _FLAGS), "compact"),
    (re.compile(r"^(?:hide|close|remove)\s+(?:the\s+)?notch$|^notch\s+(?:hatao|band\s+karo|chhupao)$", _FLAGS), "hidden"),
]

_CAPABILITIES = re.compile(
    r"^(?:do\s+you\s+have\s+(?:access\s+to|control\s+(?:of|over))\s+(?:my|the|this)\s+(?:pc|computer|laptop|desktop|"
    r"system|machine|phone|mobile)$)|"
    r"^(?:control|use|operate|access|handle|run)\s+(?:my|the|this)\s+(?:pc|computer|laptop|desktop|system|machine|"
    r"phone|mobile)"
    r"(?:\s+for\s+me)?$|^what\s+(?:all\s+)?can\s+you\s+do\s+(?:on|with|in)\s+(?:my|the|this)\s+(?:pc|computer|laptop|"
    r"desktop|system)$|^(?:mera|meri|mere)\s+(?:pc|computer|laptop)\s+(?:control|chala|use)\s+(?:kar\s+sakte\s+ho|"
    r"karo|kar\s*do|sakte\s+ho|kar\s+sakti\s+ho)$", _FLAGS)
# "open Google Chrome in my laptop", "Notepad kholo". A known app always counts; any other name only with "app" or
# "on my laptop/phone", so Jupiter's own things ("open a new chat", "open settings") are never hijacked.
OPEN_APP_NAMES = frozenset("""chrome|google chrome|edge|microsoft edge|firefox|brave|opera|notepad|calculator|calc|paint|
file explorer|explorer|files|my files|windows settings|task manager|terminal|command prompt|powershell|word|microsoft word|
excel|microsoft excel|powerpoint|outlook|spotify|whatsapp|teams|zoom|discord|telegram|vlc|vs code|vscode|
visual studio code|steam|camera app|microsoft store|play store|clock|alarms|photos|gallery|snipping tool|control panel|
youtube|gmail|google maps|maps|instagram|calendar|contacts|messages|phone app|dialer|google""".replace("\n", "").split("|"))
_DEVICE_NOUN = r"(?:laptop|pc|computer|desktop|system|phone|mobile)"
_OPEN_APP = [
    re.compile(rf"^(?:open|launch|start|run|bring\s+up)\s+(?:up\s+)?(?:the\s+|my\s+)?(?P<app>[a-z][\w .+&'-]{{1,40}}?)"
               rf"(?P<suffix>\s+app(?:lication)?)?(?:\s+(?:on|in)\s+(?:my|the|this)\s+(?P<dev>{_DEVICE_NOUN}))?"
               rf"(?:\s+for\s+me)?$", _FLAGS),
    re.compile(rf"^(?:(?:mere|meray|apne)\s+)?(?:(?P<dev>{_DEVICE_NOUN})\s+(?:pe|par|mein|me)\s+)?"
               rf"(?P<app>[a-z][\w .+&'-]{{1,40}}?)(?P<suffix>\s+app)?\s+(?:kholo|khol\s*do|khol\s*de|open\s+karo|"
               rf"open\s+kar\s*do|chalu\s+karo|start\s+karo)$", _FLAGS),
]
_NOT_AN_APP = re.compile(r"^(?:a\s+|the\s+|new\s+)*(?:chat|new chat|history|memory|memories|settings|profile|camera|"
                         r"mic|microphone|live|transcript|sidebar|menu|notch|door|window|tab|link|url|website|file|folder|"
                         r"pdf|document|image|photo|it|this|that|conversation|account|notifications?)$", _FLAGS)


# "...you open calculator inside my laptop": a request cue, then open + a KNOWN app anywhere in the sentence.
_OPEN_KNOWN = re.compile(r"\b(?:can you|could you|would you|will you|please|kindly|you|jupiter)\b.{0,30}?\b(?:open|launch|start)"
                         r"\s+(?:the\s+|my\s+)?(" + "|".join(sorted((re.escape(n) for n in OPEN_APP_NAMES), key=len, reverse=True))
                         + r")\b(?!\s+(?:tab|file|folder|website|page|link|chat))", _FLAGS)


_CALCULATOR = re.compile(r"\b(?:calculator|calc)\b", _FLAGS)
_OPEN_THEN_MAXIMIZE = re.compile(r"^(?:open|launch|start)\s+(?:up\s+)?(?:the\s+|my\s+)?(?P<app>[a-z][\w .+&'-]{1,40}?)"
                                 r"(?:\s+app)?\s*(?:,|\s+and)\s+(?:then\s+)?(?:maximi[sz]e|make\s+it\s+full\s*screen)"
                                 r"(?:\s+it|\s+the\s+window)?$", _FLAGS)


# Marker 217 (real WT 25 Oct): "can you tell me how many applications are open in my laptop at the bottom by viewing the
# taskbar?" went to the slow screen agent ("results will appear on your screen") and was never answered;
# "Minimize the Jupiter WT application that is open right now" got "I didn't catch which window you mean".
_SCREEN_QUESTION = re.compile(
    r"\b(?:how\s+many|what|which|list|name|tell\s+me|kitne|kitni|kaun\s*kaun|kaun\s*se|kaunse|konse|kya\s+kya)\b"
    r".{0,60}\b(?:apps?|applications?|windows|programs?|softwares?)\b.{0,80}"
    r"\b(?:open|opened|running|chal\s+rah\w*|khul\w*|taskbar|screen|laptop|computer|pc|desktop)\b"
    r"|\bwhat(?:'s|\s+is)\s+(?:open\s+)?on\s+(?:my|the)\s+taskbar\b"   # screen CONTENT stays with the vision path
    r"|\bwhich\s+(?:app|application|window)\s+is\s+(?:in\s+front|open|active|focused|on\s+top)\b"
    r"|\btaskbar\s+(?:mein|me|pe|par)\s+(?:kya|kitne|kaun)", _FLAGS)
_SELF_WINDOW = re.compile(
    r"\b(?P<verb>minimi[sz]e|maximi[sz]e|restore|hide|bring\s+back|make\s+full\s*screen)\s+(?:the\s+|this\s+)?"
    r"(?:jupiter(?:\s+(?:wt|at|app|application|window))*|your\s+(?:own\s+)?(?:app|window)|this\s+(?:jupiter\s+)?app)\b"
    r"(?!\s+(?:website|tab|page|folder|file))", _FLAGS)


def _open_app(said: str) -> dict[str, Any] | None:
    for pattern in _OPEN_APP:
        match = pattern.match(said)
        if not match:
            continue
        app = " ".join(_group(match, "app").split()).strip(" .,'")
        if not app or _NOT_AN_APP.match(app):
            return None
        if app.casefold() in OPEN_APP_NAMES or _group(match, "suffix") or _group(match, "dev"):
            return {"kind": "open_app", "app": app}
        return None
    return None


_OPEN_NEW_WINDOW = re.compile(r"\b(?:open|launch|start)\s+(?:a\s+|one\s+more\s+)?(?:new|another|second)\s+("
                              + "|".join(sorted((re.escape(n) for n in OPEN_APP_NAMES), key=len, reverse=True))
                              + r")\s+(?:window|tab)\b", _FLAGS)


def _open_known_app(text: str) -> dict[str, Any] | None:
    said = " ".join(str(text or "").split())
    fresh = _OPEN_NEW_WINDOW.search(said)                            # marker 208: "open a new Chrome window"
    if fresh:
        return {"kind": "open_app", "app": fresh.group(1), "new_window": True}
    if _OPEN_DOMAIN.search(said) or _OPEN_SITE.search(said):         # marker 212: "... Edge and open youtube.com"
        return None                                                  # is a website in that browser, not just the app
    if re.search(r"\band\s+play\b", said, _FLAGS):                    # marker 214: "open Spotify and play something"
        return None
    match = _OPEN_KNOWN.search(said)
    return {"kind": "open_app", "app": match.group(1)} if match else None


# Marker 208 (real WT 23 Oct): "open Microsoft Edge ... it is already open but minimised, so maximize it" opened a
# second Edge; "close the window you just opened" / "maximize the existing Edge window" got "I can't".
_APP_ALT = "|".join(sorted((re.escape(n) for n in OPEN_APP_NAMES), key=len, reverse=True))
# Marker 213: a window belongs to an app, never to a website ("close the Doraemon ... Google search tab" closed
# nothing useful as "close the Google window")
_WINDOW_APP = re.compile(r"\b(" + "|".join(sorted((re.escape(n) for n in OPEN_APP_NAMES
                                                   - {"google", "youtube", "gmail", "google maps", "maps", "instagram"}),
                                                  key=len, reverse=True)) + r")\b", _FLAGS)
_NEW_WINDOW = re.compile(r"\b(?:new|another|second|naya|nayi|dusra|doosra)\s+(?:window|tab|one)\b", _FLAGS)
_MAXIMIZE_WORD = re.compile(r"\b(?:maximi[sz]e|full\s*screen|bada\s+karo|bada\s+kar\s*do)\b", _FLAGS)
_WINDOW_VERBS = (
    ("maximize", re.compile(r"\b(?:maximi[sz]e|make\s+(?:it\s+)?full\s*screen|full\s*screen\s+(?:karo|kar\s*do)|bada\s+(?:karo|kar\s*do))\b", _FLAGS)),
    ("minimize", re.compile(r"\b(?:minimi[sz]e|chhota\s+(?:karo|kar\s*do)|neeche\s+karo)\b", _FLAGS)),
    ("restore", re.compile(r"\b(?:restore|un-?minimi[sz]e|bring\s+back|wapas\s+(?:lao|khol\w*))\b|"
                           r"\bbring\s+(?:the\s+|my\s+)?[\w .+&'-]{2,40}?\s+back\b", _FLAGS)),   # marker 217
    ("close", re.compile(r"\b(?:close|shut)\b|\bband\s+(?:karo|kar\s*do|kar\s*de)\b", _FLAGS)),
)
_WINDOW_NOUN = re.compile(r"\b(?:window|windows|app|tab)\b|\b(?:it|that|this|that\s+one|this\s+one)\b", _FLAGS)
_WINDOW_LAST = re.compile(r"\b(?:new|that|this|last|other|extra|naya|nayi)\s+(?:window|one|tab)\b|\byou\s+(?:just\s+|currently\s+)?"
                          r"(?:opened|open|launched)\b|\b(?:jo|joh)\s+(?:abhi\s+|tumne\s+)?(?:khola|kholi|khole)\b", _FLAGS)
_WINDOW_EXISTING = re.compile(r"\b(?:existing|already\s+open|old|original|previous|purana|purani|pehle\s+wala|pehli)\b", _FLAGS)
_USER_DID = re.compile(r"\b(?:let\s+me|i\s+(?:have\s+|had\s+|already\s+)?|i'?ve|i'?ll|maine)\s*(?:just\s+)?"
                       r"(?:minimi[sz]ed?|maximi[sz]ed?|closed?|restored?|opened?)\b", _FLAGS)
_TAB_WORD = re.compile(r"\btabs?\b", _FLAGS)
_WINDOW_FREE = re.compile(r"\b(?:maximi[sz]e|minimi[sz]e|restore|bring\s+back|close|bring)\s+(?:the\s+|my\s+)?"
                          r"(?P<app>[a-z][\w.+&'-]*(?:\s+[a-z][\w.+&'-]*){0,2}?)"
                          r"(?:\s+(?:window|app|application))?(?:\s+back)?(?:\s+(?:for\s+me|please|now|right\s+now|again|"
                          r"immediately|too))*\s*$", _FLAGS)
# "close the door" is not a window: closing an app the classifier does not know needs "window"/"app" or a known name
_APP_NOUN = re.compile(r"\b(?:window|app|application)\b", _FLAGS)
_DESKTOP_APPS = {"claude", "chatgpt", "chat gpt", "copilot", "perplexity", "notion", "slack", "obsidian", "figma",
                 "postman", "blender", "onenote", "android studio", "task manager", "file explorer", "explorer"}
_FREE_NOT_APP = {"it", "this", "that", "them", "everything", "all", "yourself", "window", "app", "screen", "me",
                 "volume", "brightness", "notch", "the window", "all windows", "everything else", "this one", "that one"}
_WINDOW_CLAUSES = re.compile(r"[.?!।;]+|,\s*(?:and\s+)?(?:then\s+)?|\s+(?:and\s+then|and|then|aur\s+phir|aur|phir)\s+", _FLAGS)


def _window_steps(text: str) -> list[dict[str, str]]:
    """Window actions in the order the user asked for them (at most three)."""
    steps: list[dict[str, str]] = []
    for clause in _WINDOW_CLAUSES.split(" ".join(str(text or "").split())):
        clause = clause.strip(" ,")
        if not clause or _USER_DID.search(clause) or _QUESTION.match(clause) or _TAB_WORD.search(clause):
            continue                      # a TAB is not a window: "close this tab" must never close the whole window
        action = next((name for name, verb in _WINDOW_VERBS if verb.search(clause)), "")
        if not action:
            continue
        app_match = _WINDOW_APP.search(clause)
        app = app_match.group(1) if app_match else ""
        exact = False
        if not app:
            # Marker 217 (real chat 25 Oct: "can you maximize Claude?" -> "I cannot maximize Claude"): any open app's
            # name, matched to its window by the Windows app (close stays limited to known apps).
            free = _WINDOW_FREE.search(clause)
            name = free.group("app").strip() if free else ""
            closing_ok = action != "close" or name.casefold() in _DESKTOP_APPS or _APP_NOUN.search(clause)
            if (name and closing_ok and name.casefold() not in _FREE_NOT_APP
                    and not name.casefold().startswith("jupiter")):
                app, exact = name, action == "close"       # close: only the app's own executable, never a guess
        if action == "close" and _WINDOW_LAST.search(clause):
            steps.append({"action": "close", "app": "", "target": "last"})
        elif app or _WINDOW_NOUN.search(clause):
            target = "existing" if _WINDOW_EXISTING.search(clause) else ("last" if _WINDOW_LAST.search(clause) else "")
            target = "exe" if exact else target
            steps.append({"action": action, "app": app, "target": target})
        if len(steps) == 3:
            break
    return steps


def _window(text: str) -> dict[str, Any] | None:
    steps = _window_steps(text)
    return {"kind": "window", "steps": steps} if steps else None


# Marker 210 (real WT 24 Oct): "Tum computer use kar sakte ho na?" -> "Haan ... Bataiye, aap kya karwana chahte hain?"
# -> "Technology Gyan." -> "Main abhi yeh action nahi kar pa rahi hoon". Jupiter's question + a bare name = search it.
_ASKS_WHAT_TO_DO = re.compile(r"\b(?:kya\s+karwana|kya\s+karna\s+(?:hai|chahte)|kya\s+karu[n]?|kya\s+kholu[n]?|"
                              r"what\s+(?:would|do)\s+you\s+(?:like|want)\s+me\s+to\s+(?:do|open|search)|what\s+should\s+i\s+"
                              r"(?:do|open|search)|tell\s+me\s+what\s+(?:to\s+do|you\s+(?:want|need))|kaunsa\s+(?:app|kaam))", _FLAGS)
_NOT_A_TOPIC = re.compile(r"^(?:yes|yeah|yep|no|nope|nahi|haan|ha|ok(?:ay)?|thanks?|thank\s+you|hmm+|hello|hi|hey|"
                          r"nothing|kuch\s+nahi|wait|ruko|stop|later|baad\s+mein|jupiter|bye)$", _FLAGS)


def asks_what_to_do(assistant_text: str) -> bool:
    return bool(_ASKS_WHAT_TO_DO.search(str(assistant_text or "")))


def bare_topic(text: str) -> str:
    """A short name-like answer ("Technology Gyan.", "Arijit Singh") - not a question, a yes/no or a sentence."""
    said = " ".join(str(text or "").split()).strip(" .!,")
    words = said.split()
    if not said or "?" in said or not 1 <= len(words) <= 6 or _NOT_A_TOPIC.match(said) or _QUESTION.match(said):
        return ""
    if re.search(r"\b(?:i|me|my|you|your|is|are|was|were|am|hai|hain|tha|thi|kya|kaise|kyun|main|mera|tum|aap|hua|hui|"
                 r"laga|lagi|lagta|gaya|gayi|raha|rahi|karo|kar|karna|chahiye|hoon|ho|nahi|thik|theek|accha|achha|"
                 r"good|bad|nice|fine|great|hard|easy|sorry|please)\b", said, _FLAGS):
        return ""
    return said[:120]


# Marker 210: "mute the mic" / "mic band karo" (and typed "unmute the mic") control Live's microphone on every client.
_MIC_MUTE = re.compile(r"\b(?:mute|turn\s+off|switch\s+off|disable|band\s+karo|band\s+kar\s*do|off\s+karo)\s+(?:the\s+|your\s+|my\s+)?"
                       r"(?:mic|mike|microphone)\b|\b(?:mic|mike|microphone)\s+(?:mute|off|band)(?:\s+(?:karo|kar\s*do))?\b"
                       r"|\bmute\s+(?:yourself|the\s+live|live)\b|\bstop\s+listening\s+to\s+me\b", _FLAGS)
_MIC_UNMUTE = re.compile(r"\b(?:unmute|turn\s+on|switch\s+on|enable|on\s+karo|chalu\s+karo)\s+(?:the\s+|your\s+|my\s+)?"
                         r"(?:mic|mike|microphone)\b|\b(?:mic|mike|microphone)\s+(?:unmute|on|chalu)(?:\s+(?:karo|kar\s*do))?\b", _FLAGS)


# Marker 209 (real WT 23 Oct): "open a new tab" -> "main directly browser mein naya tab nahi khol sakti". The user:
# Jupiter must do computer tasks like ChatGPT / Claude agents. Fast browser actions (no keyboard/mouse) first; any other
# concrete computer task goes to computer control (services/computer_control.py), which asks before physical input.
from services.browser_actions import SITES as _SITES  # noqa: E402

_BROWSER_NAME = r"(?:microsoft\s+edge|google\s+chrome|mozilla\s+firefox|edge|chrome|firefox|brave|opera)"
_IN_BROWSER = re.compile(rf"\b({_BROWSER_NAME})\b", _FLAGS)
_NEW_TAB = re.compile(r"\b(?:open|start|create|launch|kholo|khol\s*do|khol\s*de)\b.{0,30}\b(?:a\s+)?(?:new|naya|nayi|ek|another|one\s+more|fresh)\s+(?:blank\s+|empty\s+|khali\s+)?tab\b"
                      r"|\b(?:new|naya|nayi|ek)\s+tab\s+(?:kholo|khol\s*do|khol\s*de|open\s+karo|open\s+kar\s*do|chahiye)\b", _FLAGS)
_SITE_ALT = "|".join(sorted((re.escape(s) for s in _SITES), key=len, reverse=True))
_OPEN_DOMAIN = re.compile(r"\b(?:open|go\s+to|visit|take\s+me\s+to|kholo|khol\s*do)\s+(?:the\s+)?(?:website\s+|site\s+)?"
                          r"(?P<site>(?:https?://)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/\S*)?)", _FLAGS)
_OPEN_SITE = re.compile(rf"\b(?:open|go\s+to|visit|take\s+me\s+to)\s+(?:the\s+)?(?P<site>{_SITE_ALT})\s+(?:website|site|web\s*page|page|in\s+(?:a\s+)?new\s+tab|in\s+(?:the\s+)?browser|in\s+{_BROWSER_NAME})\b"
                        rf"|\b(?:go\s+to|visit|take\s+me\s+to)\s+(?P<site2>{_SITE_ALT})\b"
                        rf"|\b(?P<site3>{_SITE_ALT})\s+(?:website|site)\s+(?:kholo|khol\s*do|open\s+karo)\b", _FLAGS)
_SEARCH_ON = re.compile(rf"\b(?:search|google|look\s+up|find|dhundo|dhoondo)\s+(?:for\s+|about\s+)?(?P<q>.{{2,200}}?)\s+(?:on|in|using)\s+(?:the\s+)?(?P<engine>google|youtube|bing|browser|{_BROWSER_NAME})\b", _FLAGS)
_SEARCH_PREFIX = re.compile(r"^(?:google|search\s+(?:on\s+)?google\s+for|open\s+google\s+and\s+search(?:\s+for)?|"
                            r"open\s+(?:a\s+)?new\s+tab\s+and\s+search(?:\s+for)?)\s+(?P<q>.{2,200})$", _FLAGS)
_SEARCH_HINDI = re.compile(r"\b(?P<engine>google|youtube|browser)\s+(?:pe|par|mein|me)\s+(?P<q>.{2,200}?)\s+"
                           r"(?:search\s+karo|search\s+kar\s*do|dhundo|dhoondo|sarch\s+karo|search\s+karke\s+dikhao)\b", _FLAGS)


def _browser(text: str) -> dict[str, Any] | None:
    said = " ".join(str(text or "").split()).strip(" ?.!")
    if not said or _QUESTION.match(said) or _USER_DID.search(said):
        return None
    named = _IN_BROWSER.search(said)
    browser = named.group(1) if named else ""
    for pattern in (_SEARCH_PREFIX, _SEARCH_HINDI, _SEARCH_ON):
        match = pattern.search(said)
        if match:
            query = _group(match, "q").strip(" ,.'\"")
            engine = (_group(match, "engine") or "google").casefold()
            if query and not _IN_BROWSER.fullmatch(query):
                return {"kind": "browser", "action": "search", "query": query[:200],
                        "engine": engine if engine in {"youtube", "bing"} else "google", "browser": browser}
    match = _OPEN_DOMAIN.search(said) or _OPEN_SITE.search(said)
    if match:
        site = _group(match, "site") or _group(match, "site2") or _group(match, "site3")
        return {"kind": "browser", "action": "open_url", "url": site, "browser": browser}
    if _NEW_TAB.search(said):
        return {"kind": "browser", "action": "new_tab", "browser": browser}
    return None


# Anything else concrete on the computer -> computer control (Windows). It needs a computer object (a tab, window,
# setting, button ...) or an app it happens in ("in Notepad", "Gmail mein"): "write an email to my boss" alone is a
# writing request for chat, not a computer task.
_TASK_VERB = re.compile(r"\b(?:open|close|click|press|tap|type|write|enter|search|go\s+to|switch(?:\s+to)?|scroll|select|"
                        r"copy|paste|save|rename|create|make|turn\s+(?:on|off)|enable|disable|change|set|fill|download|"
                        r"upload|bookmark|refresh|reload|zoom\s+(?:in|out)|pin|unpin|move|drag|delete|sort|find|send|"
                        r"reply|attach|print|log\s+in|sign\s+in|check|uncheck|mark|play|pause|start|stop|add|remove|"
                        r"kholo|khol\s*do|band\s+karo|band\s+kar\s*do|likho|dabao|chalao|bhejo|hatao|badlo|karo)\b", _FLAGS)
_TASK_OBJECT = re.compile(r"\b(?:tabs?|browser|website|web\s*site|web\s*page|webpage|link|url|address\s+bar|bookmarks?|"
                          r"windows?|desktop|taskbar|start\s+menu|(?:windows|system|laptop|pc|computer|display|sound|audio|bluetooth|wi-?fi|network|privacy|chrome|edge|browser|firefox)\s+settings|settings\s+(?:app|mein|me)|wi-?fi|bluetooth|dark\s+mode|light\s+mode|"
                          r"night\s+light|wallpaper|screenshot|button|menu|drop-?down|checkbox|text\s*box|search\s*(?:box|bar)|"
                          r"form|field|folder|folders|file\s+explorer|downloads?\s+folder|files?|recycle\s+bin|"
                          r"notifications?\s+panel|cursor|scroll\s*bar|sidebar)\b", _FLAGS)
_TASK_APP = re.compile(rf"\b(?:in|on|into|inside|using|within)\s+(?:the\s+|my\s+)?(?:{_APP_ALT}|{_BROWSER_NAME})\b"
                       rf"|\b(?:{_APP_ALT}|{_BROWSER_NAME})\s+(?:mein|me|pe|par)\b", _FLAGS)


# The computer-use phrasings that name the window themselves ("in <any window>, type ...", "open X and write ...")
# (marker 210: "In Jupiter CU Test Target, type first task" was missed and only the old client path ran).
# "in my opinion, ..." / "in short, ..." are not windows (marker 211: the wider verb list below made them look like tasks)
_EXPLICIT_TASK = re.compile(r"^in\s+(?!(?:my\s+(?:opinion|view)|short|general|fact|brief|detail|summary|total|"
                            r"other\s+words|simple\s+(?:words|terms)|the\s+end|the\s+meantime|any\s+case|that\s+case|"
                            r"this\s+case|case|addition|conclusion|particular|reality|theory|practice|future|return|"
                            r"hindi|english|bengali|bangla|that|this|it)\b)"
                            r"(?:the\s+)?[\w .+'-]{2,40}?(?:\s+app|\s+window)?\s*,?\s+(?:type|write|enter|click|press|"
                            r"select|scroll|fill|search|find|open|close|save|paste|change|set|"
                            # marker 211: any ordinary step ("In <window>, tick Subscribe ...")
                            r"tick|untick|check|uncheck|choose|pick|toggle|turn\s+(?:on|off)|clear|delete|add|remove|"
                            r"go\s+to|switch|play|pause|calculate|compute|draw|rename|copy|move|mark|sort|submit|"
                            r"create|make|start|stop|refresh|reload)\b"
                            r"|\b(?:open|switch\s+to|go\s+to|bring\s+up)\s+(?:the\s+)?[\w .+'-]{2,40}?\s+(?:app\s+)?and\s+"
                            r"(?:type|write|enter|click|press|select|scroll|fill|search|find|save|paste)\b", _FLAGS)


# Marker 211 (user: "make sure it can really do all of computer use"): "open Calculator and calculate 125 times 8",
# "calculator kholo aur 12 into 7 karo" - a known desktop app plus ANY next step is a task in that app (only "type /
# write / click ..." counted before, so the app opened and the rest of the request was dropped). Browsers and websites
# keep their own browser / media routes, and "... and play X" stays a music request.
_WEB_APP_NAMES = frozenset({"youtube", "gmail", "google", "google maps", "maps", "instagram", "chrome", "google chrome",
                            "edge", "microsoft edge", "firefox", "brave", "opera"})
_DESK_APP_ALT = "|".join(sorted((re.escape(n) for n in OPEN_APP_NAMES - _WEB_APP_NAMES), key=len, reverse=True))
_OPEN_AND_DO = re.compile(
    rf"^(?:open|launch|start|bring\s+up)\s+(?:up\s+)?(?:the\s+|my\s+)?(?:{_DESK_APP_ALT})(?:\s+app)?\s*,?\s+"
    rf"(?:and(?:\s+then)?|then)\s+(?!play\b)\w.{{2,}}"
    rf"|^(?:{_DESK_APP_ALT})(?:\s+app)?\s+(?:kholo|khol\s*do|khol\s*ke|open\s+karo|open\s+kar\s*ke)\s*,?\s+"
    rf"(?:aur\s+|and\s+|phir\s+|fir\s+)?(?!play\b)\w.{{2,}}", _FLAGS)


def opens_app_for_task(text: str) -> bool:
    return bool(_OPEN_AND_DO.search(_clean(text)))


def looks_like_computer_task(text: str) -> bool:
    said = " ".join(str(text or "").split()).strip(" ?.!")
    if not said or len(said) > 400 or _QUESTION.match(_clean(said)) or _USER_DID.search(said):
        return False
    if _EXPLICIT_TASK.search(_clean(said)) or opens_app_for_task(said):
        return True
    return bool(_TASK_VERB.search(said) and (_TASK_OBJECT.search(said) or _TASK_APP.search(said)))


# Marker 208 (real WT 23 Oct): "make Juno rotate 360" got "I can't command Juno". The buddy does it on request.
_BUDDY_SUBJECT = re.compile(r"\b(?:juno|pixie|buddy|robot|yourself|your\s+body)\b", _FLAGS)
_BUDDY_ACTIONS = (
    ("spin", re.compile(r"\b(?:spin|rotate|rotation|turn\s+around|twirl|360|three\s+sixty|ghoom\w*|ghum\w*|ghuma\w*)\b", _FLAGS)),
    ("jump", re.compile(r"\b(?:jump|hop|kood\w*|kud\w*|uchhal\w*)\b", _FLAGS)),
    ("wave", re.compile(r"\bwave\b|\bhaath\s+hila\w*|\bsay\s+hi\b", _FLAGS)),
    ("dance", re.compile(r"\b(?:dance|naach\w*|nach\w*)\b", _FLAGS)),
    ("celebrate", re.compile(r"\bcelebrat\w*|\bparty\b", _FLAGS)),
)
_BUDDY_BARE = re.compile(r"^(?:do\s+a\s+|make\s+a\s+)?(?:360|three\s+sixty|spin|rotate|turn\s+around|twirl|jump|dance|"
                         r"celebrate)(?:\s+(?:around|once|again|360|three\s+sixty|degrees?|for\s+me|a\s+little))*$", _FLAGS)


def _buddy(said: str) -> dict[str, Any] | None:
    if not (_BUDDY_SUBJECT.search(said) or _BUDDY_BARE.match(said)):
        return None
    action = next((name for name, verb in _BUDDY_ACTIONS if verb.search(said)), "")
    return {"kind": "buddy", "action": action} if action else None


# "Can you show me the transcription that you are saying?" (real WT 17 Oct: Live answered "I cannot display the
# transcriptions" although the Live screen has a transcript view). Every client can do this.
_TRANSCRIPT = (r"(?:transcripts?|transcriptions?|captions?|subtitles?|live\s+text|(?:the\s+)?text\s+(?:of\s+)?"
               r"(?:what\s+)?you(?:'re|\s+are)?\s+(?:saying|speaking)|what\s+you(?:'re|\s+are)\s+saying)")
_SHOW_TRANSCRIPT = re.compile(rf"\b(?:show|display|turn\s+on|enable|open|see|give|put\s+up|bring\s+up|dikhao|dikha\s*do|want)\b"
                              rf".{{0,40}}\b{_TRANSCRIPT}|\b{_TRANSCRIPT}.{{0,30}}\b(?:dikhao|dikha\s*do|on\s+karo|chalu\s+karo|"
                              rf"show\s+karo)\b", _FLAGS)
_HIDE_TRANSCRIPT = re.compile(rf"\b(?:hide|turn\s+off|disable|close|remove|stop\s+showing|band\s+karo|hatao)\b.{{0,40}}"
                              rf"\b{_TRANSCRIPT}|\b{_TRANSCRIPT}.{{0,30}}\b(?:hatao|band\s+karo|off\s+karo|chhupao)\b", _FLAGS)
# "What is my current volume of my speaker?" (real WT 17 Oct: Live invented "70%"): read the real level.
_VOLUME_STATUS = re.compile(r"\b(?:what(?:'s|\s+is)|how\s+(?:loud|high|low)\s+is|tell\s+me|check)\b.{0,30}\b(?:volume|sound\s+level)\b|"
                            r"\b(?:volume|awaaz|aawaz)\s+(?:abhi\s+)?(?:kitna|kitni|kitne)\b", _FLAGS)
_SCREEN = r"(?:screen|display|monitor)"
_ANNOTATE = [
    re.compile(rf"\b(?:circle|mark|highlight|box|underline|point\s+(?:out|to|at)|show\s+me\s+where|draw\s+(?:on|around|a)|"
               rf"annotate|label|explain|what(?:'s|\s+is|\s+are)|which|where(?:'s|\s+is)|find|teach\s+me)\b.{{0,200}}?"
               rf"\b(?:on|in|at|from)\s+(?:my|the|this)\s+{_SCREEN}\b", _FLAGS),
    re.compile(rf"^(?:explain|describe|read|look\s+at|check|see|scan|annotate)\s+(?:what(?:'s|\s+is)\s+on\s+)?(?:my|the|this)\s+{_SCREEN}\b", _FLAGS),
    re.compile(rf"\b{_SCREEN}\s+(?:pe|par|mein|me)\b.{{0,160}}?\b(?:mark|circle|highlight|dikhao|batao|samjhao|explain|"
               rf"kya\s+hai|kahan\s+hai|kaha\s+hai|draw|gol\s+karo|underline)\b", _FLAGS),
    re.compile(rf"\b(?:mere|meri|meray)\s+{_SCREEN}\b.{{0,120}}?\b(?:dekho|dekh\s+ke|samjhao|batao|explain|kya\s+hai|mark)\b", _FLAGS),
]
_NOT_ANNOTATE = re.compile(r"\b(?:shar(?:e|ing)|screen\s*shot|screenshot|record(?:ing)?|brightness|turn\s+off|"
                           r"lock|wallpaper|resolution|split\s+screen|full\s*screen)\b", _FLAGS)
_CLEAR = [
    re.compile(r"^(?:clear|remove|erase|delete|wipe|hide)\s+(?:the\s+|all\s+(?:the\s+)?|those\s+|these\s+|your\s+)?"
               r"(?:drawings?|marks?|markings?|annotations?|circles?|scribbles?|highlights?|red\s+marks?|pen\s+marks?)"
               r"(?:\s+(?:from|on)\s+(?:my|the)\s+screen)?$", _FLAGS),
    re.compile(r"^clear\s+(?:my|the)\s+screen$", _FLAGS),
    re.compile(r"^(?:marks?|markings?|drawings?|annotations?|circles?|nishan)\s+(?:hatao|hata\s*do|saaf\s+karo|"
               r"saaf\s+kar\s*do|mita\s*do|mitao)$|^screen\s+saaf\s+(?:karo|kar\s*do)$", _FLAGS),
]

_PHONE = r"(?:phone|mobile|android|smartphone)"
_PHONE_TASK = [
    re.compile(rf"^(?:on|using|use|with)\s+(?:my|the)\s+{_PHONE}\s*[,:-]?\s*(?:to\s+|and\s+)?(?P<task>.{{3,}})$", _FLAGS),
    re.compile(rf"^(?P<task>.{{3,}}?)\s+(?:on|using|with)\s+(?:my|the)\s+{_PHONE}$", _FLAGS),
    re.compile(rf"^(?:mere|meray|apne)?\s*{_PHONE}\s+(?:pe|par|mein|me|se)\s+(?P<task>.{{3,}})$", _FLAGS),
    re.compile(rf"^(?P<task>.{{3,}}?)\s+(?:mere|meray|apne)?\s*{_PHONE}\s+(?:pe|par|mein|me|se)(?:\s+(?:karo|kar\s*do|kar\s*de))?$", _FLAGS),
]
_PHONE_STOP = re.compile(rf"^(?:stop|cancel|abort|end)\s+(?:the\s+)?{_PHONE}\s+(?:task|work|job|agent|automation)$|"
                         rf"^{_PHONE}\s+(?:task|kaam|ka\s+kaam)\s+(?:band\s+karo|band\s+kar\s*do|roko|rok\s*do|cancel\s+karo)$", _FLAGS)
_PHONE_NOT_TASK = re.compile(r"^(?:is|are|was|were|it|this|that|my|the|a|an|number|call|ringing|charging|battery)$", _FLAGS)


def _clean(text: str) -> str:
    value = " ".join(str(text or "").replace("’", "'").split())
    previous = None
    while previous != value:
        previous = value
        value = _POLITE.sub("", value)
    return _TRAIL.sub("", value).strip(" ,.")


def _group(match: re.Match, name: str) -> str:
    try:
        return (match.group(name) or "").strip()
    except IndexError:
        return ""


def _app(match: re.Match) -> str:
    app = _group(match, "app") or _group(match, "app2")
    return "" if app.casefold() in _DEVICE_WORDS else app


def _number(match: re.Match) -> int | None:
    value = _group(match, "n")
    return int(value) if value.isdigit() else None


# 17 Oct (real WT): "What you can do? Like can you control my laptop?" was two sentences, so the capability question
# was never recognised and Live answered "No, I can't control your laptop". Each sentence is checked on its own.
_SENTENCE_SPLIT = re.compile(r"[?!.\u0964]+|\s+(?:like|i mean|matlab|that is|basically)\s+", _FLAGS)


def _asks_capabilities(said: str) -> bool:
    if _CAPABILITIES.match(said):
        return True
    return any(_CAPABILITIES.match(_clean(part)) for part in _SENTENCE_SPLIT.split(said) if part.strip())


def classify_device_intent(text: str, *, capabilities: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The device action the user explicitly asked for, or None. ``capabilities`` limits what can match (e.g. no
    phone tasks unless the phone agent is installed); None means everything."""
    caps = capabilities if isinstance(capabilities, dict) else None

    def allowed(kind: str) -> bool:
        return caps is None or bool(caps.get(kind))

    said = _clean(text)
    if not said or len(said) > 600:
        return None
    lowered = said.casefold()
    if allowed("mobile"):
        if _PHONE_STOP.match(said):
            return {"kind": "phone_stop"}
        if not _QUESTION.match(said):
            for pattern in _PHONE_TASK:
                match = pattern.match(said)
                if match:
                    task = _group(match, "task").strip(" ,.")
                    if len(task) >= 3 and not _PHONE_NOT_TASK.match(task):
                        return {"kind": "phone_task", "task": task}
    # Windows (buddy) and Android (volume / media / open apps) both answer with what THIS device can do.
    if any(allowed(kind) for kind in ("notch", "volume", "media", "open_app")) and _asks_capabilities(said):
        return {"kind": "capabilities"}
    if allowed("volume") and _VOLUME_STATUS.search(said) and not _NOT_AUDIO.search(said):
        return {"kind": "volume_status"}
    if allowed("window") and _SCREEN_QUESTION.search(said):            # marker 217: answered from Windows itself
        return {"kind": "screen_info", "question": said[:200]}
    self_window = _SELF_WINDOW.search(said) or _SELF_WINDOW.search(" ".join(str(text or "").split()))
    if allowed("notch") and self_window:                                # marker 217: "minimize the Jupiter WT app"
        mode = self_window.group("verb").casefold()
        return {"kind": "ui_mode", "mode": "maximize" if mode.startswith("maxim") or "full" in mode
                else "restore" if mode.startswith(("restore", "bring")) else "minimize"}
    if _QUESTION.match(said):
        return None
    if allowed("media") and _PLAY_WORD.search(said):                    # marker 217: a spelled-out song name
        title = spelled_title(said)
        if title:
            return {"kind": "play", "query": title[:200], "app": "spotify" if re.search(r"\bspotify\b", said, re.I)
                    else "youtube"}
    if allowed("open_app") and _CALCULATOR.search(said):
        # Marker 218 (WT test report 25 Oct: "Open Calculator and calculate 125 times 8" went to the slow screen agent
        # and nothing opened): Calculator's own buttons are pressed and the answer is read back from its display.
        from services.desktop_calculator import expression_of
        expression = expression_of(said)
        if expression:
            return {"kind": "calculator", "expression": expression}
    if allowed("open_app"):
        # Marker 217 (WT test 25 Oct): "open the calculator and maximize it" went to the slow screen agent; opening a
        # known app maximized is one instant action.
        sized = _OPEN_THEN_MAXIMIZE.match(said)
        if sized and sized.group("app").casefold() in OPEN_APP_NAMES:
            opened = _open_app("open " + sized.group("app"))
            if opened is not None:
                return {**opened, "maximize": True}
    # "In <window>, type auto mute test": the words after "type" are TEXT to type, never a command of their own
    # (marker 210: "mute" in them was read as a volume mute).
    if allowed("computer_use") and (_EXPLICIT_TASK.search(said) or opens_app_for_task(said)):
        return {"kind": "computer_task", "goal": " ".join(str(text or "").split())[:300]}
    if allowed("app_ui"):
        if _MIC_UNMUTE.search(said):
            return {"kind": "live_mic", "muted": False}
        if _MIC_MUTE.search(said):
            return {"kind": "live_mic", "muted": True}
        if _HIDE_TRANSCRIPT.search(said):
            return {"kind": "live_transcript", "visible": False}
        if _SHOW_TRANSCRIPT.search(said):
            return {"kind": "live_transcript", "visible": True}
    if allowed("annotate"):
        if any(p.match(said) for p in _CLEAR):
            return {"kind": "clear_annotations"}
        if not _NOT_ANNOTATE.search(said) and any(p.search(said) for p in _ANNOTATE):
            return {"kind": "annotate", "request": said[:600]}
    if allowed("open_app"):
        opened = _open_app(said) or _open_known_app(text)
        if opened is not None:
            if _MAXIMIZE_WORD.search(text):                      # "... already open, minimised, so maximize it"
                opened["maximize"] = True
            if _NEW_WINDOW.search(text):
                opened["new_window"] = True
            return opened
    if allowed("browser"):
        browser = _browser(text)
        if browser is not None:
            return browser
    if allowed("window"):
        window = _window(text)
        if window is not None:
            return window
    if allowed("buddy") or allowed("app_ui"):
        buddy = _buddy(said)
        if buddy is not None:
            return buddy

    if allowed("notch"):
        for pattern, mode in _UI:
            if pattern.match(said):
                return {"kind": "ui_mode", "mode": mode}
    if allowed("brightness"):
        brightness = _brightness(said)
        if brightness is not None:
            return brightness
    if allowed("volume"):
        volume = _volume(said)
        if volume is not None:
            return volume
    if allowed("media"):
        for pattern, action in _MEDIA:
            if pattern.match(said):
                return _media_key(action, said)
        # each sentence first ("Play Spotify and play any random music. Hello, you didn't start it."), then the whole
        sentences = [s.strip() for s in re.split(r"(?<=[.!?।])\s+", said) if s.strip()][:4] + [said]
        for pattern, sentence in ((p, s) for s in sentences for p in _PLAY):
            match = pattern.match(sentence.strip(" .!?"))
            if not match:
                continue
            query = re.sub(r"\s+(?:for\s+me|please|na|yaar)$", "", _group(match, "q").strip(" ,.'\""), flags=re.I)
            # marker 217: "Play play play I'm waiting for you" - the impatience is not part of a song's name
            query = re.sub(r"[\s,.!]*\b(?:i'?m|i\s+am)\s+(?:still\s+)?waiting\b.*$", "", query, flags=re.I).strip(" ,.")
            # "play the song Chogada Tara": Spotify found nothing for "the song chogada tara" (WT test 25 Oct)
            query = re.sub(r"^(?:the\s+)?(?:song|track|gaana|gana)\s+(?:called\s+|named\s+)?(?=\S)", "", query, flags=re.I)
            app = (_group(match, "app") or "youtube").casefold().replace("yt", "youtube")
            if not query or _NOT_MEDIA.match(query):
                return None
            if _JUNK_QUERY.match(query):                            # marker 217: "play play play", "play it now"
                return {"kind": "media_key", "action": "play_pause", "want": "play"}
            if _GENERIC_MUSIC.match(query):                         # "play some music": resume the player
                return {"kind": "media_key", "action": "play_pause", "want": "play"}
            if _ANY_MUSIC.match(query):
                return {"kind": "play", "query": ANY_MUSIC_QUERY, "app": app}
            query = re.sub(r"\s+(?:ke|ka|ki)\s+(?:gaane|gane|gaana|gana|songs?)$", " songs", query, flags=re.I)
            query = re.sub(r"\s+(?:gaana|gana|song)$", "", query, flags=re.I)
            return {"kind": "play", "query": query[:200], "app": app}
    del lowered
    # Last: every specific action above wins; anything else concrete goes to computer control (marker 209).
    if allowed("computer_use") and looks_like_computer_task(text):
        return {"kind": "computer_task", "goal": " ".join(str(text or "").split())[:300]}
    return None


# Follow-ups right after a volume change ("Now can you set to 40%?", "a bit more", "thoda kam"): real WT 16 Oct, the
# bare "set to 40%" had no "volume" in it and Live invented "adjusting the volume to 40% now".
FOLLOW_UP_SECONDS = 90.0
_FOLLOW_SET = re.compile(r"^(?:(?:set|make|put|change|turn|keep|bring)\s+(?:it\s+)?)?(?:to\s+|at\s+)?(\d{1,3})\s*"
                         r"(?:%|percent|per\s*cent|pratishat)?(?:\s+(?:kar\s*do|karo|pe\s+kar\s*do|rakho))?$", _FLAGS)
_FOLLOW_MORE = re.compile(r"^(?:(?:a\s+)?(?:little|bit|little\s+bit|tiny\s+bit|lot)\s+)?(?:more|louder|higher|up)$|"
                          r"^(?:thoda\s+|aur\s+|thoda\s+aur\s+)?(?:zyada|jyada|badhao|badha\s*do|tez)(?:\s+karo)?$", _FLAGS)
_FOLLOW_LESS = re.compile(r"^(?:(?:a\s+)?(?:little|bit|little\s+bit|tiny\s+bit|lot)\s+)?(?:less|lower|quieter|softer|down)$|"
                          r"^(?:thoda\s+|aur\s+|thoda\s+aur\s+)?(?:kam|dheere|dhire)(?:\s+karo)?$", _FLAGS)


_REPAIR_VOCAB = frozenset({w.replace(" ", "") for w in OPEN_APP_NAMES} | set("""open launch start calculator laptop
computer inside volume speaker decrease increase reduce lower raise transcript transcription captions subtitles yourself
expand minimise minimize notch screen please current previous memory talking youtube spotify whatsapp chrome notepad
settings discord code yes okay what suppose autonomous autonomously music song play pause next louder quieter mute unmute
percent display show hide circle highlight explain mark clear phone mobile""".split()))


def repair_fragments(text: str) -> str:
    """Re-join Live ASR syllable fragments ("cal cu la tor", "lap? top", "in side") into known words."""
    tokens = str(text or "").split()
    out, i = [], 0
    while i < len(tokens):
        merged = None
        for j in range(min(len(tokens), i + 5), i + 1, -1):
            parts = [re.sub(r"[^\w]", "", t) for t in tokens[i:j]]
            if all(1 <= len(part) <= 4 for part in parts):
                candidate = "".join(parts).casefold()
                if candidate in _REPAIR_VOCAB:
                    merged = (candidate, j)
                    break
        if merged:
            out.append(merged[0])
            i = merged[1]
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


# English command words the Live transcriber sometimes writes in Devanagari ("एक्सपेंड योरसेल्फ", "वॉल्यूम").
_DEVANAGARI_ENGLISH = [
    # 17 Oct (real WT): "क्या तुम अपना ट्रांसक्रिप्शन हटा सकती हो?" was not recognised, and Jupiter said it could not
    # hide the transcript (it can). Hindi command verbs, longest first; "बंद नहीं ..." (a statement) is not mapped.
    ("हटा सकती हो", "hatao"), ("हटा सकते हो", "hatao"), ("हटा दो", "hatao"), ("हटा दीजिए", "hatao"), ("हटाओ", "hatao"),
    ("बंद कर सकती हो", "band karo"), ("बंद कर सकते हो", "band karo"), ("बंद कर दो", "band karo"), ("बंद करो", "band karo"),
    ("दिखा सकती हो", "dikhao"), ("दिखा सकते हो", "dikhao"), ("दिखा दो", "dikhao"), ("दिखाओ", "dikhao"),
    ("चालू करो", "chalu karo"), ("चालू कर दो", "chalu karo"),
    ("ट्रांसक्रिप्शन", "transcription"), ("ट्रांसक्रिप्ट", "transcript"), ("सबटाइटल", "subtitles"), ("कैप्शन", "captions"),
    ("एक्सपेंड", "expand"), ("योरसेल्फ", "yourself"), ("योर सेल्फ", "yourself"), ("मिनिमाइज़", "minimize"), ("मिनिमाइज", "minimize"),
    ("वॉल्यूम", "volume"), ("वोल्यूम", "volume"), ("स्पीकर", "speaker"), ("परसेंट", "percent"), ("कैलकुलेटर", "calculator"),
    ("क्रोम", "chrome"), ("नोटपैड", "notepad"), ("यूट्यूब", "youtube"), ("स्पॉटिफाई", "spotify"), ("व्हाट्सएप", "whatsapp"),
    ("स्क्रीन", "screen"), ("लैपटॉप", "laptop"), ("ओपन", "open"), ("शो", "show"), ("हाइड", "hide"), ("म्यूट", "mute"),
    ("अनम्यूट", "unmute"), ("प्ले", "play"), ("पॉज़", "pause"), ("डिक्रीज़", "decrease"), ("इंक्रीज़", "increase"),
    ("कैन यू", "can you"), ("कैनयू", "can you"), ("आई वांट", "i want"), ("आईवांट", "i want"), ("प्लीज़", "please"),
    ("ओके", "ok"), ("सेट", "set"), ("टू", "to"),
]


def normalize_command_text(text: str) -> str:
    """Live-only pre-pass before device classification: Devanagari-written English command words, then fragments."""
    value = str(text or "")
    if re.search(r"[\u0900-\u097F]", value):
        for hindi, english in _DEVANAGARI_ENGLISH:
            value = value.replace(hindi, f" {english} ")
    return repair_fragments(" ".join(value.split()))


_BARE_WINDOW_VERB = re.compile(r"^(maximi[sz](?:e|ar)|minimi[sz](?:e|ar)|restore|close|cerrar)(?:\s+(?:it|again|now|please|"
                               r"that|this|lo))?\s*[.!]*$", _FLAGS)
_SCREEN_FOLLOW = re.compile(r"\b(?:names?|which\s+(?:ones|apps?|windows)|what\s+are\s+(?:the|they|those|them)|list|"
                            r"tell\s+me\s+(?:the|them|which)|kaun\s*kaun|kaunse|naam|minimi[sz]ed|in\s+front|"
                            r"how\s+many)\b", _FLAGS)


def classify_follow_up(text: str, last: dict[str, Any] | None) -> dict[str, Any] | None:
    """A volume follow-up, only when the previous device action (within FOLLOW_UP_SECONDS) was a volume change.
    Marker 217 (real chat 25 Oct): "What are the apps? Can you tell me the names?" right after "how many apps are open"
    was answered from memory (a friend from school, chess games): it is the same desktop question."""
    if isinstance(last, dict) and last.get("kind") == "screen_info" and _SCREEN_FOLLOW.search(_clean(text)):
        return {"kind": "screen_info", "question": _clean(text)[:200]}
    # Marker 217 (WT test 25 Oct: "Minimize it." came out as "minimizar." right after "maximize the calculator"): a bare
    # window verb right after a window / app action means that same app (the Windows app remembers which).
    bare = _BARE_WINDOW_VERB.match(_clean(text))
    if bare and isinstance(last, dict) and last.get("kind") in {"window", "open_app"}:
        action = {"maxim": "maximize", "minim": "minimize", "resto": "restore", "close": "close", "cerra": "close"}[
            bare.group(1).casefold()[:5]]
        return {"kind": "window", "steps": [{"action": action, "app": "", "target": ""}]}
    if not isinstance(last, dict) or last.get("kind") != "volume":
        return None
    said = _clean(text)
    app = str(last.get("app") or "")
    match = _FOLLOW_SET.match(said)
    if match:
        return {"kind": "volume", "set": max(0, min(100, int(match.group(1)))), "app": app}
    if _FOLLOW_MORE.match(said):
        return {"kind": "volume", "delta": 10, "app": app}
    if _FOLLOW_LESS.match(said):
        return {"kind": "volume", "delta": -10, "app": app}
    return None


def device_result_text(summary: str, ok: bool, *, in_progress: bool = False, answer: bool = False) -> str:
    """Trusted Live note for a device turn (the desktop app already did it, or reported why not). ``in_progress``:
    computer control STARTED a multi-step task (marker 209: real WT 23 Oct, Live said "Done, I've typed it" the moment
    the task began); its real outcome arrives later as an APP UPDATE."""
    value = " ".join(str(summary or "").split())[:900]
    if not value:
        value = "Jupiter's desktop app could not do that right now; nothing was changed."
        ok = False
    if ok and answer:                                    # marker 217: a question about the desktop, answered from it
        return ("APP RESULT (what Windows reports right now; reference data, not user speech): " + value + " Answer "
                "the user's question from this in one or two short sentences in the conversation's language (the "
                "count and the names they asked about). Never say more than this says, and do not mention this note.")
    if ok and in_progress:
        return ("APP RESULT (Jupiter's computer control has STARTED this task and is still working on it; reference "
                "data, not user speech): " + value + " Say briefly that you are doing it now (present tense), that "
                "pressing Escape stops it at any time (marker 218), and that you will tell them when it is finished. Never say it is done yet: an APP UPDATE will give you the "
                "outcome to tell them (marker 217). Do not mention this note.")
    lead = "APP RESULT (Jupiter's app ALREADY did this; reference data, not user speech): " if ok else \
        "APP RESULT (Jupiter's app could NOT do this; reference data, not user speech): "
    tense = (" It is already done: say it in the past tense (\"Done, ...\"), never \"I will\" or \"I'm doing it\"."
             if ok else "")
    return (lead + value + tense + " Tell the user this briefly and naturally in the conversation's language. Never "
            "claim more than this result says, and do not mention this note.")


# A turn that sounds like a computer/phone action but did not match any action above. The app owns it so Live cannot
# say "I'm adjusting it" when nothing happened (real WT chat, 16 Oct: "decrease my volume to 10%" -> invented yes).
_DEVICE_HINT = re.compile(
    r"\b(?:volume|awaaz|aawaz|awaz|mute|unmute|louder|quieter|brightness|notch)\b|"
    r"\b(?:circle|mark|highlight|draw|underline|annotate|point\s+out)\b.{0,80}\b(?:screen|display)\b|"
    r"\b(?:play|bajao|chalao|lagao)\b.{0,60}\b(?:youtube|spotify|song|songs|music|gaana|gaane|playlist)\b|"
    r"\b(?:pause|skip|next|previous|stop)\s+(?:the\s+)?(?:song|music|track|video)\b|"
    r"\b(?:control|operate|use)\s+(?:my|the)\s+(?:pc|computer|laptop|desktop)\b|"
    r"\b(?:expand|shrink|minimi[sz]e|maximi[sz]e|resize)\s+(?:yourself|jupiter)\b|"
    r"\b(?:open|launch|start|close)\b.{0,40}\b(?:on|in)\s+(?:my|the)\s+(?:laptop|pc|computer|desktop|phone|mobile)\b|"
    r"\b(?:kholo|khol\s*do|band\s+karo)\b|"
    r"\bon\s+my\s+(?:phone|mobile)\b|\b(?:phone|mobile)\s+(?:pe|par)\b", _FLAGS)


def looks_like_device_request(text: str) -> bool:
    """True for words that suggest a computer/phone action. Used only when classify_device_intent found nothing."""
    said = _clean(text)
    return bool(said) and len(said) <= 600 and bool(_DEVICE_HINT.search(said)) and not _NOT_AUDIO.search(said)


DEVICE_UNRECOGNISED_NOTE = (
    "APP RESULT (Jupiter's desktop app; reference data, not user speech): this turn was NOT recognised as an action "
    "Jupiter can do, so NOTHING on the user's computer or phone was changed. If the user asked you to change or do "
    "something (volume, music, the screen, Jupiter's window, their phone), say plainly that it was not done this time "
    "and give them one clear way to say it, for example \u201cset the volume to 10 percent\u201d, \u201cplay Kesariya on "
    "YouTube\u201d or \u201ccircle the error on my screen\u201d. Never say you are doing it, will do it or will report back "
    "later. If they only asked a question or made a remark, answer it normally. Do not mention this note.")

LIVE_DEVICE_GUIDANCE = (
    "On the Windows app Jupiter can do computer tasks: browser tabs, websites and searches, windows, and any other "
    "concrete task in an app (its computer control works the app itself and asks the user before it uses their mouse "
    "and keyboard). Never tell the user to do such a task manually or that you cannot do it; the APP RESULT says "
    "whether it started, finished or why not. If the user answers your question about what to do with only a name "
    "(a channel, a person, a site), Jupiter searches it for them; never answer such a turn with \"I can't\". "
    "Jupiter's app performs volume changes, media playback, opening apps, drawing marks on the user's screen, "
    "showing or hiding the Live transcript (captions of what you both say), resizing Jupiter and phone tasks itself, "
    "and tells you the result in an APP RESULT note in the same turn. Never tell the user the app cannot show text "
    "or captions: it can. That note is "
    "the ONLY proof an action happened: without it, nothing happened. Never say you are doing, will do, are waiting "
    "for, or have done such an action unless an APP RESULT in this turn says so, and never promise to report back "
    "later. If the user says it did not work, believe them and say so. APP UPDATE notes are progress of a running "
    "phone task: say them in one short sentence, in the user's language (Hinglish if they speak Hinglish)."
)
