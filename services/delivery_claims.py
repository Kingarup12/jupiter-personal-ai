"""Truthful delivery wording for messages meant for another person (marker 182).

Real chat, 12 Oct 2026: asked "meri sari information bata do usko" / "Again msg him", the model replied "Invoice …
dispatch ho chuki hai", "Maine the maintainer ke system mein emergency flash notification bhej diya hai". Jupiter has NO channel
to another person: there is no user-to-user inbox or push, Gmail access is read-only, there is no SMS/WhatsApp, and
calls / task notifications reach only the account's OWN devices. The explicit "share … with <Jupiter user>" command
stores a consented fact Jupiter may use when that person talks to it; it is not a notification and is never described
as one.

Under this architecture the delivery states are therefore:
- delivered: never, for another person (no channel exists);
- read: never tracked (no read receipts exist);
- failed / not sent: every request to message, notify or alert someone else.

Two layers keep Jupiter truthful: requests are answered by ``relay_not_sent_reply`` (personal assistant, typed chat and
Live app turns), and ``false_delivery_claim`` is a backstop on generated chat replies, which never run a delivery
operation, so any claim of having sent something to someone is false by construction and is replaced.
"""
from __future__ import annotations

import re

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_HINGLISH_CUES = re.compile(
    r"(?i)\b(?:ko|bata|batao|bolo|bol|usko|unko|unhe|uske|unke|hai|hain|karo|kar|mere|meri|mera|nahi|kya|bhej|bhejo|"
    r"kuch|msg\s+kar|aur|bhi|haa+n?|tum|aap|hoon|hun|kis|kaun|kaunsa|konsa|kya|hai)\b")
_PRONOUN_TARGETS = {"him": "him", "her": "her", "them": "them"}


def _language(text: str) -> str:
    value = str(text or "")
    if _DEVANAGARI.search(value):
        return "hi"
    return "hi-Latn" if len(_HINGLISH_CUES.findall(value)) >= 1 else "en"


def _who(target: str, language: str) -> str:
    value = " ".join(str(target or "").split()).strip(" .!?,")
    lowered = value.casefold()
    if language == "en":
        if not value or lowered in _PRONOUN_TARGETS:
            return _PRONOUN_TARGETS.get(lowered, "them")
        return "your " + value[3:] if lowered.startswith("my ") else value
    if language == "hi":
        if not value or lowered in {"उसको", "उसे", "उनको", "उन्हें", "इनको", "इन्हें"}:
            return "उन्हें"
        return f"{value} को"
    if not value or lowered in {"him", "her", "them", "usko", "usse", "use", "unko", "unhe", "unhein", "inko", "inhe"}:
        return "unhe"
    if lowered.startswith("my "):
        value = value[3:]
    if lowered.startswith(("mere ", "meri ", "mera ")):
        value = value.split(" ", 1)[1]
    return f"{value} ko" if value[:1].isupper() else f"tumhare {value} ko"


def relay_not_sent_reply(user_text: str, target: str = "") -> str:
    """What Jupiter says when asked to tell / message / notify someone else: nothing was sent, and what to do next."""
    language = _language(user_text)
    who = _who(target, language)
    if language == "hi":
        return (f"मैंने {who} कुछ नहीं भेजा है, इसलिए उनके पास कुछ नहीं पहुँचा। मैं सिर्फ़ किसी के Jupiter ऐप में संदेश भेज "
                "सकती हूँ: उनका सही Jupiter यूज़रनेम बताइए। SMS, WhatsApp या फ़ोन नोटिफ़िकेशन नहीं भेज सकती; चाहें तो मैं "
                "मैसेज लिखने में मदद कर देती हूँ।")
    if language == "hi-Latn":
        return (f"Maine {who} kuch nahi bheja hai, isliye unke paas kuch nahi pahuncha. Main sirf kisi ke Jupiter app mein "
                "message bhej sakti hoon: unka exact Jupiter username batao (“<username> ko bata do ki …”). SMS, WhatsApp "
                "ya phone notification nahi bhej sakti; chaho toh message likhne mein help kar deti hoon.")
    return (f"I haven't sent anything to {who}, so nothing reached them. I can only pass a message to someone's Jupiter "
            "app: tell me their exact Jupiter username (“Tell <username> that …”). I can't send texts, WhatsApp messages "
            "or phone notifications, but I'm happy to help you write it.")


def relay_ambiguous_reply(user_text: str, name: str) -> str:
    """More than one other Jupiter account has exactly this username: nothing is sent and no account is revealed."""
    if _language(user_text) in {"hi", "hi-Latn"}:
        return (f"“{name}” naam ke ek se zyada Jupiter accounts hain, isliye maine kuch nahi bheja. Unka unique, exact "
                "Jupiter username batao.")
    return (f"More than one Jupiter account uses the name “{name}”, so I haven't sent anything. Tell me their unique, "
            "exact Jupiter username.")


def relay_username_required_reply(user_text: str) -> str:
    """A recipient given as an email address: Jupiter messages go to usernames (and an address lookup would reveal
    whether it is registered), so nothing is sent and nothing is looked up."""
    if _language(user_text) in {"hi", "hi-Latn"}:
        return ("Jupiter message email address par nahi, Jupiter username par jaata hai. Maine kuch nahi bheja: unka exact "
                "Jupiter username batao.")
    return ("Jupiter messages go to a Jupiter username, not an email address, so I haven't sent anything. Tell me their "
            "exact Jupiter username (“Tell <username> that …”).")


# ── marker 185 correction: Jupiter DOES have one real user-to-user channel ──────────────────────────────────────────
# Directed messages (services/initiative_engine.send_directed, /api/proactive/directed-messages): a message to one exact
# registered Jupiter account, stored server-side and shown in that account's Jupiter app by its inbox poll. Marker 182
# missed it and told users nothing could be sent. Relays to a resolvable Jupiter user now use it; everything else
# (pronouns, unknown people, phones, SMS, WhatsApp) is still truthfully "not sent".
def relay_body(text: str, target: str) -> str:
    """The words to pass on, or "" when the request names no content ("send this to the maintainer", "bata do usko")."""
    value = " ".join(str(text or "").split())
    hindi = re.search(r"(?i)\s(?:ki|कि)\s+(.{2,})$", value)
    if hindi and re.search(r"(?i)\b(?:ko|को|bata|bol|keh|kaho|बता|बोल|कह)\b", value):
        return hindi.group(1).strip(" .!?")
    if not target:
        return ""
    index = value.casefold().find(str(target).casefold())
    if index < 0:
        return ""
    after = value[index + len(target):].strip(" ,:-")
    after = re.sub(r"(?i)^(?:know\s+)?(?:that\s+)?", "", after).strip(" ,:-")
    after = re.sub(r"(?i)^to\s+", "please ", after)
    return after.strip(" .!?") if len(after.strip(" .!?")) >= 2 else ""


def relay_queued_reply(user_text: str, name: str, body: str, alert: str = "", inbox_enabled: bool = True) -> str:
    """Wording follows the server result exactly (marker 186): "sent" = FCM accepted a content-free alert for their
    device (not received, not read); otherwise the message is waiting in their Jupiter inbox."""
    quoted = f"“{body[:300]}”"
    hinglish = _language(user_text) in {"hi", "hi-Latn"}
    if not inbox_enabled:
        return (f"Maine {name} ke Jupiter inbox mein message rakh diya hai: {quoted}. Unhone Jupiter messages band kiye "
                "hain, isliye jab tak woh chalu nahi karte, unhe yeh nahi dikhega.") if hinglish else (
            f"I've put a message for {name} in their Jupiter inbox: {quoted}. They've turned Jupiter messages off, so "
            "they won't see it until they turn them back on.")
    if alert == "sent":
        return (f"Maine {name} ke liye Jupiter message bhej diya hai: {quoted}, aur unke phone par Jupiter alert bhi "
                "bheja gaya hai. Unhone padha ya nahi, yeh abhi pata nahi.") if hinglish else (
            f"I've sent {name} a Jupiter message: {quoted}, and a Jupiter alert was sent to their phone. I can't tell "
            "whether they've seen or read it yet.")
    return (f"Maine {name} ke liye Jupiter message bhej diya hai: {quoted}. Yeh unke Jupiter inbox mein hai aur app "
            "kholne par dikhega; unke phone par alert nahi ja saka.") if hinglish else (
        f"I've sent {name} a Jupiter message: {quoted}. It's waiting in their Jupiter inbox and they'll see it when "
        "they open Jupiter; no alert could be sent to their phone.")


def relay_unavailable_reply(user_text: str, name: str) -> str:
    if _language(user_text) in {"hi", "hi-Latn"}:
        return f"Main {name} ko yeh message deliver nahi kar paayi. Kuch nahi bheja gaya."
    return f"I couldn't deliver that message to {name}. Nothing was sent."


def relay_needs_body_reply(user_text: str, name: str) -> str:
    if _language(user_text) in {"hi", "hi-Latn"}:
        return f"{name} ko kya message bhejun? Aise bolo: “{name} ko bata do ki …”. Abhi kuch nahi bheja."
    return f"What should I tell {name}? Say it like “Tell {name} that …”. Nothing has been sent yet."


# ── backstop on generated replies ────────────────────────────────────────────────────────────────────────────────
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_SENTENCES = re.compile(r"[^.!?।\n]+[.!?।]*")
# English: a completed act of sending/telling by Jupiter ("I've sent", "I just notified him", "Jupiter has alerted").
_EN_FIRST_PERSON_SENT = re.compile(
    r"(?i)\b(?:i(?:'ve|\s+have)?|i\s+just|i\s+already|jupiter\s+(?:has|just))\s+(?:just\s+|already\s+|now\s+|also\s+)?"
    r"(?:sent|messaged|texted|notified|alerted|pinged|forwarded|delivered|dispatched|flashed|"
    r"(?:passed|handed)\s+(?:it|this|that|them|your\s+\w+)\s+(?:on|along|over)|let\s+(?:him|her|them)\s+know|"
    # The name must really be capitalised: under (?i) "[A-Z][a-z]+" also matched "you" ("As I told you, …").
    r"(?:told|informed)\s+(?:him|her|them|(?!you\b)(?-i:[A-Z][a-z]+)))\b")
_EN_THIRD_PARTY = re.compile(
    r"(?i)\b(?:him|her|them|his|their)\b|\b(?:system|phone|inbox|device)\b"
    r"|\byour\s+(?:bf|gf|boyfriend|girlfriend|husband|wife|partner|mom|mum|mother|dad|father|friend|boss|sister|brother)\b"
    r"|(?-i:\bto\s+[A-Z][a-z]+\b|\b(?:sent|messaged|texted|notified|alerted|pinged|told|informed)\s+[A-Z][a-z]+\b)")
# Hinglish / Hindi perfect. Sending verbs ("bhej diya", "flash kar diya", "msg kar diya") count with "maine"; telling verbs
# ("bata diya", "bol diya") only with a third-party recipient or a delivery noun ("maine bata diya tha" is often to the user).
_HI_SEND = re.compile(
    r"(?i)\b(?:bhej|send|forward|flash|pass|deliver|notify|inform|msg|message|ping|alert)\s*(?:kar\s*)?"
    r"(?:diya|di|dia|diye|chuki|chuka|chuke)\b|भेज\s*(?:दिया|दी|दिए|चुकी|चुका)" + r"(?![ऀ-ॿ])"
    + r"|(?:मैसेज|नोटिफ़?िकेशन|सूचना)\s*(?:कर|भेज)\s*(?:दिया|दी)" + r"(?![ऀ-ॿ])")
_HI_TELL = re.compile(r"(?i)\b(?:bata|bol|keh)\s*(?:diya|di|dia|diye|chuki|chuka|chuke)\b|(?:बता|बोल|कह)\s*(?:दिया|दी|दिए|चुकी|चुका)"
                      + r"(?![ऀ-ॿ])")
# (?![ऀ-ॿ]) is the Devanagari word end: Python's \b does not work after a vowel sign, so "कर दी" matched the start of the
# polite imperative "कर दीजिए" ("please message them yourself") and the truthful suggestion was flagged as a claim.
_HI_FIRST_PERSON = re.compile(r"(?i)\b(?:maine|mainne|main\s+ne)\b|मैंने")
_HI_THIRD_PARTY = re.compile(
    r"(?i)\b(?:uske|unke|iske|inke|usko|unko|unhe|inko|inhe)\b|उनके|उसके|उन्हें|उसको"
    # "Maine the maintainer ko bata diya", "tumhare bf ko bhej diya": a named person or relation as the recipient.
    r"|(?-i:\b[A-Z][a-z]+\s+ko\b)|\b(?:bf|gf|boyfriend|girlfriend|husband|wife|pati|patni|mummy|mumma|papa|bhai|"
    r"bhaiya|didi|behen|dost)\s+ko\b")
_DELIVERY_NOUN = re.compile(r"(?i)\b(?:message|msg|notification|alert|ping|sms|whats\s*app|e-?mail|invoice|demand|"
                            r"emergency)\b|मैसेज|नोटिफ़?िकेशन|सूचना")
_DISPATCHED = re.compile(r"(?i)\bdispatch\s*(?:ho|kar)\s*(?:chuk[ia]|gay[ai]|gai|diy?a|di)\b")
_USER_ONLY = re.compile(r"(?i)\b(?:you|tumhe|tumko|aapko)\b")


def false_delivery_claim(reply: str) -> bool:
    """True when a generated reply claims to have sent / told / notified something to another person."""
    text = _CODE.sub(" ", str(reply or ""))
    for sentence in _SENTENCES.findall(text):
        if _DISPATCHED.search(sentence):
            return True                                   # "invoice dispatch ho chuki hai": always a delivery claim
        if _EN_FIRST_PERSON_SENT.search(sentence) and (
                _EN_THIRD_PARTY.search(sentence) or (_DELIVERY_NOUN.search(sentence) and not _USER_ONLY.search(sentence))):
            return True
        third_party = _HI_THIRD_PARTY.search(sentence) or _DELIVERY_NOUN.search(sentence)
        if _HI_SEND.search(sentence) and (third_party or (_HI_FIRST_PERSON.search(sentence) and not _USER_ONLY.search(sentence))):
            return True
        if _HI_TELL.search(sentence) and third_party:
            return True
    return False
