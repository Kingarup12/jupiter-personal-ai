"""First-class personal-assistant intents: explicit memory commands and reminders.

Decided from the *structure* of a request, before capability detection, never from a bare keyword:
"How does computer memory work?" is chat, "Do you remember how RAM works?" is chat, "Remember that Mira is my
project partner." is a memory write, "Remind me tomorrow at 8 AM to submit my assignment." is a reminder.

Regression (production, 30-Sep-2026 17:32 IST, dedicated test account): "I want you to remember the following
context about a friend and use it naturally whenever I talk about her in the future..." was answered by the image
editor ("Attach an image or choose an image ...") because the word "change" appeared in the pasted context.
Personal intents now run first and own explicit memory/reminder operations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MEMORY_WRITE = "MEMORY_WRITE"
MEMORY_READ = "MEMORY_READ"
MEMORY_UPDATE = "MEMORY_UPDATE"
MEMORY_FORGET = "MEMORY_FORGET"
MEMORY_SHARE = "MEMORY_SHARE"
MEMORY_UNSHARE = "MEMORY_UNSHARE"
REMINDER_CREATE = "REMINDER_CREATE"
REMINDER_LIST = "REMINDER_LIST"
REMINDER_UPDATE = "REMINDER_UPDATE"
REMINDER_CANCEL = "REMINDER_CANCEL"
REMINDER_SNOOZE = "REMINDER_SNOOZE"
TASK_NOTIFY = "TASK_NOTIFY"                 # marker 159: "WhatsApp me when done", "tell me if it fails"
RELAY_MESSAGE = "RELAY_MESSAGE"             # marker 182: "tell him …", "usko bata do", "msg him" — another person
ACCOUNT_SELF = "ACCOUNT_SELF"               # marker 184: "which email am I signed in with?" — own account only
CONTACT_REQUEST = "CONTACT_REQUEST"         # marker 185: "what is the maintainer email?" — consent request to that account
CALL_USER = "CALL_USER"                     # marker 186: "call the maintainer" — another person; Jupiter can't place it
NONE = "NONE"

MEMORY_INTENTS = frozenset({MEMORY_WRITE, MEMORY_READ, MEMORY_UPDATE, MEMORY_FORGET, MEMORY_SHARE, MEMORY_UNSHARE})
REMINDER_INTENTS = frozenset({REMINDER_CREATE, REMINDER_LIST, REMINDER_UPDATE, REMINDER_CANCEL, REMINDER_SNOOZE})

SELF = "me"
LAST = "last"


@dataclass(frozen=True)
class PersonalIntent:
    kind: str = NONE
    subject: str = ""          # a person's name, SELF, or "" when not about a person
    content: str = ""          # the fact / new content / reminder request text
    target: str = ""           # share/unshare recipient as the user named them
    attribution: bool | None = None
    reference: str = ""        # LAST for "that / this / it"
    remainder: str = ""        # a second, non-personal request in the same message ("... and crop this photo")

    @property
    def is_personal(self) -> bool:
        return self.kind != NONE


# ── shared pieces ───────────────────────────────────────────────────────────────────────────────────────────────
_ADDRESS = re.compile(r"(?i)^\W*(?:(?:hey|hi|ok|okay|yo)\s+)?jupiter\b[\s,.:!-]*")
_POLITE = re.compile(
    r"(?i)^(?:please|pls|plz|kindly|just|also|and|so|now|actually|from\s+now\s+on|going\s+forward|btw|by\s+the\s+way|"
    r"(?:can|could|would|will)\s+you(?:\s+please)?|i\s+(?:want|need|would\s+like|wanted)\s+you\s+to|"
    r"i'?d\s+like\s+you\s+to|i\s+would\s+love\s+(?:it\s+)?if\s+you(?:\s+could)?|i\s+want\s+to\s+make\s+sure\s+you)"
    r"[\s,.:!-]+"
)
_STOP_NAMES = frozenset({
    "i", "i'm", "im", "me", "my", "mine", "myself", "the", "this", "that", "these", "those", "it", "its", "he",
    "she", "they", "them", "we", "us", "you", "your", "yours", "our", "his", "her", "their", "a", "an", "and",
    "jupiter", "please", "today", "tomorrow", "tonight", "yesterday", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december", "everyone", "everything", "something", "someone",
    "anything", "nothing", "all", "what", "who", "when", "where", "why", "how", "which", "also", "actually", "if",
    "so", "but", "or", "not", "no", "yes", "okay", "ok", "hey", "hi", "hello", "dear", "mom", "dad", "mum",
    "god", "here", "there", "now", "then", "about", "following", "context", "information",
})
# A name token: capitalised Latin word, or any word written in a script without case (Devanagari, Bengali, ...).
# A capitalised word, an initial ("J."), or a word in a caseless script. A period ends a name ("…about Asha. Asha
# is my…" is one name followed by a new sentence, never "Asha Asha").
_NAME_TOKEN = r"(?:[A-Z]\.(?=\s)|[A-Z][\w'’-]*|[^\W\d_A-Za-z][\w'’-]*)"
_NAME = rf"({_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,2}})"


def normalize(text: str) -> str:
    return " ".join(str(text or "").split())


def strip_request_prefix(text: str) -> str:
    """Remove "Hey Jupiter," / "please" / "can you" / "I want you to" style lead-ins (repeatedly)."""
    value = _ADDRESS.sub("", normalize(text))
    for _ in range(6):
        stripped = _POLITE.sub("", value, count=1)
        if stripped == value:
            break
        value = stripped
    return value


def clean_name(candidate: str) -> str:
    """A plausible person name, or "" (drops pronouns, weekdays, 'Jupiter', trailing punctuation)."""
    tokens = [token.strip(".,:;!?\"'’()[]") for token in normalize(candidate).split()]
    tokens = [token for token in tokens if token]
    while tokens and tokens[-1].casefold() in _STOP_NAMES:
        tokens.pop()
    if not tokens or tokens[0].casefold() in _STOP_NAMES:
        return ""
    if any(token.casefold() in {"i", "my", "me"} for token in tokens):
        return ""
    name = " ".join(tokens[:3])
    return name if 1 < len(name) <= 60 else ""


def _possessive_base(token: str) -> str:
    return re.sub(r"['’]s$", "", token)


# ── memory patterns ─────────────────────────────────────────────────────────────────────────────────────────────
_WRITE_VERB = (
    r"(?:remember|memori[sz]e|keep\s+in\s+mind|make\s+(?:a\s+)?note(?:\s+of)?|take\s+(?:a\s+)?note(?:\s+of)?|"
    r"note(?:\s+down)?|save|store|don'?t\s+forget|do\s+not\s+forget|never\s+forget|learn)"
)
_WRITE = re.compile(
    rf"(?i)^{_WRITE_VERB}\s*(?::\s*|,\s*|\s+(?:that|this|these|the\s+following|following|something|about|"
    r"my|i\b|i'm|i've|me\b|our|we\b|(?=[A-Z])|(?=[^\W\d_A-Za-z])))"
)
_WRITE_TRAILING = re.compile(
    r"(?is)^(?P<body>.{6,}?)[\s,.;:!-]+(?:(?:please|pls|jupiter)[\s,]+)?(?:(?:can|could|will)\s+you\s+)?"
    r"(?:remember|memori[sz]e|note|save|store|keep\s+in\s+mind|don'?t\s+forget)\s+(?:that|this|it)"
    r"(?:\s+(?:please|pls|for\s+me|for\s+later))?\s*[.!]*$"
)
_TRAILING_NOT_A_FACT = re.compile(
    r"(?i)^\W*(?:what|who|why|how|when|where|which|can|could|would|will|do|does|did|is|are|please)\b|\?\s*$"
)
_NOT_A_MEMORY = re.compile(
    r"(?i)^(?:remember|recall)\s+(?:when|the\s+time|how\s+we|that\s+time)\b"          # reminiscing
    r"|^(?:save|store)\s+(?:this|that|the|my)?\s*(?:image|photo|picture|pic|file|pdf|chat|conversation|audio|"
    r"recording|video|document|draft|code)\b"
    r"|^(?:don'?t|do\s+not|never)\s+forget\s+to\s+remind\b"
)
_READ = [
    re.compile(r"(?i)^(?:do|did|can)\s+you\s+(?:still\s+)?(?:remember|know|recall)\s+(?:anything\s+about\s+|about\s+)?"
               r"(?P<subject>.+?)\s*\??$"),
    re.compile(r"(?i)^what\s+(?:do|did|else\s+do)\s+you\s+(?:remember|know|recall|have\s+saved)\s+about\s+"
               r"(?P<subject>.+?)\s*\??$"),
    re.compile(r"(?i)^what\s+(?:have|did)\s+i\s+(?:told|tell)\s+you\s+about\s+(?P<subject>.+?)\s*\??$"),
    re.compile(r"(?i)^(?:tell\s+me\s+)?what\s+you\s+(?:remember|know)\s+about\s+(?P<subject>.+?)\s*[.?!]*$"),
    re.compile(r"(?i)^what\s+(?:do|did)\s+you\s+(?:remember|know)(?P<subject>)\s*\??$"),
    re.compile(r"(?i)^who\s+is\s+(?P<subject>.+?)\s+to\s+me\s*\??$"),
    re.compile(r"(?i)^what\s+(?:is|'s)\s+(?:in\s+)?your\s+memory\s+(?:of|about)\s+(?P<subject>.+?)\s*\??$"),
]
_FORGET = re.compile(
    r"(?i)^(?:forget|delete|erase|remove|drop|wipe)\s+"
    r"(?:(?P<last>that|this|it|what\s+i\s+just\s+(?:said|told\s+you))\b(?!\s+about)"
    r"|what\s+i\s+(?:said|told\s+you|shared)\s+about\s+(?P<s1>.+?)"
    r"|(?:everything|all)\s+(?:you\s+(?:know|remember)\s+)?about\s+(?P<s2>.+?)"
    r"|(?:the\s+)?(?:memory|memories|notes?|context|info(?:rmation)?|details)\s+about\s+(?P<s3>.+?)"
    r"|about\s+(?P<s4>.+?)"
    r"|(?P<s6>my\s+(?:friend|girlfriend|boyfriend|partner|sister|brother|mom|mother|dad|father|cousin|colleague|wife|"
    r"husband|son|daughter|boss|teacher|roommate)\s+\S.+?)"
    r"|(?P<s5>.+?)\s+from\s+(?:your\s+)?memory)\s*[.!]*$"
)
# Marker 174: forget ONE specific saved fact by its wording ("forget that Riya likes tea", "delete the memory that
# my sister lives in Pune"). A bare "forget that." still means the last saved memory (_FORGET below).
_FORGET_FACT = re.compile(
    r"(?i)^(?:please\s+)?(?:forget|delete|erase|remove)\s+(?:the\s+(?:memory|fact|note)\s+)?that\s+(?P<fact>\S.{2,})$")
_FORGET_LAST = re.compile(
    r"(?i)^(?:don'?t|do\s+not)\s+(?:use|remember|keep)\s+(?:that|this|it)(?:\s+anymore|\s+any\s+more)?\s*[.!]*$"
    r"|^stop\s+(?:using|remembering)\s+(?:that|this|it)\s*[.!]*$"
)
_UPDATE = re.compile(
    r"(?i)^(?:change|update|correct|fix|edit|replace)\s+what\s+you\s+(?:remember|know|have\s+saved|saved)\s+"
    r"about\s+(?P<subject>.+?)(?:\s*[:,—–-]\s*|\s+to\s+(?:say\s+)?(?:that\s+)?|\.\s+)(?P<content>.+)$"
)
_SHARE = [
    re.compile(r"(?i)^share\s+(?:(?:this|that|it)|what\s+i\s+(?:told\s+you|said|shared)\s+about\s+(?P<subject>.+?)|"
               r"(?:my\s+)?(?:notes?|memor(?:y|ies)|context)\s+about\s+(?P<subject2>.+?))\s+with\s+(?P<target>.+?)"
               r"(?P<rest>(?:[,.;]|\s+and\s+|\s+but\s+).*)?$"),
    re.compile(r"(?i)^you\s+(?:can|may|are\s+allowed\s+to)\s+(?:tell|share\s+(?:this|that|it)\s+with)\s+"
               r"(?P<target>[^,.;]+?)(?:\s+(?:about\s+(?:this|that|it)|that\s+i\s+told\s+you))?(?P<rest>(?:[,.;]|\s+and\s+).*)?$"),
    re.compile(r"(?i)^(?:it'?s|it\s+is)\s+(?:ok|okay|fine|alright)\s+(?:to|if\s+you)\s+(?:tell|share\s+(?:this|that|it)\s+with)\s+"
               r"(?P<target>[^,.;]+?)(?:\s+about\s+(?:this|that|it))?(?P<rest>(?:[,.;]|\s+and\s+).*)?$"),
]
# Marker 182 (real chat, 12 Oct): "meri sari information bata do usko", "mere bf ko bolo …", "Again msg him" reached the
# model, which answered "dispatch ho chuki hai" / "emergency notification bhej diya" although Jupiter has NO channel to
# another person. These requests to pass something to SOMEONE ELSE are recognised here and answered truthfully by the
# personal assistant. "Tell me …", questions ("what should I tell him?") and consent sharing ("share this with X",
# "you can tell X about this", matched first above) are not relays.
_NOT_A_PERSON = r"(?:me|us|myself|ourselves|it|this|that|these|those|you|yourself|jupiter|about|what|how|why|when|a|an|the|story|joke|everything|anything)\b"
# Relay targets: a stop word excludes only the WHOLE token, so "maintainer@example.invalid" is still a recipient
# (handled by the relay, never by Gmail) while "tell jupiter that …" is not.
_NOT_A_PERSON_TOKEN = _NOT_A_PERSON[:-2] + r"(?![\w.@-])"
_RELAY = [
    re.compile(r"(?i)^(?:again\s+|also\s+|now\s+|quickly\s+)?(?:tell|message|msg|text|ping|notify|alert|inform|remind|warn|"
               r"whats\s*app|dm)\s+(?!" + _NOT_A_PERSON_TOKEN + r")(?P<target>him|her|them|my\s+[a-z]+|[a-z][\w.@-]*)\b"
               r"(?:\s+(?:again|now|too|also))?(?:\s|[,.!?]|$)"),
    re.compile(r"(?i)^(?:again\s+)?let\s+(?!" + _NOT_A_PERSON_TOKEN + r")(?P<target>him|her|them|my\s+[a-z]+|[a-z][\w.@-]*)\s+know\b"),
    re.compile(r"(?i)^(?:again\s+)?(?:send|forward|deliver|pass)\s+(?:this|that|it|these|my\s+\w+(?:\s+\w+)?|"
               r"(?:a|the|an)\s+(?:message|msg|notification|alert|reminder|note|invoice|bill|list|demand)s?)\s+"
               r"(?:again\s+)?(?:on\s+)?to\s+(?!" + _NOT_A_PERSON_TOKEN + r")(?P<target>.+?)\s*[.!?]*$"),
    re.compile(r"(?i)^(?:again\s+)?send\s+(?!" + _NOT_A_PERSON_TOKEN + r")(?P<target>him|her|them|my\s+[a-z]+|[a-z]\w*)\s+"
               r"(?:a|an|the|this|that|my)\s+(?:message|msg|notification|alert|reminder|note|invoice|bill)\b"),
]
_RELAY_VERB_HI = (r"(?:bata\s*(?:do|dena|de|dijiye|o)|batao|bol\s*(?:do|dena|de|dijiye)|bolo|keh\s*(?:do|dena|de)|kaho|"
                  r"(?:msg|message|text|sms)\s*(?:kar|karo|kr|bhej)\w*(?:\s*(?:do|dena|de))?|notification\s*bhej\w*|"
                  r"bhej\s*(?:do|dena|de|dijiye|o)|inform\s*kar\w*|yaad\s*dila\s*(?:do|dena|o))")
_RELAY_TARGET_HI = (r"(?:usko|unko|unhe|unhein|inko|inhe|"
                    r"(?!(?:mujh|muj|hum|ham|apne|apni|khud|sab|is|us|kis|jis|kal|aaj|ghar|code|isse)\w*\s+ko\b)[a-z]+\s+ko)")
_RELAY_HI = [
    re.compile(r"(?i)\b(?P<target>" + _RELAY_TARGET_HI + r")\b(?P<gap>.{0,90}?)\b(?P<verb>" + _RELAY_VERB_HI + r")\b"),
    re.compile(r"(?i)\b(?P<verb>" + _RELAY_VERB_HI + r")\s+(?P<target>usko|unko|unhe|inko|inhe|[a-z]+\s+ko)\b"),
    re.compile(r"(?P<target>उसको|उसे|उनको|उन्हें|इनको|इन्हें|\S+\s+को)\s*.{0,80}?(?:बता\s*दो|बताओ|बोल\s*दो|बोलो|कह\s*दो|"
               r"मैसेज\s*कर\s*दो|भेज\s*दो)"),
]
_RELAY_QUESTION = re.compile(r"(?i)^(?:what|how|should|shall|can\s+i|could\s+i|kya|kaise|kab|kyun|kya\s+main)\b|\?\s*$")
_RELAY_PRONOUNS = frozenset({"him", "her", "them", "usko", "unko", "unhe", "unhein", "inko", "inhe",
                             "उसको", "उसे", "उनको", "उन्हें", "इनको", "इन्हें"})
_RELAY_RELATIONS = frozenset({
    "bf", "gf", "boyfriend", "girlfriend", "husband", "wife", "partner", "fiance", "fiancee", "pati", "patni", "biwi",
    "mom", "mum", "mummy", "mumma", "mother", "maa", "dad", "papa", "father", "bhai", "bhaiya", "brother", "didi",
    "behen", "sister", "dost", "friend", "boss", "sir", "madam", "teacher", "colleague", "manager", "team",
    "दोस्त", "पापा", "मम्मी", "माँ", "भाई", "भैया", "दीदी", "बहन", "पति", "पत्नी"})


def _person_target(target: str, original: str) -> bool:
    """Only clearly-a-person targets: a pronoun, "my <someone>", a relationship word, an e-mail address, or a name the
    user wrote capitalised. "Python ko install karna bata do" (tell ME how) and "text summary" are not relays."""
    value = normalize(target).strip(" .!?,")
    lowered = value.casefold()
    if not value or lowered in _RELAY_PRONOUNS or lowered in _RELAY_RELATIONS or "@" in value:
        return bool(value)
    if lowered.startswith("my ") or lowered.split()[-1] in _RELAY_RELATIONS:
        return True
    first = value.split()[0]
    if re.fullmatch(r"[a-z][a-z0-9_.]*[0-9_][a-z0-9_.]*", first):
        return True                                     # a Jupiter username such as khushi_12 (marker 185)
    return first[:1].isupper() and first[1:].islower() and re.search(rf"\b{re.escape(first)}\b", original) is not None


def _relay_intent(value: str) -> "PersonalIntent | None":
    for pattern in _RELAY:
        match = pattern.match(value)
        if match and _person_target(match.group("target"), value):
            return PersonalIntent(RELAY_MESSAGE, target=normalize(match.group("target")).strip(" .!?,"), content=value)
    if _RELAY_QUESTION.search(value):
        return None                     # "usko kya bolu?", "should I tell him?" are questions, not instructions
    for pattern in _RELAY_HI:
        match = pattern.search(value)
        if match:
            target = re.sub(r"(?i)\s+ko$|\s+को$", "", normalize(match.group("target"))).strip(" .!?,")
            if not _person_target(target, value):
                continue
            groups = match.groupdict()
            named = (target.casefold() not in _RELAY_PRONOUNS and target.casefold() not in _RELAY_RELATIONS
                     and not target.casefold().startswith("my "))
            # "Python ko install karna bata do" asks ME how to install Python; "the maintainer ko bata do ki …" is a relay.
            if named and str(groups.get("verb") or "").casefold().startswith("bata") and str(groups.get("gap") or "").strip():
                continue
            return PersonalIntent(RELAY_MESSAGE, target=target, content=value)
    return None


# Marker 184: "Which email am I signed in with?" got "For security reasons, I cannot share or verify the specific email
# address you're logged in with": the model never receives the account email (identity context sends only the display
# name), so it refused. Questions about the SIGNED-IN account's OWN email / mobile number are answered by the personal
# assistant from the server-authenticated session. Anything naming someone else ("the maintainer email", "my sister's number",
# "what is maintainer@example.invalid's password") is not this intent.
_ACCOUNT_EMAIL = r"(?:e-?mail|gmail|mail\s*id|email\s*(?:id|address)|login|log-?in\s*id|account|user\s*id|id)"
_ACCOUNT_PHONE = r"(?:phone(?:\s*number)?|mobile(?:\s*number)?|mobile\s*no|phone\s*no|number|contact\s*number)"
_ACCOUNT_SELF = [
    # "which email am I signed in with", "what account am I logged into", "which gmail is logged in here"
    re.compile(r"(?i)^(?:which|what)\s+(?:e-?mail|gmail|account|email\s*(?:id|address)|id)\s+(?:am\s+i|is|are\s+we|have\s+i)\s+"
               r"(?:currently\s+|now\s+)?(?:signed|logged|log)\s*(?:in|into|on)(?:\s+(?:with|as|to|here|on\s+this\s+(?:app|device)))?"
               r"(?:\s+(?:right\s+)?now)?\s*\??$"),
    re.compile(r"(?i)^(?:which|what)\s+(?:e-?mail|gmail|account|email\s*(?:id|address))\s+(?:is\s+)?(?:currently\s+)?"
               r"(?:signed|logged)\s*in(?:\s+(?:here|now|right\s+now|on\s+this\s+(?:app|device)))?\s*\??$"),
    # "what is my email / my registered email / my account email / my login id", "tell me my email"
    re.compile(r"(?i)^(?:what(?:'s|\s+is)|tell\s+me|show\s+(?:me\s+)?|give\s+me)\s+my\s+(?:registered\s+|account\s+|jupiter\s+|"
               r"current\s+|login\s+|sign[- ]?in\s+)?" + _ACCOUNT_EMAIL + r"(?:\s+(?:address|id))?"
               r"(?:\s+(?:on|in|for)\s+(?:jupiter|this\s+(?:app|account|device)))?\s*\??$"),
    # "what is my registered phone/mobile number", "what number is on my account"
    re.compile(r"(?i)^(?:what(?:'s|\s+is)|tell\s+me|show\s+(?:me\s+)?|give\s+me)\s+my\s+(?:registered\s+|account\s+|jupiter\s+|"
               r"verified\s+|login\s+)" + _ACCOUNT_PHONE + r"(?:\s+(?:on|in|for)\s+(?:jupiter|this\s+(?:app|account)))?\s*\??$"),
    re.compile(r"(?i)^(?:what(?:'s|\s+is)|tell\s+me)\s+my\s+" + _ACCOUNT_PHONE + r"\s+(?:on|in|for|registered\s+(?:on|with|in))\s+"
               r"(?:jupiter|this\s+(?:app|account)|my\s+(?:jupiter\s+)?account)\s*\??$"),
    re.compile(r"(?i)^(?:which|what)\s+" + _ACCOUNT_PHONE + r"\s+is\s+(?:on|linked\s+to|registered\s+(?:on|with|to))\s+"
               r"my\s+(?:jupiter\s+)?account\s*\??$"),
    # "is my email and phone saved", "what email and phone are on my account"
    re.compile(r"(?i)^(?:what|which)\s+(?:e-?mail|email\s*(?:id|address))\s+(?:and|&)\s+" + _ACCOUNT_PHONE
               + r"\s+(?:are|is)\s+(?:on|in|linked\s+to)\s+my\s+(?:jupiter\s+)?account\s*\??$"),
    # Hinglish / Hindi: "main kis email se login hoon", "kaunsa email login hai", "mera registered email kya hai"
    re.compile(r"(?i)^(?:main|mai|mein|hum)\s+(?:kis|kaun\s*se|kaunse)\s+(?:e-?mail|gmail|account|id)\s+se\s+"
               r"(?:login|logged\s*in|sign\s*in|signed\s*in)\s+(?:hoon|hun|hu|hain|hai)\s*\??$"),
    re.compile(r"(?i)^(?:kaun\s*sa|kaunsa|konsa|kon\s*sa|kaun\s*si|kaunsi|konsi)\s+(?:e-?mail|gmail|account|id)\s+"
               r"(?:abhi\s+)?(?:login|logged\s*in|sign\s*in|signed\s*in)\s+(?:hai|h)\s*\??$"),
    re.compile(r"(?i)^(?:mera|meri)\s+(?:registered\s+|account\s+(?:ka\s+)?|login\s+)?(?:e-?mail|gmail|email\s*id|mail\s*id|"
               r"login\s*id|mobile(?:\s*number)?|phone(?:\s*number)?|number)\s+(?:kya|kaunsa|konsa)\s+(?:hai|h)\s*\??$"),
    re.compile(r"(?:मैं|हम)\s+(?:किस|कौन\s*से)\s+(?:ईमेल|ई-मेल|अकाउंट|आईडी)\s+से\s+(?:लॉगिन|साइन\s*इन)\s+(?:हूँ|हूं|हैं)\s*\??$"),
    re.compile(r"(?:मेरा|मेरी)\s+(?:रजिस्टर्ड\s+)?(?:ईमेल|ई-मेल|मोबाइल\s*नंबर|फ़ोन\s*नंबर|फोन\s*नंबर|नंबर)\s+क्या\s+है\s*\??$"),
]
_ACCOUNT_PHONE_WORD = re.compile(r"(?i)\b(?:phone|mobile|number|contact)\b|मोबाइल|फ़ोन|फोन|नंबर")
_ACCOUNT_EMAIL_WORD = re.compile(r"(?i)\b(?:e-?mail|gmail|mail|login|log-?in|sign(?:ed)?[- ]?in|account|user\s*id|id)\b"
                                 r"|ईमेल|ई-मेल|लॉगिन|अकाउंट|आईडी")


# Marker 185: another person's contact details are never looked up: they become a consent request to that account.
# "my sister's email" stays a memory question (the user may have saved it); only a named person / username / email
# address starts a request. CONTACT_REQUEST.reference = "explicit" for "ask X to share …" (always answered directly).
_CONTACT_EMAIL = r"(?:e-?mail(?:\s*(?:id|address))?|gmail(?:\s*id)?|mail\s*id)"
_CONTACT_PHONE = r"(?:(?:phone|mobile|contact|whats\s*app|cell)(?:\s*(?:number|no\.?|nambar))?|number|nambar)"
_CONTACT_FIELD = r"(?:" + _CONTACT_EMAIL + "|" + _CONTACT_PHONE + ")"
_CONTACT_TARGET = r"(?P<target>[^\s'’?]+(?:\s+[^\s'’?]+){0,2}?)"
_CONTACT_REQUEST = [
    re.compile(r"(?i)^(?:what(?:'s|\s+is|\s+are)|tell\s+me|give\s+me|share|send\s+me|show\s+me|can\s+i\s+(?:get|have)|"
               r"could\s+i\s+(?:get|have)|i\s+(?:want|need)|get\s+me)\s+" + _CONTACT_TARGET + r"['’]s?\s+"
               r"(?P<fields>" + _CONTACT_FIELD + r"(?:\s+(?:and|&|or)\s+" + _CONTACT_FIELD + r")?)\s*[.?!]*$"),
    re.compile(r"(?i)^(?P<explicit>ask)\s+" + _CONTACT_TARGET + r"\s+(?:to|if\s+(?:i|we)\s+can\s+(?:get|have))\s+"
               r"(?:(?:share|send|give)\s+(?:me\s+)?)?(?:his|her|their|the)\s+"
               r"(?P<fields>" + _CONTACT_FIELD + r"(?:\s+(?:and|&|or)\s+" + _CONTACT_FIELD + r")?)(?:\s+with\s+me)?\s*[.?!]*$"),
    re.compile(r"(?i)^(?:mujhe\s+|muje\s+)?" + _CONTACT_TARGET + r"\s+ka\s+(?P<fields>" + _CONTACT_FIELD
               + r"(?:\s+(?:aur|and|&)\s+" + _CONTACT_FIELD + r")?)\s+(?:kya\s+hai|chahiye|do|de\s*do|dedo|bata\s*do|"
               r"batao|share\s+karo|bhejo)\s*[.?!]*$"),
]


# Marker 186: an EXPLICIT request to call another person. "call me (when …)" is a Jupiter call to the user (handled
# earlier as TASK_NOTIFY / the call feature) and "tell X …" is a message: neither ever matches here.
_CALL_REASON = (r"(?P<rest>(?:\s*[,.!:;-]\s*|\s+(?:it'?s|its|it\s+is|this\s+is|because|cause|since|as|he|she|they|"
                r"urgent(?:ly)?|asap|right\s+now|immediately|now|please|emergency|accident|hospital|there'?s|i\s+need)\b).*)?")
_CALL_USER = [
    re.compile(r"(?i)^(?:please\s+|urgently\s+|quickly\s+|immediately\s+)*(?:video\s+|voice\s+|audio\s+)?(?:call|ring|dial|phone)\s+"
               r"(?!me\b|us\b|myself\b|back\b)"
               r"(?P<target>my\s+[a-z]+|him|her|them|[a-z][\w.@-]*(?:\s+[a-z][\w.-]*){0,2}?)" + _CALL_REASON + r"\s*[.!?]*$"),
    re.compile(r"(?i)^(?:make|place|start)\s+an?\s+(?:urgent\s+|emergency\s+)?(?:video\s+|voice\s+)?call\s+(?:to|with)\s+"
               r"(?P<target>my\s+[a-z]+|him|her|them|[a-z][\w.@-]*(?:\s+[a-z][\w.-]*){0,2}?)" + _CALL_REASON + r"\s*[.!?]*$"),
    re.compile(r"(?i)^(?P<target>(?:[a-z][\w.-]*\s+){0,2}[a-z][\w.@-]*|usko|unko|unhe)\s+ko\s+(?:abhi\s+|turant\s+|jaldi\s+)?"
               r"(?:video\s+)?(?:call|phone)\s+(?:karo|kar\s*do|lagao|laga\s*do|milao)(?P<rest>\b.*)?\s*[.!?]*$"),
]


def _call_user_intent(value: str) -> "PersonalIntent | None":
    for pattern in _CALL_USER:
        match = pattern.match(value)
        if match:
            target = normalize(match.group("target")).strip(" .!?,")
            if target.casefold() in {"me", "jupiter", "you", "it", "this", "that"}:
                return None
            words = target.split()
            # A multi-word target is a person only when written as a name ("Call the project creator"), never "call the
            # function twice" / "call this api again".
            if len(words) > 1 and not target.casefold().startswith("my ") and not all(w[:1].isupper() for w in words):
                return None
            return PersonalIntent(CALL_USER, target=target, content=value)
    return None


# Marker 223 (real 28 Oct, jupiter.testing12 on WT): "call my owner acc. u know my owner acc?" -> "Who should I call?"
# -> "my owner acc maintainer@example.invalid" -> "Gmail isn't connected". Jupiter now learns who "my owner acc" /
# "my girlfriend" is from the user's own words and resolves it next time.
EMAIL_ADDRESS = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w-])")
_ACCOUNT_ALIASES = {"acc": "account", "acct": "account", "accnt": "account", "a/c": "account", "ac": "account",
                    "id": "account"}
_IDENTIFIER_SUFFIX = re.compile(r"(?i)(?:'s|s')?\s+(?:jupiter\s+)?(?:email|e-mail|mail|username|user\s*name|"
                                r"email\s+address|address)$")
_IDENTIFIER_STATEMENT = re.compile(
    r"(?i)^\W*(?:and\s+|also\s+|btw\s+|ok(?:ay)?\s+)?(?:my|mera|meri|mere)\s+(?P<label>[a-z][a-z/'\s-]{0,40}?)\s*"
    r"(?:\bis\b|=|:|-|\bhai\b)?\s*(?P<identifier>" + EMAIL_ADDRESS.pattern + r")\s*(?:hai|h)?[.!]*\s*$")
_USERNAME_STATEMENT = re.compile(
    r"(?i)^\W*(?:and\s+|also\s+|btw\s+)?(?:my|mera|meri|mere)\s+(?P<label>[a-z][a-z/'\s-]{0,40}?)(?:'s|s')?\s+"
    r"(?:jupiter\s+)?(?:username|user\s*name)\s*(?:is|=|:|-|hai)?\s*(?P<identifier>[\w.-]{2,40})\s*(?:hai|h)?[.!]*\s*$")
_IDENTIFIER_QUESTION = re.compile(
    r"(?i)^\W*(?:(?:do|d|did)\s+)?(?:you|u|ya|tum|tu)\s+(?:know|remember|jaante\s+ho|jante\s+ho|janti\s+ho)\s+"
    r"(?:what|which|who)?\s*(?:is\s+)?(?:my|mera|meri|mere)\s+(?P<label>[a-z][a-z/'\s-]{0,40}?)\s*\??\s*$"
    r"|^\W*(?:what|which)(?:'s|\s+is)\s+my\s+(?P<label2>[a-z][a-z/'\s-]{0,40}?(?:account|acc|a/c|id|username))\s*\??\s*$")


def identifier_label(phrase: str) -> str:
    """"my Owner Acc" / "owner a/c" -> "owner account" (the user's own label, normalised for matching)."""
    value = " ".join(str(phrase or "").split()).strip(" .,!?:;-'\"")
    value = re.sub(r"(?i)^(?:my|mera|meri|mere)\s+", "", value)
    value = _IDENTIFIER_SUFFIX.sub("", value).casefold()
    value = re.sub(r"(?:'s|s')$", "", value)
    return " ".join(_ACCOUNT_ALIASES.get(word, word) for word in value.split())[:60]


def identifier_statement(text: str) -> tuple[str, str] | None:
    """("owner account", "maintainer@example.invalid") for "my owner acc maintainer@example.invalid" / "my girlfriend's
    username is friend_handle"; None for anything else."""
    value = " ".join(str(text or "").split())
    match = _IDENTIFIER_STATEMENT.match(value) or _USERNAME_STATEMENT.match(value)
    if not match:
        return None
    label = identifier_label(match.group("label"))
    if not label or label in {"name", "naam"} or len(label.split()) > 4:
        return None
    return label, match.group("identifier")


def identifier_question_label(text: str) -> str:
    """"u know my owner acc?" -> "owner account"; "" when the message is not that question."""
    match = _IDENTIFIER_QUESTION.match(" ".join(str(text or "").split()))
    if not match:
        return ""
    return identifier_label(match.group("label") or match.group("label2") or "")


def _contact_request_intent(value: str) -> "PersonalIntent | None":
    for pattern in _CONTACT_REQUEST:
        match = pattern.match(value)
        if not match:
            continue
        target = normalize(match.group("target")).strip(" .?!,")
        first = target.split()[0] if target else ""
        lowered = target.casefold()
        if (not target or lowered.startswith(("my ", "mera ", "meri ", "mere ", "your ", "his ", "her ", "their "))
                or lowered in {"me", "my", "mera", "meri", "i", "you", "him", "her", "them", "usko", "unka", "uska"}):
            return None                      # "my sister's email" is the user's own memory, not another account
        person = "@" in target or first[:1].isupper() or re.search(r"[\d_]", target) is not None
        if not person:
            return None
        fields_text = match.group("fields").casefold()
        fields = []
        if re.search(_CONTACT_EMAIL, fields_text, re.I):
            fields.append("email")
        if re.search(_CONTACT_PHONE, re.sub(_CONTACT_EMAIL, " ", fields_text, flags=re.I), re.I):
            fields.append("phone")
        return PersonalIntent(CONTACT_REQUEST, target=target, content=",".join(fields or ["email"]),
                              reference="explicit" if match.groupdict().get("explicit") else "")
    return None


def _account_self_intent(value: str) -> "PersonalIntent | None":
    if not any(pattern.match(value) for pattern in _ACCOUNT_SELF):
        return None
    wants_phone = bool(_ACCOUNT_PHONE_WORD.search(value))
    wants_email = bool(_ACCOUNT_EMAIL_WORD.search(value)) or not wants_phone
    subject = "both" if wants_phone and wants_email and re.search(r"(?i)\b(?:and|&|aur)\b|और", value) else (
        "phone" if wants_phone and not re.search(r"(?i)\b(?:e-?mail|gmail|mail)\b|ईमेल|ई-मेल", value) else "email")
    return PersonalIntent(ACCOUNT_SELF, subject=subject, content=value)


_UNSHARE = [
    re.compile(r"(?i)^(?:stop|quit)\s+sharing\s+(?:(?:this|that|it)|what\s+i\s+(?:told\s+you|said)\s+about\s+(?P<subject>.+?))"
               r"(?:\s+with\s+(?P<target>.+?))?\s*[.!]*$"),
    re.compile(r"(?i)^unshare\s+(?:(?:this|that|it)|what\s+i\s+(?:told\s+you|said)\s+about\s+(?P<subject>.+?))"
               r"(?:\s+(?:with|from)\s+(?P<target>.+?))?\s*[.!]*$"),
    re.compile(r"(?i)^(?:don'?t|do\s+not)\s+(?:tell|share\s+(?:this|that|it)\s+with)\s+(?P<target>[^,.;]+?)\s+"
               r"(?:about\s+(?:this|that|it)|anymore|any\s+more)\s*[.!]*$"),
]
_ATTRIBUTE_YES = re.compile(
    r"(?i)\b(?:tell|let)\s+(?:her|him|them|\w+)\s+(?:know\s+)?(?:(?:that\s+)?it'?s|it\s+(?:was|came))?\s*from\s+me\b"
    r"|\byou\s+can\s+(?:say|mention|tell\s+(?:her|him|them))\s+(?:that\s+)?(?:i|it\s+was\s+me|it'?s\s+from\s+me)\b"
    r"|\bheard\s+(?:about\s+(?:her|him|them|you)\s+)?from\s+me\b|\bmention\s+(?:that\s+)?(?:it'?s|it\s+was)?\s*(?:from\s+)?me\b"
    r"|\bsay\s+(?:that\s+)?(?:i\s+told\s+you|it'?s\s+from\s+me|it\s+came\s+from\s+me)\b|\bwith\s+my\s+name\b"
)
_ATTRIBUTE_NO = re.compile(
    r"(?i)\b(?:don'?t|do\s+not|never)\s+(?:say|mention|tell\s+(?:her|him|them))\s+(?:that\s+)?(?:it'?s|it\s+was|it\s+came|i)\b"
    r"|\banonymous(?:ly)?\b|\bwithout\s+(?:saying|mentioning|naming)\b|\bkeep\s+(?:me|my\s+name)\s+out\b"
)
_SECOND_REQUEST = re.compile(
    r"(?i)[,;]?\s+(?:and|then|also|plus)\s+(?:then\s+|also\s+)?(?:please\s+)?(?:can\s+you\s+)?"
    r"(?=(?:crop|edit|enhance|upscale|sharpen|blur|brighten|darken|remove\s+the\s+background|recolou?r|restore|retouch|"
    r"generate|draw|create\s+an?\s+(?:image|picture|photo|poster|logo)|make\s+(?:it|this|the\s+(?:image|photo|picture))|"
    r"search|check\s+my|find\s+my|remind\s+me|set\s+a\s+reminder)\b)"
)

# ── task notification preference ("WhatsApp me when done", "call me when this finishes", "tell me if it fails",
#    "only WhatsApp; don't call"): a request to be told about a TASK's outcome, never a timed reminder ──────────────
_TASK_NOTIFY = re.compile(
    r"(?i)\b(?:whats\s*app|call|ring|text|sms|e-?mail|notify|tell|ping|message|inform|alert)\s+me\b[^.?!]*?"
    r"\b(?:when|once|after|if|as\s+soon\s+as)\b[^.?!]*?"
    r"\b(?:it|this|that|the\s+(?:work|task|job|build|run))\b[^.?!]*?"
    r"\b(?:done|finish(?:es|ed)?|complete[sd]?|ready|fails?|failed|over|stuck|needs?\s+me)\b"
    r"|\b(?:whats\s*app|call|ring|text|sms|e-?mail|notify|tell|ping|message)\s+me\s+(?:when|once)\s+(?:you(?:'re|\s+are)\s+)?"
    r"(?:done|finished|complete)\b"
    r"|\blet\s+me\s+know\s+(?:when|once|if)\b[^.?!]*?\b(?:done|finish(?:es|ed)?|complete[sd]?|ready|fails?|failed)\b"
    r"|^\W*only\s+(?:whats\s*app|email|sms|in[- ]app)\b"
)

_NOTIFY_CLAUSE = re.compile(
    r"(?i),?\s+(?:and|then|&)\s+(?:then\s+)?(?:please\s+)?(?=(?:whats\s*app|call|ring|text|sms|e-?mail|notify|tell|ping|"
    r"message|inform|alert|let)\s+me\b)")
_CHANNEL_ONLY_SENTENCE = re.compile(
    r"(?i)^\W*(?:only|just|don'?t|do\s+not|never|no)\b[^.?!;]*\b(?:whats\s*app|call(?:s|ing)?|ring|sms|text(?:s)?|"
    r"e-?mails?|phone|notifications?)\b")
_CONTEXT_ONLY_SENTENCE = re.compile(r"(?i)^\W*(?:i'?m|i\s+am|i'?ll\s+be)\s+(?:going|heading|off|leaving|out|away|busy)\b")


def split_work_and_notification(text: str) -> tuple[str, str]:
    """Separate the requested WORK from the notification instruction in one message.

    "Write a hello.txt file. Notify me when done." -> ("Write a hello.txt file.", "Notify me when done.")
    "Build the release and tell me if it fails"   -> ("Build the release", "tell me if it fails")
    "Only WhatsApp; don't call."                   -> ("", "Only WhatsApp; don't call.")
    Context-only sentences ("I'm going to class.") belong to neither and are dropped from the work."""
    value = " ".join(str(text or "").split())
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.!?;])\s+", value):
        pieces.extend(part for part in _NOTIFY_CLAUSE.split(sentence) if part and part.strip())
    work, notify = [], []
    for piece in pieces:
        piece = piece.strip()
        if _TASK_NOTIFY.search(piece) or _CHANNEL_ONLY_SENTENCE.search(piece):
            notify.append(piece)
        elif not _CONTEXT_ONLY_SENTENCE.search(piece):
            work.append(piece)
    return " ".join(work).strip(), " ".join(notify).strip()


# ── reminder patterns ───────────────────────────────────────────────────────────────────────────────────────────
_WHEN_LATER = (r"(?:(?:in|after)\s+(?:just\s+|only\s+|about\s+)?(?:\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|"
               r"fifteen|twenty|thirty|half\s+an|half)\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)"
               r"|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?|tomorrow(?:\s+(?:morning|at\s+\S+))?|tonight)")
_CALL_REMINDER_CREATE = re.compile(
    r"(?i)\b(?:call|ring)\s+me\b(?=[^.?!]*"
    r"(?:\b(?:in|after)\s+(?:\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|"
    r"twenty|thirty|forty|forty-five|fortyfive|sixty|half(?:\s+an)?)\s*"
    r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b"
    r"|\b(?:at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|today|tomorrow|tonight|day\s+after\s+tomorrow|"
    r"next\s+(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)|"
    r"on\s+(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b)))"
)
_CALL_ME_LATER = re.compile(
    rf"(?i)^(?:please\s+|jupiter,?\s+|hey\s+jupiter,?\s+|okay,?\s+)*(?:call|ring)\s+me\s+(?:back\s+)?{_WHEN_LATER}\b"
    r"|(?i:\b\d+\s*(?:second|seconds|sec|minute|minutes|min|ghante|ghanta)\s+(?:baad|bad|mein|me)\s+(?:mujhe\s+)?"
    r"(?:call|phone)\s+(?:karna|karo|kar\s*do|kariye|kijiye))")
_REMIND_INFO_QUESTION = re.compile(r"(?i)\bremind\s+me\s+(?:what|how|why|who|where|which|whether|of\s+(?:what|how|your|the\s+(?:name|formula|meaning)))\b")
_REMIND_CREATE = re.compile(
    r"(?i)\bremind\s+me\b|\bset\s+(?:up\s+)?(?:a|an|the|my)\s+reminder\b|\b(?:add|create|make|schedule)\s+(?:a|an|the)\s+reminder\b"
    r"|\bset\s+(?:a|an)\s+(?:alarm|alert)\s+(?:to\s+remind|for\s+me\s+to)\b"
)
# Marker 220 (real 26 Oct: "1 baje bol dena call kerke station aa raha hai" -> the chat model said "Done" and nothing
# was set): a time ("1 baje", "12:54 pe", "10 minute baad", "kal subah 7 baje") plus a Hinglish ask-to-tell verb.
_HI_TIME = (r"(?:\b\d{1,2}(?:[:.]\d{2})?\s*baje\b|\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b|"
            r"\b\d+\s*(?:second|sec|minute|min|ghante|ghanta)s?\s+(?:baad|bad|mein|me)\b)")
_HI_ASK = (r"(?:bol\s+(?:dena|do|dijiye)|bata\s+(?:dena|do|dijiye)|batana|yaad\s+(?:dila\s*(?:dena|do|na|iyo|dijiye)|dilana)|"
           r"uth(?:a|aa)\s*(?:dena|do)|uthana|jaga\s*(?:dena|do)|jagana|"
           r"(?:call|phone|ring)\s+(?:karna|karo|kar\s*do|kar\s*dena|kar\s*ke|karke|kerke|krke|kariye|kijiye))")
_REMIND_CREATE_HI = re.compile(rf"(?i){_HI_TIME}.*\b{_HI_ASK}\b|\b{_HI_ASK}\b.*{_HI_TIME}")
# "change it to 12:54", "make it 1:30", "12:54 pe kar do", "instead 12:54", "nahi, 12:54" (only right after a reminder)
REMINDER_RETIME = re.compile(
    rf"(?i)^(?:(?:no|nahi|nahin|actually|sorry|wait|oh)[,!. ]+)?(?:(?:change|move|shift|make|set|push|update)\s+(?:it|that|this|the\s+time)"
    rf"\s+(?:to|at|for)\s+(?P<a>.+?)|(?:instead|rather)\s+(?P<b>.+?)|(?P<c>.+?)\s+(?:pe|par|ko)\s+(?:kar|set\s+kar|rakh|shift\s+kar)"
    rf"\s*(?:do|dena|de|dijiye)|(?P<d>.+?)\s+(?:instead|kar\s+do))\s*[.!]*$"
)
_REMIND_LIST = re.compile(
    r"(?i)^(?:(?:what|which)\s+(?:are\s+)?(?:(?:all\s+)?my\s+|the\s+)?(?:upcoming\s+|pending\s+|active\s+)?reminders?"
    r"(?:\s+(?:do\s+i\s+have|have\s+i\s+(?:got|set)|are\s+(?:there|set|pending)|for\s+(?:today|tomorrow|this\s+week)))?"
    r"|(?:show|list|tell|give)\s+(?:me\s+)?(?:all\s+)?(?:of\s+)?(?:my|the)\s+(?:upcoming\s+|pending\s+|active\s+)?reminders?(?:\s+for\s+\w+(?:\s+\w+)?)?"
    r"|(?:do\s+i\s+have|have\s+i\s+(?:got|set))\s+any\s+reminders?(?:\s+\w+(?:\s+\w+)?)?"
    r"|(?:any|my)\s+reminders?(?:\s+for\s+\w+(?:\s+\w+)?)?)\s*[?.!]*$"
)
_REMIND_CANCEL = re.compile(
    r"(?i)^(?:cancel|delete|remove|stop|clear|turn\s+off|drop|get\s+rid\s+of|dismiss)\s+"
    r"(?P<scope>(?:all\s+(?:of\s+)?(?:my\s+)?|both\s+(?:of\s+)?(?:my\s+)?|my\s+|the\s+|that\s+|this\s+|those\s+)?)"
    r"(?P<pre>(?:[\w'’-]+\s+){0,3}?)reminders?\b(?:\s+(?:about|for|to|on|regarding|at)\s+(?P<post>.+?))?\s*[.!]*$"
)
_REMIND_SNOOZE = re.compile(
    r"(?i)^snooze\b(?:\s+(?:that|it|this|my|the)\s*)?(?:\s*reminder)?(?:\s+(?:for|by)\s+)?\s*"
    r"(?P<amount>\d+|an?|half\s+an|one|two|three|four|five|ten|fifteen|twenty|thirty)?\s*"
    r"(?P<unit>minutes?|mins?|hours?|hrs?)?\b.*$"
)
_REMIND_UPDATE = re.compile(
    r"(?i)^(?:move|change|reschedule|push|shift|update|postpone|set|make)\s+"
    r"(?:(?:my|the|that|this)\s+)?(?P<pre>(?:[\w'’-]+\s+){0,3}?)reminder\b(?:\s+(?:about|for|to|on|regarding)\s+(?P<post>.+?))?"
    r"\s+(?:to|for|until|till|at|on)\s+(?P<when>.+?)\s*[.!]*$"
)


def _person_subject(text: str) -> str:
    """The person a memory statement is about, or SELF / ""."""
    value = normalize(text)
    about = re.search(rf"(?:\b(?:context|information|info|details|facts|things|notes?|stuff)\s+)?\babout\s+{_NAME}", value)
    if about and clean_name(about.group(1)):
        return clean_name(about.group(1))
    lead = re.match(rf"^(?:that\s+|this\s*:\s*)?{_NAME}(?:\s+(?:from|at|in|of)\s+(?:my\s+|the\s+)?[\w'’-]+"
                    rf"(?:\s+[\w'’-]+)?)?(?:['’]s\b|\s+(?:is|was|has|had|likes|loves|hates|lives|works|"
                    rf"studies|goes|plays|and\s+i|will|can|prefers|wants|needs|doesn'?t|isn'?t))", value)
    if lead:
        first = lead.group(1).split()
        candidate = " ".join(_possessive_base(token) for token in first)
        if clean_name(candidate):
            return clean_name(candidate)
    identify = re.match(rf"^(?:that\s+)?(?:this|that|he|she|they)\s+(?:is|are)\s+{_NAME}\b", value)
    if identify and clean_name(identify.group(1)):
        return clean_name(identify.group(1))
    relation = re.match(rf"^(?:that\s+)?my\s+(?:[\w-]+\s+){{0,2}}?(?:friend|girlfriend|boyfriend|partner|wife|husband|"
                        rf"sister|brother|mom|mother|dad|father|cousin|aunt|uncle|colleague|classmate|mentor|teacher|boss|"
                        rf"manager|roommate|neighbou?r|grandma|grandpa|grandmother|grandfather|son|daughter|fianc[eé]e?)\s+"
                        rf"(?:is\s+(?:named|called)\s+)?{_NAME}", value)
    if relation and clean_name(relation.group(1)):
        return clean_name(relation.group(1))
    possessive = re.search(rf"\b({_NAME_TOKEN})['’]s\s+(?:birthday|anniversary|number|favou?rite|phone|username)\b", value)
    if possessive and clean_name(possessive.group(1)):
        return clean_name(possessive.group(1))
    if re.match(r"(?i)^(?:that\s+)?(?:i\b|i'm|i've|i'd|my\b|me\b|myself\b)", value):
        return SELF
    return ""


def _split_second_request(body: str) -> tuple[str, str]:
    match = _SECOND_REQUEST.search(body)
    if not match or match.start() < 6:
        return body, ""
    return body[:match.start()].rstrip(" ,;."), body[match.end():].strip()


def _subject_from(value: str | None) -> str:
    raw = normalize(value or "").strip(" ?.!,")
    if not raw:
        return ""
    if re.fullmatch(r"(?i)(?:me|myself|about\s+me|me\s+personally|my\s+(?:life|preferences|self))", raw):
        return SELF
    if re.fullmatch(r"(?i)(?:that|this|it)", raw):
        return LAST
    raw = re.sub(r"(?i)^(?:my\s+)?(?:friend|girlfriend|boyfriend|partner|sister|brother|mom|dad|cousin|colleague)\s+", "", raw)
    return clean_name(raw) or (raw if len(raw.split()) <= 3 and not re.search(r"(?i)\b(how|what|why|when|where|which)\b", raw) else "")


def classify_personal_intent(text: str) -> PersonalIntent:
    """Deterministic, conservative classification; anything unclear is NONE (normal chat)."""
    original = normalize(text)
    if not original or len(original) > 20000:
        return PersonalIntent()
    value = strip_request_prefix(original)
    lowered = value.casefold()

    # Reminders first: "don't forget to remind me", "remember to remind me" are reminders, not memories.
    if _REMIND_LIST.match(value):
        return PersonalIntent(REMINDER_LIST, content=value)
    snooze = _REMIND_SNOOZE.match(value)
    if snooze:
        return PersonalIntent(REMINDER_SNOOZE, content=value, reference=LAST)
    cancel = _REMIND_CANCEL.match(value)
    if cancel:
        detail = normalize(" ".join(filter(None, [(cancel.group("pre") or "").strip(), (cancel.group("post") or "").strip()])))
        scope = (cancel.group("scope") or "").strip().casefold()
        return PersonalIntent(REMINDER_CANCEL, content=detail, reference=("all" if scope.startswith("all") else
                                                                           LAST if not detail and scope in {"that", "this", ""} else ""))
    update = _REMIND_UPDATE.match(value)
    if update:
        detail = normalize(" ".join(filter(None, [(update.group("pre") or "").strip(), (update.group("post") or "").strip()])))
        return PersonalIntent(REMINDER_UPDATE, content=update.group("when"), subject=detail, reference="" if detail else LAST)
    if _CALL_ME_LATER.search(value):
        # Marker 219 (real WT 26 Oct: "Call me after just 10 sec" -> "I can't make phone calls" plus a memory question
        # about "After Just"): a Jupiter call to the user at a time is a reminder delivered as a call.
        return PersonalIntent(REMINDER_CREATE, content=value)
    if _CALL_REMINDER_CREATE.search(value):
        body, remainder = _split_second_request(value)       # "... call me tomorrow at 7 to study, and ..."
        return PersonalIntent(REMINDER_CREATE, content=body, remainder=remainder)
    if _REMIND_CREATE_HI.search(value) and not _REMIND_INFO_QUESTION.search(value):
        body, remainder = _split_second_request(value)
        return PersonalIntent(REMINDER_CREATE, content=body, remainder=remainder)
    if _TASK_NOTIFY.search(value) and not re.search(r"(?i)\bremind\s+me\b", value):
        return PersonalIntent(TASK_NOTIFY, content=value)
    if _REMIND_CREATE.search(value) and not (_REMIND_INFO_QUESTION.search(value) and not re.search(
            r"(?i)\b(?:at\s+\d|in\s+\d|tomorrow|tonight|every|on\s+\w+day|next\s+\w+)\b", value)):
        body, remainder = _split_second_request(value)
        return PersonalIntent(REMINDER_CREATE, content=body, remainder=remainder)

    for pattern in _UNSHARE:
        match = pattern.match(value)
        if match:
            groups = match.groupdict()
            return PersonalIntent(MEMORY_UNSHARE, subject=_subject_from(groups.get("subject")) or LAST,
                                  target=normalize(groups.get("target") or "").strip(" .!?"))
    for pattern in _SHARE:
        match = pattern.match(value)
        if match:
            groups = match.groupdict()
            target = normalize(groups.get("target") or "").strip(" .!?")
            target = re.sub(r"(?i)\s+(?:too|as\s+well|also)$", "", target)
            # A refusal wins over any overlapping positive phrase ("don't mention it was me").
            attribution = False if _ATTRIBUTE_NO.search(value) else (True if _ATTRIBUTE_YES.search(value) else None)
            subject = _subject_from(groups.get("subject") or groups.get("subject2")) or LAST
            if not clean_name(target) and not re.search(r"@", target):
                break
            return PersonalIntent(MEMORY_SHARE, subject=subject, target=target, attribution=attribution)

    contact = _contact_request_intent(value)
    if contact is not None:
        return contact
    call = _call_user_intent(value)
    if call is not None:
        return call
    relay = _relay_intent(value)
    if relay is not None:
        return relay
    account = _account_self_intent(value)
    if account is not None:
        return account

    update_memory = _UPDATE.match(value)
    if update_memory:
        return PersonalIntent(MEMORY_UPDATE, subject=_subject_from(update_memory.group("subject")),
                              content=update_memory.group("content").strip())

    if _FORGET_LAST.match(value):
        return PersonalIntent(MEMORY_FORGET, reference=LAST)
    forget_fact = _FORGET_FACT.match(value)
    if forget_fact and not re.fullmatch(r"(?i)(?:it|that|this|about\s+it)\s*(?:,.*)?", forget_fact.group("fact").strip(" .!")):
        return PersonalIntent(MEMORY_FORGET, content=forget_fact.group("fact").strip(" .!"))
    forget = _FORGET.match(re.sub(r"(?i)^please\s+", "", value))
    # "Forget it (, never mind)" dismisses a topic; "Forget that." targets the last saved memory (the handler falls
    # back to normal chat when nothing was saved in this conversation).
    # "Forget it (, never mind)" dismisses a topic; "Forget that." targets the last saved memory (the handler falls
    # back to normal chat when nothing was saved in this conversation).
    dismissal = re.fullmatch(r"(?i)forget\s+it[.!]*", value) or re.match(
        r"(?i)^forget\s+(?:it|that|about\s+it)\s*,?\s*(?:never\s*mind|nvm|it'?s\s+fine|whatever|leave\s+it)", value)
    if forget and not dismissal:
        groups = forget.groupdict()
        if groups.get("last"):
            return PersonalIntent(MEMORY_FORGET, reference=LAST)
        subject = next((groups[key] for key in ("s1", "s2", "s3", "s4", "s5", "s6") if groups.get(key)), "")
        resolved = _subject_from(subject)
        if resolved:
            return PersonalIntent(MEMORY_FORGET, subject=resolved)

    # "Who am I?" / "Do you remember me?" are answered from the authenticated account profile, not a memory search.
    from services.identity_context import is_account_identity_question
    for pattern in ([] if is_account_identity_question(value) else _READ):
        match = pattern.match(value)
        if match:
            subject = _subject_from(match.group("subject")) or (SELF if not match.group("subject") else "")
            if subject:
                return PersonalIntent(MEMORY_READ, subject=subject)
            break

    if _NOT_A_MEMORY.match(value):
        return PersonalIntent()
    write = _WRITE.match(value)
    if write:
        body = value[write.end():].strip(" :,-")
        # "(the following) context/information/details about Asha. Asha is …" -> subject Asha, content "Asha is …".
        # _WRITE may already have consumed "the following", so the noun phrase is matched on its own too.
        body = re.sub(r"(?i)^(?:(?:the\s+)?following\s+)?(?:context|information|info|details|facts|things|notes?)"
                      r"\s+(?:about|on|regarding|for)\s+(?=\S)", "about ", body)
        body = re.sub(r"(?i)^(?:the\s+following|following)\b[\s:,-]*", "", body).strip(" :,-")
        body, remainder = _split_second_request(body)
        subject = ""
        lead = re.match(rf"(?i:about)\s+{_NAME}\s*[.:;,–—-]+\s*(?P<rest>\S.*)$", body, re.S)
        if lead and clean_name(lead.group(1)):
            subject, body = clean_name(lead.group(1)), lead.group("rest").strip()
        if len(body) < 3:
            return PersonalIntent()
        subject = subject or _person_subject(body)
        return PersonalIntent(MEMORY_WRITE, subject=subject, content=body, remainder=remainder)
    # The statement first, the request last: "Rahul likes chess, remember that." / "My locker code is 12. Note it."
    trailing = _WRITE_TRAILING.match(value)
    if trailing and len(trailing.group("body").split()) >= 2 and not _TRAILING_NOT_A_FACT.match(trailing.group("body")):
        body = trailing.group("body").strip(" :,-")
        return PersonalIntent(MEMORY_WRITE, subject=_person_subject(body), content=body)
    return PersonalIntent()
