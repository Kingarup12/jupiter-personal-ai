"""Reminders on Jupiter's existing per-user scheduler (initiative.db) — natural language in, exact time out.

Conventions (always stated back in the confirmation, never silent):
  "morning" 9:00 · "afternoon" 14:00 · "evening" 18:00 · "tonight"/"night" 21:00 · a date with no time 9:00 ·
  a bare hour 1–7 with no am/pm means PM ("at 5" = 5 PM), 8–11 means AM.
Time zone: the user's explicit Proactive-assistance setting, else the device's time zone sent with the request, else
Asia/Kolkata. Due times are stored in UTC; recurrences are computed on the local wall clock, so 8 AM stays 8 AM across
DST changes. One occurrence of a series is pending at a time; the next is created when one is delivered (idempotent
per series and due time), so restarts never duplicate or lose a reminder.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Kolkata"
PART_OF_DAY = {"morning": 9, "afternoon": 14, "evening": 18, "tonight": 21, "night": 21}
DEFAULT_HOUR = 9
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_RE = r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
                 "forty-five": 45, "fortyfive": 45, "sixty": 60, "half an": 0.5, "half": 0.5}


def weekday_index(token: str) -> int:
    token = token.casefold()
    return next(index for index, name in enumerate(WEEKDAYS) if name.startswith(token[:3]))


def month_index(token: str) -> int:
    token = token.casefold()
    return next(index for index, name in enumerate(MONTHS) if name.startswith(token[:3])) + 1


# Windows Python ships no IANA database; without the `tzdata` package every ZoneInfo() raised and the existing
# scheduler silently created no reminders at all. Fixed offsets keep the no-DST zones exact if tzdata is ever absent.
_FIXED_ZONES = {"Asia/Kolkata": 330, "Asia/Calcutta": 330, "Asia/Dhaka": 360, "Asia/Kathmandu": 345,
                "Asia/Dubai": 240, "Asia/Singapore": 480, "Asia/Tokyo": 540, "UTC": 0, "Etc/UTC": 0, "GMT": 0}


def zone(name: str | None):
    key = str(name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError):
        minutes = _FIXED_ZONES.get(key, _FIXED_ZONES.get(DEFAULT_TIMEZONE if key not in _FIXED_ZONES else key, 0))
        return timezone(timedelta(minutes=minutes), key)


# Legacy IANA links some runtimes still report (WebView2 on Windows says "Asia/Calcutta").
_ZONE_ALIASES = {"Asia/Calcutta": "Asia/Kolkata", "Asia/Katmandu": "Asia/Kathmandu", "Asia/Saigon": "Asia/Ho_Chi_Minh",
                 "Asia/Rangoon": "Asia/Yangon", "Europe/Kiev": "Europe/Kyiv", "US/Eastern": "America/New_York",
                 "US/Central": "America/Chicago", "US/Mountain": "America/Denver", "US/Pacific": "America/Los_Angeles"}


def valid_zone(name: str | None) -> str | None:
    value = str(name or "").strip()[:80]
    value = _ZONE_ALIASES.get(value, value)
    if not value:
        return None
    try:
        ZoneInfo(value)
        return value
    except (ZoneInfoNotFoundError, ValueError):
        return value if value in _FIXED_ZONES else None


@dataclass
class ParsedWhen:
    due: datetime | None = None           # aware, in the user's zone
    recurrence: dict | None = None        # {"freq": daily|weekly|monthly|yearly, "weekdays": [...], "hour", "minute", ...}
    has_time: bool = False
    has_date: bool = False
    spans: list = field(default_factory=list)
    assumed: str = ""                     # the stated convention used, if any ("9 AM")
    passed: bool = False                  # an explicit time today that has already passed

    @property
    def complete(self) -> bool:
        return self.due is not None


def _mark(spans: list, match: re.Match) -> None:
    spans.append((match.start(), match.end()))


def _number(token: str) -> float | None:
    token = token.casefold().strip()
    if token.isdigit():
        return float(token)
    return _NUMBER_WORDS.get(token)


# Marker 220 (real 26 Oct: "1 baje bol dena call kerke station aa raha hai" set nothing): Hinglish clock times.
_BAJE = re.compile(r"(?i)\b(?:(subah|subha|savere|sawere|dopahar|dopehar|dupahar|shaam|sham|raat|rat)\s+(?:ke\s+|ko\s+)?)?"
                   r"(\d{1,2})(?:[:.](\d{2}))?\s*baje\b")


def _baje_clock(text: str, spans: list) -> tuple[int, int] | None:
    match = _BAJE.search(text)
    if not match:
        return None
    part = (match.group(1) or "").casefold()
    hour, minute = int(match.group(2)), int(match.group(3) or 0)
    if hour > 12 and not part and hour <= 23:
        pass                                          # "14 baje": already 24 h
    elif hour > 12 or minute > 59:
        return None
    elif part in {"subah", "subha", "savere", "sawere"}:
        hour = 0 if hour == 12 else hour
    elif part in {"dopahar", "dopehar", "dupahar"}:
        hour = hour if hour == 12 else hour + 12
    elif part in {"shaam", "sham"}:
        hour = hour if hour == 12 else hour + 12
    elif part in {"raat", "rat"}:
        hour = 0 if hour == 12 else (hour + 12 if hour >= 6 else hour)
    elif 1 <= hour <= 7:
        hour += 12                                    # "1 baje" = 1 PM, like "at 1"
    _mark(spans, match)
    return hour, minute


def _clock(text: str, spans: list) -> tuple[int, int] | None:
    """Explicit clock time (hour, minute) in 24 h, or None."""
    baje = _baje_clock(text, spans)
    if baje:
        return baje
    for pattern in (
        r"\b(?:at\s+)?(\d{1,2})(?::|\.)(\d{2})\s*(a\.?m\.?|p\.?m\.?)?(?![\d/])",
        r"\b(?:at\s+)?(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?![a-z])",
        r"\bat\s+(\d{1,2})(?!\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?|%|st|nd|rd|th|\d))\b",
    ):
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        groups = [group for group in match.groups()]
        hour = int(groups[0])
        minute = int(groups[1]) if len(groups) > 2 and groups[1] and groups[1].isdigit() else 0
        meridiem = next((group for group in groups[1:] if group and not group.isdigit()), "")
        meridiem = meridiem.replace(".", "").casefold()
        if hour > 23 or minute > 59:
            continue
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        elif not meridiem and 1 <= hour <= 7:
            hour += 12                    # "at 5" = 5 PM (documented convention)
        _mark(spans, match)
        return hour, minute
    for word, value in (("noon", (12, 0)), ("midday", (12, 0)), ("midnight", (0, 0))):
        match = re.search(rf"\b(?:at\s+)?{word}\b", text, re.I)
        if match:
            _mark(spans, match)
            return value
    return None


def _part_of_day(text: str, spans: list) -> tuple[str, int] | None:
    match = re.search(r"\b(?:in\s+the\s+|this\s+|tomorrow\s+(?=morning|afternoon|evening|night))?"
                      r"(morning|afternoon|evening|tonight|night)\b", text, re.I)
    if not match:
        return None
    word = match.group(1).casefold()
    start = match.start(1) if match.group(0).casefold().startswith("tomorrow") else match.start()
    spans.append((start, match.end()))
    return word, PART_OF_DAY[word]


def parse_when(text: str, now: datetime) -> ParsedWhen:
    """Parse the scheduling part of a reminder request relative to `now` (aware, user zone)."""
    value = " ".join(str(text or "").split())
    result = ParsedWhen()
    spans = result.spans

    # Marker 219 (real WT 26 Oct: "Call me after just 10 sec" was not understood): "in" or "after", optional
    # "just/only", seconds too, and Hinglish "10 second baad" / "5 minute mein".
    amounts = (r"(\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty(?:-?five)?|sixty|"
               r"half\s+an|half)")
    units = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|ghante|ghanta|din)"
    relative = (re.search(rf"\b(?:in|after)\s+(?:just\s+|only\s+|about\s+)?{amounts}\s*{units}\b", value, re.I)
                or re.search(rf"\b{amounts}\s*{units}\s+(?:baad|bad|mein|me)\b", value, re.I))
    if relative:
        amount = _number(relative.group(1)) or 0
        unit = relative.group(2).casefold()
        delta = (timedelta(seconds=amount) if unit.startswith("sec") else timedelta(minutes=amount) if unit.startswith("min")
                 else timedelta(hours=amount) if unit.startswith(("hour", "hr", "ghant")) else
                 timedelta(days=amount) if unit.startswith(("day", "din")) else timedelta(weeks=amount))
        _mark(spans, relative)
        # seconds stay exact ("in 10 seconds"); minutes and longer are rounded to the minute as before
        due = (now + delta).replace(microsecond=0) if unit.startswith("sec") else \
            (now + delta).replace(second=0, microsecond=0)
        if due <= now:
            due += timedelta(minutes=1)
        clock = _clock(value, spans) if unit.startswith(("day", "week")) else None
        if clock:
            due = due.replace(hour=clock[0], minute=clock[1])
        result.due, result.has_time, result.has_date = due, True, True
        return result

    recurrence: dict | None = None
    every = re.search(rf"\bevery\s+(day|morning|evening|night|weekday|weekend|week|month|year|other\s+day|"
                      rf"{_WEEKDAY_RE}(?:s)?(?:\s*(?:,|and|&)\s*{_WEEKDAY_RE}(?:s)?)*)\b"
                      rf"|\b(daily|weekly|monthly|yearly|annually)\b|\bon\s+(weekdays|weekends)\b"
                      rf"|\bon\s+{_WEEKDAY_RE}s\b", value, re.I)
    if every:
        token = (every.group(1) or every.group(every.lastindex or 1) or "").casefold()
        _mark(spans, every)
        whole = every.group(0).casefold()
        if token in {"day", "daily", "other day"} or token in {"morning", "evening", "night"}:
            recurrence = {"freq": "daily"}
            if token in {"morning", "evening", "night"}:
                recurrence["part"] = token
        elif token in {"weekday", "weekdays"} or "weekdays" in whole:
            recurrence = {"freq": "weekly", "weekdays": [0, 1, 2, 3, 4]}
        elif token in {"weekend", "weekends"} or "weekends" in whole:
            recurrence = {"freq": "weekly", "weekdays": [5, 6]}
        elif token in {"week", "weekly"}:
            recurrence = {"freq": "weekly", "weekdays": [now.weekday()]}
        elif token in {"month", "monthly"}:
            recurrence = {"freq": "monthly", "day": now.day}
            ordinal = re.search(r"\b(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\s+(?:day\s+)?(?:of\s+)?(?:(?:every|each)\s+month)?",
                                value, re.I)
            if ordinal and 1 <= int(ordinal.group(1)) <= 31:
                recurrence["day"] = int(ordinal.group(1))
                _mark(spans, ordinal)
        elif token in {"year", "yearly", "annually"}:
            recurrence = {"freq": "yearly", "month": now.month, "day": now.day}
        else:
            days = sorted({weekday_index(item) for item in re.findall(_WEEKDAY_RE, whole, re.I)})
            recurrence = {"freq": "weekly", "weekdays": days or [now.weekday()]}

    date: datetime | None = None
    explicit_today = False
    match = re.search(r"\bday\s+after\s+tomorrow\b|\bparso[n]?\b", value, re.I)
    if match:
        date, _ = now + timedelta(days=2), _mark(spans, match)
    elif re.search(r"\btomorrow\b|\bkal\b", value, re.I):
        match = re.search(r"\btomorrow\b|\bkal\b", value, re.I)   # "kal" in a reminder is tomorrow
        date, _ = now + timedelta(days=1), _mark(spans, match)
    elif re.search(r"\b(?:today|tonight|aaj)\b", value, re.I):
        match = re.search(r"\btoday\b|\baaj\b", value, re.I)
        if match:
            _mark(spans, match)
        date, explicit_today = now, True
    iso = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", value)
    day_month = re.search(rf"\b(?:on\s+)?(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_RE}\b(?:,?\s+(20\d{{2}}))?", value, re.I)
    month_day = re.search(rf"\b(?:on\s+)?{_MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(20\d{{2}}))?", value, re.I)
    weekday = None if recurrence else re.search(rf"\b(?:on\s+|this\s+|next\s+|coming\s+)?{_WEEKDAY_RE}\b", value, re.I)
    try:
        if iso:
            date = now.replace(year=int(iso.group(1)), month=int(iso.group(2)), day=int(iso.group(3)))
            _mark(spans, iso)
        elif day_month or month_day:
            found = day_month or month_day
            day = int(found.group(1) if day_month else found.group(2))
            month = month_index(found.group(2) if day_month else found.group(1))
            year = int(found.group(3)) if found.group(3) else now.year
            date = now.replace(year=year, month=month, day=day)
            if not found.group(3) and date.date() < now.date():
                date = date.replace(year=year + 1)
            _mark(spans, found)
            if recurrence and recurrence["freq"] == "yearly":
                recurrence.update(month=month, day=day)
            elif recurrence and recurrence["freq"] == "monthly":
                recurrence["day"] = day
        elif weekday:
            target = weekday_index(weekday.group(1))
            ahead = (target - now.weekday()) % 7
            if ahead == 0 or weekday.group(0).casefold().startswith("next"):
                ahead = ahead or 7
            date = now + timedelta(days=ahead)
            _mark(spans, weekday)
    except ValueError:
        date = None

    clock = _clock(value, spans)
    part = None if clock else _part_of_day(value, spans)
    hour, minute = clock if clock else ((part[1], 0) if part else (None, None))
    if recurrence and recurrence.get("part") and hour is None:
        hour, minute = PART_OF_DAY[recurrence["part"]], 0
    result.has_time = clock is not None or part is not None or bool(recurrence and recurrence.get("part"))
    result.has_date = date is not None or recurrence is not None
    if hour is None:
        if part is None and (date is not None or recurrence is not None) and not explicit_today:
            hour, minute = DEFAULT_HOUR, 0
            result.assumed = "9 AM"
        elif explicit_today and re.search(r"\btonight\b", value, re.I):
            hour, minute = PART_OF_DAY["tonight"], 0
            result.has_time = True
    if part and not clock:
        result.assumed = {9: "9 AM", 14: "2 PM", 18: "6 PM", 21: "9 PM"}[part[1]]

    if recurrence is not None:
        if hour is None:
            return result
        recurrence.update(hour=hour, minute=minute)
        result.recurrence = recurrence
        start = date if date is not None else now
        result.due = next_occurrence(recurrence, (start if date is not None else now) - timedelta(seconds=1), now.tzinfo)
        if result.due <= now:
            result.due = next_occurrence(recurrence, now, now.tzinfo)
        return result
    if hour is None:
        return result
    base = date if date is not None else now
    due = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= now:
        if date is None:
            due += timedelta(days=1)          # "at 6 pm" after 6 pm means tomorrow
        elif explicit_today:
            result.passed = True
    result.due = due
    return result


def next_occurrence(rule: dict, after: datetime, tz) -> datetime:
    """The first occurrence strictly after `after` on the local wall clock of `tz`."""
    local = after.astimezone(tz)
    hour, minute = int(rule.get("hour", DEFAULT_HOUR)), int(rule.get("minute", 0))
    freq = rule.get("freq")
    for offset in range(0, 800):
        day = (local + timedelta(days=offset)).date()
        if freq == "weekly" and day.weekday() not in set(rule.get("weekdays") or [local.weekday()]):
            continue
        if freq == "monthly" and day.day != int(rule.get("day", local.day)):
            continue
        if freq == "yearly" and (day.month, day.day) != (int(rule.get("month", local.month)), int(rule.get("day", local.day))):
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if candidate > after:
            return candidate
    raise ValueError("No next occurrence within two years.")


_TRIGGER = re.compile(
    r"(?i)\b(?:(?:please|pls|kindly)\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:remind\s+me|set\s+(?:up\s+)?(?:a|an|the|my)\s+reminder|(?:add|create|make|schedule)\s+(?:a|an|the)\s+reminder)\b"
)


# "bol dena", "call kerke batana", "yaad dila dena", "pe/ko" (marker 220): request words, not what to remind about
_TRIGGER_HI = re.compile(
    r"(?i)(?:\b(?:mujhe|muje)\s+)?(?:\b(?:pe|par|ko)\s+)?(?:\b(?:call|phone|ring)\s+(?:kar\s*ke|karke|kerke|krke|kar\s*kar)\s+)?"
    r"\b(?:bol\s+(?:dena|do|dijiye|dena\s+mujhe)|bata\s+(?:dena|do|dijiye)|batana|bolna|yaad\s+(?:dila\s*(?:dena|do|na|iyo|dijiye)|dilana)|"
    r"uth(?:a|aa)\s*(?:dena|do)|uthana|jaga\s*(?:dena|do)|jagana|"
    r"(?:call|phone|ring)\s+(?:karna|karo|kar\s*do|kar\s*dena|kar\s*ke|karke|kerke|krke|kariye|kijiye))\b(?:\s+(?:ki|ke|that|ki\s+ki))?"
)


def reminder_title(text: str, parsed: ParsedWhen) -> str:
    """What to remind about, with the trigger and schedule phrases removed ("call Dad")."""
    value = " ".join(str(text or "").split())
    chars = list(value)
    for start, end in parsed.spans:
        for index in range(start, min(end, len(chars))):
            chars[index] = " "
    remaining = "".join(chars)
    remaining = _TRIGGER.sub(" ", remaining)
    remaining = _TRIGGER_HI.sub(" ", remaining)
    remaining = re.sub(r"(?i)^\W*(?:hey\s+)?jupiter\W*", " ", remaining)
    remaining = re.sub(r"(?i)\b(?:at|on|in|by|for|from|this|next|every)\s*(?=[,.!?]|$)", " ", remaining)
    remaining = " ".join(remaining.split()).strip(" ,.!?;:-")
    marker = re.match(r"(?i)^(?:(?:to|about|that|of|regarding|for)\s+){1,2}(.+)$", remaining)
    if marker:
        remaining = marker.group(1)
    else:
        inner = re.search(r"(?i)\b(?:to|about|that|regarding)\s+(.+)$", remaining)
        if inner and inner.start() <= 3:
            remaining = inner.group(1)
    remaining = re.sub(r"(?i)^(?:please\s+)", "", remaining).strip(" ,.!?;:-")
    remaining = re.sub(r"(?i)\s+(?:please|pls)$", "", remaining)
    if re.fullmatch(r"(?i)(?:it|this|that|something|me|a\s+reminder|reminder)?", remaining):
        # "kal subah 7 baje uthana" / "jaga dena": the reminder is to wake up (marker 220)
        return "wake up" if re.search(r"(?i)\b(?:uth(?:a|aa)\s*(?:dena|do)|uthana|jaga\s*(?:dena|do)|jagana)\b", value) else ""
    return remaining[:300]


_OBJECT_POSITION = frozenset({
    "tell", "told", "telling", "remind", "reminded", "ask", "asked", "give", "gave", "show", "showed", "help",
    "helped", "thank", "thanked", "call", "called", "message", "text", "let", "with", "to", "for", "about", "at",
    "from", "by", "of", "than", "trust", "need", "want", "love", "like", "see", "meet", "met", "send", "sent",
})


def second_person(text: str) -> str:
    """Restate the user's words in Jupiter's voice: 'submit my assignment' -> 'submit your assignment', and
    'whenever I tell you' -> 'whenever you tell me' (a simultaneous two-way swap, never 'you tell you')."""
    swaps = {"my": "your", "mine": "yours", "myself": "yourself", "me": "you", "i": "you", "i'm": "you're",
             "im": "you're", "i've": "you've", "i'll": "you'll", "your": "my", "yours": "mine", "yourself": "myself",
             "you're": "I'm", "you’re": "I'm", "you've": "I've", "you'll": "I'll"}
    pattern = re.compile(r"(?i)\b(?:my|mine|myself|me|i'm|im|i've|i'll|i|am|are|your|yours|yourself|you're|you’re|"
                         r"you've|you'll|you)\b")
    previous: list[str] = []

    def swap(match: re.Match) -> str:
        word, lower = match.group(0), match.group(0).casefold()
        before = text[:match.start()].casefold().split()
        prior = before[-1].strip(",.;:!?\"'") if before else ""
        last_swapped = previous[-1] if previous and previous[-1][1] == match.start() else None
        if lower == "you":
            replacement = "me" if prior in _OBJECT_POSITION else "I"
        elif lower == "are":
            replacement = "am" if last_swapped and last_swapped[0] == "you" else word
        elif lower == "am":
            replacement = "are" if last_swapped and last_swapped[0] == "i" else word
        else:
            replacement = swaps.get(lower, word)
        previous.append((lower, match.end() + 1))
        if replacement == "I" or replacement.startswith("I'"):
            return replacement
        return replacement.capitalize() if word[:1].isupper() and match.start() == 0 else replacement
    return pattern.sub(swap, text)


def describe_when(due_local: datetime, now_local: datetime, recurrence: dict | None = None) -> str:
    """'tomorrow at 8 AM', 'today at 6:30 PM', 'on Wed, 15 Oct at 9 AM', 'every Sunday at 7 PM'."""
    clock = due_local.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    if recurrence:
        freq = recurrence.get("freq")
        if freq == "daily":
            return f"every day at {clock}"
        if freq == "weekly":
            days = sorted(recurrence.get("weekdays") or [])
            if days == [0, 1, 2, 3, 4]:
                return f"every weekday at {clock}"
            if days == [5, 6]:
                return f"every weekend at {clock}"
            names = [WEEKDAYS[index].capitalize() for index in days]
            joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            return f"every {joined} at {clock}"
        if freq == "monthly":
            return f"on day {recurrence.get('day')} of every month at {clock}"
        if freq == "yearly":
            return f"every year on {due_local.day} {due_local.strftime('%B')} at {clock}"
    days = (due_local.date() - now_local.date()).days
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if 1 < days < 7:
        return f"on {due_local.strftime('%A')} at {clock}"
    year = f" {due_local.year}" if due_local.year != now_local.year else ""
    return f"on {due_local.day} {due_local.strftime('%B')}{year} at {clock}"


class ReminderService:
    """Explicit reminders as 'reminder' events in the existing per-user scheduler; ownership on every query."""

    def __init__(self, engine=None):
        from services.initiative_engine import InitiativeEngine
        self.engine = engine or InitiativeEngine()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        return self.engine._connect()

    def _migrate(self) -> None:
        with closing(self._connect()) as connection, connection:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(initiative_events)")}
            for column in ("recurrence_json", "timezone", "series_id", "title"):
                if column not in columns:
                    try:
                        connection.execute(f"ALTER TABLE initiative_events ADD COLUMN {column} TEXT")
                    except sqlite3.OperationalError as error:
                        if "duplicate column" not in str(error).lower():
                            raise
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_initiative_series_due "
                "ON initiative_events(series_id, due_at) WHERE series_id IS NOT NULL"
            )

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    def effective_timezone(self, user_id: str, client_timezone: str | None = None) -> str:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT timezone FROM proactive_settings WHERE user_id=?",
                                     (self.engine.normalize_user(user_id),)).fetchone()
        return (valid_zone(row[0]) if row else None) or valid_zone(client_timezone) or DEFAULT_TIMEZONE

    def create(self, user_id: str, title: str, due: datetime, tz_name: str, *, recurrence: dict | None = None,
               source_chat_id: str | None = None, source_message_id: str | None = None,
               call: bool = False) -> dict[str, Any]:
        user = self.engine.normalize_user(user_id)
        title = " ".join(str(title or "").split())[:300]
        if not user or not title:
            raise ValueError("A signed-in user and a reminder are required.")
        now = datetime.now(timezone.utc)
        if due <= now:
            raise ValueError("That time has already passed.")
        event_id, stamp = uuid4().hex, self._iso(now)
        series_id = uuid4().hex if recurrence else None
        payload = {"summary": f"Reminder: {title}", "title": title, "explicit": True}
        if call:
            payload["call"] = True                 # the owner asked for a Jupiter in-app call when it is due (marker 172)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO initiative_events (
                    event_id, user_id, event_type, due_at, expires_at, priority, interruption_policy, payload_json,
                    source_chat_id, source_message_id, created_at, updated_at, recurrence_json, timezone, series_id, title
                ) VALUES (?, ?, 'reminder', ?, ?, 'important', 'explicit_reminder', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                # A one-off reminder missed while every device was off is still shown late (up to a week); a series
                # occurrence rolls forward instead (roll_forward_missed).
                (event_id, user, self._iso(due), self._iso(due + timedelta(days=2 if recurrence else 7)),
                 json.dumps(payload, ensure_ascii=False), source_chat_id, source_message_id, stamp, stamp,
                 json.dumps(recurrence) if recurrence else None, tz_name, series_id, title),
            )
            if not cursor.rowcount:
                raise ValueError("That reminder was already created for this message.")
        return self.get(user, event_id)

    def _row(self, row) -> dict[str, Any]:
        item = dict(row)
        item["recurrence"] = json.loads(item.pop("recurrence_json") or "null")
        item.pop("payload_json", None)
        return item

    def get(self, user_id: str, event_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM initiative_events WHERE event_id=? AND user_id=? AND event_type='reminder'",
                (event_id, self.engine.normalize_user(user_id)),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, user_id: str, *, include_notified: bool = False) -> list[dict[str, Any]]:
        statuses = ("pending", "notified") if include_notified else ("pending",)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM initiative_events WHERE user_id=? AND event_type='reminder'
                    AND status IN ({','.join('?' * len(statuses))}) ORDER BY COALESCE(snoozed_until, due_at) LIMIT 100""",
                (self.engine.normalize_user(user_id), *statuses),
            ).fetchall()
        return [self._row(row) for row in rows]

    def recent(self, user_id: str, *, hours: int = 48, limit: int = 20) -> list[dict[str, Any]]:
        """Marker 220: reminders that came due recently, with what happened (delivered / missed) and, for a reminder
        call, the call's outcome, so the owner can always check whether a reminder really rang."""
        now = datetime.now(timezone.utc)
        since = self._iso(now - timedelta(hours=max(1, hours)))
        with closing(self._connect()) as connection:
            # 'notified' = shown and not yet acknowledged: it came due and was delivered
            rows = connection.execute(
                """SELECT * FROM initiative_events WHERE user_id=? AND event_type='reminder'
                    AND status IN ('notified','delivered','completed','dismissed','expired') AND due_at>=? AND due_at<=?
                    ORDER BY due_at DESC LIMIT ?""",
                (self.engine.normalize_user(user_id), since, self._iso(now), max(1, min(limit, 50))),
            ).fetchall()
            items = []
            for row in rows:
                item = self._row(row)
                call = None
                try:
                    found = connection.execute(
                        "SELECT status, end_reason, answered_at FROM call_invitations WHERE source_kind='reminder' "
                        "AND source_id=? ORDER BY created_at DESC LIMIT 1", (item["event_id"],)).fetchone()
                    if found:
                        call = {"status": found[0], "end_reason": found[1], "answered": bool(found[2])}
                except sqlite3.Error:
                    call = None                       # no call table yet on this install
                item["call"] = call
                items.append(item)
        return items

    def find(self, user_id: str, words: str) -> list[dict[str, Any]]:
        """Pending reminders whose title shares the meaningful words of `words`."""
        wanted = {word for word in re.findall(r"[\w'-]{3,}", words.casefold())
                  if word not in {"the", "my", "about", "for", "and", "reminder", "reminders", "that", "this", "with"}}
        items = self.list(user_id)
        if not wanted:
            return items
        scored = []
        for item in items:
            title_words = set(re.findall(r"[\w'-]{3,}", str(item.get("title") or "").casefold()))
            overlap = len(wanted & title_words) + sum(
                1 for word in wanted for title_word in title_words if word != title_word and (word.startswith(title_word[:5]) or title_word.startswith(word[:5]))
            )
            if overlap:
                scored.append((overlap, item))
        top = max((score for score, _ in scored), default=0)
        return [item for score, item in scored if score == top]

    def cancel(self, user_id: str, event_id: str) -> bool:
        user = self.engine.normalize_user(user_id)
        stamp = self._iso(datetime.now(timezone.utc))
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT series_id FROM initiative_events WHERE event_id=? AND user_id=? "
                                     "AND event_type='reminder'", (event_id, user)).fetchone()
            if not row:
                return False
            if row[0]:
                cursor = connection.execute("UPDATE initiative_events SET status='cancelled', updated_at=? WHERE series_id=? "
                                            "AND user_id=? AND status IN ('pending','notified')", (stamp, row[0], user))
            else:
                cursor = connection.execute("UPDATE initiative_events SET status='cancelled', updated_at=? WHERE event_id=? "
                                            "AND user_id=? AND status IN ('pending','notified')", (stamp, event_id, user))
        return bool(cursor.rowcount)

    def reschedule(self, user_id: str, event_id: str, due: datetime, *, title: str | None = None,
                   recurrence: dict | None | bool = False) -> dict[str, Any] | None:
        user = self.engine.normalize_user(user_id)
        if due <= datetime.now(timezone.utc):
            raise ValueError("That time has already passed.")
        updates = ["due_at=?", "expires_at=?", "snoozed_until=NULL", "status='pending'", "updated_at=?"]
        values: list[Any] = [self._iso(due), self._iso(due + timedelta(days=7)), self._iso(datetime.now(timezone.utc))]
        if title:
            clean = " ".join(title.split())[:300]
            updates.append("title=?")
            values.append(clean)
            updates.append("payload_json=?")
            values.append(json.dumps({"summary": f"Reminder: {clean}", "title": clean, "explicit": True}, ensure_ascii=False))
        if recurrence is not False:
            updates.append("recurrence_json=?")
            values.append(json.dumps(recurrence) if recurrence else None)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"UPDATE initiative_events SET {', '.join(updates)} WHERE event_id=? AND user_id=? "
                "AND event_type='reminder' AND status IN ('pending','notified')",
                (*values, event_id, user),
            )
        return self.get(user, event_id) if cursor.rowcount else None

    def snooze(self, user_id: str, event_id: str, minutes: int) -> dict[str, Any] | None:
        if not self.engine.act_on_event(user_id, event_id, "snooze", minutes):
            return None
        return self.get(user_id, event_id)

    def schedule_next(self, user_id: str, event: dict[str, Any], *, after: datetime | None = None) -> dict[str, Any] | None:
        """Create the next occurrence of a series (idempotent per series + due time)."""
        rule = event.get("recurrence")
        if isinstance(rule, str):
            rule = json.loads(rule or "null")
        if not rule or not event.get("series_id"):
            return None
        tz = zone(event.get("timezone"))
        previous = datetime.fromisoformat(str(event["due_at"]).replace("Z", "+00:00"))
        base = max(previous, after or previous)
        due = next_occurrence(rule, base, tz)
        user = self.engine.normalize_user(user_id)
        stamp = self._iso(datetime.now(timezone.utc))
        title = str(event.get("title") or "")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT OR IGNORE INTO initiative_events (
                    event_id, user_id, event_type, due_at, expires_at, priority, interruption_policy, payload_json,
                    source_chat_id, source_message_id, created_at, updated_at, recurrence_json, timezone, series_id, title
                ) VALUES (?, ?, 'reminder', ?, ?, 'important', 'explicit_reminder', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uuid4().hex, user, self._iso(due), self._iso(due + timedelta(days=2)),
                 json.dumps({"summary": f"Reminder: {title}", "title": title, "explicit": True}, ensure_ascii=False),
                 event.get("source_chat_id"), f"{event['series_id']}:{self._iso(due)}", stamp, stamp,
                 json.dumps(rule), event.get("timezone"), event["series_id"], title),
            )
            row = connection.execute("SELECT * FROM initiative_events WHERE series_id=? AND due_at=?",
                                     (event["series_id"], self._iso(due))).fetchone()
        return self._row(row) if row else None

    def roll_forward_missed(self, user_id: str, now: datetime | None = None) -> int:
        """A series occurrence missed for over six hours moves to its next time instead of firing stale or expiring."""
        now = now or datetime.now(timezone.utc)
        user = self.engine.normalize_user(user_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM initiative_events WHERE user_id=? AND event_type='reminder' AND status='pending' "
                "AND series_id IS NOT NULL AND due_at<?", (user, self._iso(now - timedelta(hours=6))),
            ).fetchall()
        moved = 0
        for row in rows:
            item = self._row(row)
            tz = zone(item.get("timezone"))
            due = next_occurrence(item["recurrence"], now, tz)
            with closing(self._connect()) as connection, connection:
                exists = connection.execute("SELECT 1 FROM initiative_events WHERE series_id=? AND due_at=?",
                                            (item["series_id"], self._iso(due))).fetchone()
                if exists:
                    connection.execute("UPDATE initiative_events SET status='expired', updated_at=? WHERE event_id=?",
                                       (self._iso(now), item["event_id"]))
                else:
                    connection.execute("UPDATE initiative_events SET due_at=?, expires_at=?, updated_at=? WHERE event_id=? "
                                       "AND status='pending'", (self._iso(due), self._iso(due + timedelta(days=2)),
                                                                self._iso(now), item["event_id"]))
            moved += 1
        return moved
