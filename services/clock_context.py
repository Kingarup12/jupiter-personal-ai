"""The user's real local clock for replies (marker 220).

Real 26 Oct: at 12:55 IST "What is the time?" was answered "Abhi 12:52" because nothing ever told the reply model the
time; it guessed from the conversation (a 12:54 reminder had just been set). Every chat turn now carries one line with
the exact local date and time, a Live session carries it at the start, and a spoken "what's the time?" in Live is
answered by the app from the clock (a call can last many minutes, so a time fixed at the start goes stale).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from services.reminders import DEFAULT_TIMEZONE, valid_zone, zone

_ZONE_LABELS = {"Asia/Kolkata": "IST", "Asia/Dhaka": "BST", "Asia/Kathmandu": "NPT", "UTC": "UTC", "Etc/UTC": "UTC"}

# "what's the time", "time kya hua", "kitne baje hain", "abhi kitna baja", "समय क्या है", "কটা বাজে", "what day is it today"
_TIME_QUESTION = re.compile(
    r"(?ix)^(?:hey\s+|ok(?:ay)?\s+|jupiter[,\s]+|acha\s+|achha\s+|btw\s+|bro\s+|bata(?:o)?\s+)*(?:"
    r"(?:what(?:'s|\s+is)?|tell\s+me)\s+(?:the\s+)?(?:current\s+|exact\s+)?time(?:\s+(?:now|right\s+now|is\s+it))?"
    r"|what\s+time\s+is\s+it(?:\s+(?:now|right\s+now))?"
    r"|(?:abhi\s+|aby\s+)?(?:time|samay)\s+(?:kya|kitna)\s+(?:hua|hai|ho\s+raha\s+hai|hua\s+hai)"
    r"|(?:abhi\s+)?(?:kya|kitna)\s+(?:time|samay)\s+(?:hua|hai|ho\s+raha\s+hai)"
    r"|(?:abhi\s+)?kitn[ae]\s+baj[ae](?:\s+(?:hain|hai|gaye|gaya|rahe\s+hain))?"
    r"|(?:what(?:'s|\s+is)?\s+(?:the\s+|today'?s\s+)?date(?:\s+today)?|what\s+day\s+is\s+(?:it|today)"
    r"|aaj\s+(?:kya|kaun\s*sa|konsa)\s+(?:date|din|tarikh|tareekh)\s*(?:hai)?|aaj\s+(?:date|tarikh)\s+kya\s+hai)"
    r")(?:\s+(?:bhai|boss|yaar|please|pls|jupiter|abhi|now|hai|hain|today|right\s+now))*\s*[?.!]*$"
)
_TIME_QUESTION_NATIVE = re.compile(r"^(?:अभी\s+)?(?:कितने\s+बजे|कितना\s+बजा|समय\s+क्या|टाइम\s+क्या)|^(?:এখন\s+)?ক(?:টা|’টা)\s+বাজে|^সময়\s+কত")
_DATE_WORDS = re.compile(r"(?i)\b(?:date|day|din|tarikh|tareekh)\b|তারিখ|তারীখ|तारीख")


def user_timezone(user_id: str | None, client_timezone: str | None = None) -> str:
    """The account's saved zone (Settings), else the device's, else Asia/Kolkata. Never raises."""
    if user_id:
        try:
            from services.reminders import ReminderService
            return ReminderService().effective_timezone(user_id, client_timezone)
        except Exception:
            pass
    return valid_zone(client_timezone) or DEFAULT_TIMEZONE


def local_now(tz_name: str | None, now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(zone(tz_name or DEFAULT_TIMEZONE))


def _label(tz_name: str) -> str:
    return _ZONE_LABELS.get(tz_name, tz_name)


def _clock(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def clock_line(tz_name: str | None, now: datetime | None = None) -> str:
    """System line for a reply: the real current local date and time."""
    name = valid_zone(tz_name) or DEFAULT_TIMEZONE
    value = local_now(name, now)
    return (f"Current date and time for the user (real clock, {name}): {value.strftime('%A')}, {value.day} "
            f"{value.strftime('%B %Y')}, {_clock(value)} {_label(name)}. Use exactly this for any question about the "
            "time, date or day; never guess a time or reuse a time from earlier messages.")


def is_time_question(text: str) -> bool:
    value = " ".join(str(text or "").split()).strip()
    if not value or len(value) > 80:
        return False
    return bool(_TIME_QUESTION.match(value) or _TIME_QUESTION_NATIVE.match(value))


def time_answer(text: str, tz_name: str | None, now: datetime | None = None) -> str:
    """What the app tells Live to say for a time/date question (the facts only; Live says it in the user's language)."""
    name = valid_zone(tz_name) or DEFAULT_TIMEZONE
    value = local_now(name, now)
    if _DATE_WORDS.search(str(text or "")):
        return f"Today is {value.strftime('%A')}, {value.day} {value.strftime('%B %Y')}. The time is {_clock(value)}."
    return f"The time is {_clock(value)} ({_label(name)}), {value.strftime('%A')} {value.day} {value.strftime('%B')}."
