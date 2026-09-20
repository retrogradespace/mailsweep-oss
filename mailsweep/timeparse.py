"""Normalize event times to what Calendar creation and review expect.

Canonical forms, always local wall-clock time with no timezone suffix:
  "YYYY-MM-DD"        date only -- no clock time was stated (an all-day event)
  "YYYY-MM-DDTHH:MM"  timed event
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

_ISO = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[T\s]+(.*))?$")
_US = re.compile(r"^\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?:[T\s]+(.*))?$")
# 14:30 | 14:30:00 | 1430 | 2:30pm | 11:45A | 8pm | 10:30:00+02:00 | 12:00:00.000Z
_TIME = re.compile(
    r"^(\d{1,2})(?::?(\d{2}))?(?::?\d{2})?(?:\.\d+)?\s*([ap])?\.?(?:m\.?)?\s*"
    r"(Z|[+-]\d{2}:?\d{2})?\s*$", re.I)


def _clock(text: str) -> tuple[int, int, int | None] | None:
    """(hour, minute, utc_offset_minutes) from a clock string, or None if there
    isn't a usable one. The offset is None when the string carries none."""
    m = _TIME.match((text or "").strip())
    if not m:
        return None
    hour, minute, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ap == "p" else 0)
    elif m.group(2) is None:
        return None                       # a bare "12" with no am/pm isn't a time
    if hour > 23 or minute > 59:
        return None
    tz = m.group(4)
    off = None
    if tz:
        if tz.upper() == "Z":
            off = 0
        else:
            digits = tz[1:].replace(":", "")
            off = (int(digits[:2]) * 60 + int(digits[2:])) * (-1 if tz[0] == "-" else 1)
    return hour, minute, off


def _build(y: int, mo: int, d: int, time_text: str) -> tuple[str, bool] | None:
    try:
        day = date(y, mo, d)
    except ValueError:
        return None
    clock = _clock(time_text)
    # Midnight almost always means "no time was given", not a real 12:00 AM start.
    if clock is None or clock[:2] == (0, 0):
        return day.isoformat(), True
    hour, minute, off = clock
    if off:
        # A real zone (say -07:00 for a Pacific webinar): convert to the recipient's
        # local time. UTC ("Z", "+00:00") is deliberately NOT converted -- the local
        # model tacks it onto plain wall-clock times far more often than it is
        # reporting a real UTC time (checked against the source emails), and
        # converting those would shift events by the UTC offset.
        local = datetime(y, mo, d, hour, minute, tzinfo=timezone(timedelta(minutes=off))).astimezone()
        return local.strftime("%Y-%m-%dT%H:%M"), False
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}", False


def normalize_start(raw: str) -> tuple[str, bool] | None:
    """Canonical (start, date_only) for a model-extracted start string, or
    None if it has no usable date."""
    m = _ISO.match(raw or "")
    if not m:
        return None
    return _build(int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")


def normalize_end(start: str, raw_end: str) -> str:
    """Canonical end for an already-normalized `start`, or "" when the end is
    missing, unreadable, not after the start, or lacks a time for a timed
    event (Calendar then defaults to a one-hour event)."""
    parsed = normalize_start(raw_end) if raw_end else None
    if not parsed:
        return ""
    end, end_date_only = parsed
    if len(start) == 10:                  # all-day: only a later date means a multi-day event
        return end[:10] if end[:10] > start else ""
    if end_date_only:
        return ""
    return end if end > start else ""


def parse_user_datetime(text: str, today: date | None = None) -> tuple[str, bool] | None:
    """Parse a hand-typed date/time for a correction: "2026-09-20",
    "2026-09-20 14:30", "9/20 2:30pm", "9/20/2026". Returns (start, date_only)."""
    today = today or date.today()
    m = _ISO.match(text or "")
    if m:
        return _build(int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")
    m = _US.match(text or "")
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        return _build(year, int(m.group(1)), int(m.group(2)), m.group(4) or "")
    return None
