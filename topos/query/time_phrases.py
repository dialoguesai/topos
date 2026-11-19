"""Plain time phrases beyond the planner's table of named periods.

The planner's `_RELATIVE_RANGES` holds six named periods (today, yesterday, this/last
week, this/last month) plus the counted "last/past N days|hours" forms. Measured
19 Nov 2026, everything else an owner ordinarily says produced no window at all, so the
ask ran undated: last night, this morning, two days ago, on Monday, last Friday, last
weekend, a week ago, this year, last year, in October, the last 3 months.
"The day before yesterday" was read as BEFORE + yesterday: 1970 through two days ago.

This module reads those. It is deliberately a reader of phrases and nothing else: the
planner still owns precedence, boundary words (before/after/since), union spans and
comparison windows, and calls in here first because every phrase below is more specific
than the table entry it may contain ("the day before yesterday" holds "yesterday").

A bare day of the month ("on the 5th") is still not read: the planner's rule is that a
day number with no month is ambiguous, and `test_bare_day_number_yields_no_range` holds it.

A period that has not started yet is not read either (see `find_all`).

All arithmetic is on the caller's wall clock (`now`, offset-aware), the same rule the
table follows, so a named zone keeps its own offsets across a DST change.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

Window = Tuple[str, str]


@dataclass(frozen=True)
class PhraseMatch:
    """One phrase found in the text, with the window it names.

    `start()` is where the PERIOD begins, after any governing word, so the planner's
    boundary check ("since Monday", "before last Friday") still sees that word as the
    thing immediately in front of the period.
    """

    begin: int
    stop: int
    window: Window

    def start(self) -> int:
        return self.begin

    def end(self) -> int:
        return self.stop


_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a couple": 2, "a couple of": 2, "couple": 2, "couple of": 2,
}
_COUNT = (
    r"(?P<n>\d{1,3}|a couple(?: of)?|couple(?: of)?|a few|few|an?|one|two|three|four|five|six|"
    r"seven|eight|nine|ten|eleven|twelve)"
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY_PART = r"(?:morning|afternoon|evening|night)"
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "apr": 4, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11,
    "dec": 12,
}
#: Names that are also ordinary words. They are a month only behind a preposition of time:
#: "in May", never "this may be" or "should we march".
_AMBIGUOUS_MONTHS = frozenset({"may", "march"})
#: "Black Friday", "Taco Tuesday": a weekday that is part of a name, not a day.
_WEEKDAY_NAME_PREFIXES = frozenset(
    {"black", "cyber", "taco", "good", "palm", "ash", "easter", "super", "fat", "giving", "bowl"}
)
#: "every Monday", "on Mondays": a habit, which no single window answers.
_HABIT_PREFIXES = frozenset({"every", "each", "most", "all"})


def _count(raw: str) -> Optional[int]:
    raw = " ".join(raw.lower().split())
    if raw.isdigit():
        return int(raw)
    return _NUMBER_WORDS.get(raw)


def _day_start(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _day_end(moment: datetime) -> datetime:
    return moment.replace(hour=23, minute=59, second=59, microsecond=0)


def _days(start: datetime, end: Optional[datetime] = None) -> Window:
    return _day_start(start).isoformat(), _day_end(end or start).isoformat()


def _on_date(now: datetime, year: int, month: int, day: int) -> datetime:
    return now.replace(year=year, month=month, day=day)


def _shift_months(moment: datetime, months_back: int) -> datetime:
    index = moment.year * 12 + (moment.month - 1) - months_back
    year, month = divmod(index, 12)
    month += 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _calendar_month(now: datetime, year: int, month: int) -> Window:
    last = calendar.monthrange(year, month)[1]
    return _days(_on_date(now, year, month, 1), _on_date(now, year, month, last))


def _part_of_day(day: datetime, part: str) -> Window:
    part = part.lower()
    if part == "morning":
        return _day_start(day).isoformat(), day.replace(hour=11, minute=59, second=59, microsecond=0).isoformat()
    if part == "afternoon":
        return (
            day.replace(hour=12, minute=0, second=0, microsecond=0).isoformat(),
            day.replace(hour=17, minute=59, second=59, microsecond=0).isoformat(),
        )
    return day.replace(hour=17, minute=0, second=0, microsecond=0).isoformat(), _day_end(day).isoformat()


# --- resolvers: (match, now, text) -> (period start index, window) or None --------------

_Resolved = Optional[Tuple[int, Window]]


def _day_before_yesterday(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    return match.start(), _days(now - timedelta(days=2))


def _last_night(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    yesterday = now - timedelta(days=1)
    return match.start(), (
        yesterday.replace(hour=17, minute=0, second=0, microsecond=0).isoformat(),
        now.replace(hour=4, minute=59, second=59, microsecond=0).isoformat(),
    )


def _today_part(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    part = match.group("part") if "part" in match.groupdict() and match.group("part") else "evening"
    return match.start(), _part_of_day(now, part)


def _yesterday_part(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    return match.start(), _part_of_day(now - timedelta(days=1), match.group("part"))


def _days_ago(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    raw = " ".join(match.group("n").lower().split())
    if raw in ("a few", "few"):
        return match.start(), _days(now - timedelta(days=4), now - timedelta(days=2))
    count = _count(raw)
    if not count:
        return None
    return match.start(), _days(now - timedelta(days=count))


def _weeks_ago(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    raw = " ".join(match.group("n").lower().split())
    count = 3 if raw in ("a few", "few") else _count(raw)
    if not count:
        return None
    monday = now - timedelta(days=now.weekday() + 7 * count)
    return match.start(), _days(monday, monday + timedelta(days=6))


def _months_ago(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    raw = " ".join(match.group("n").lower().split())
    count = 3 if raw in ("a few", "few") else _count(raw)
    if not count:
        return None
    then = _shift_months(now.replace(day=1), count)
    return match.start(), _calendar_month(now, then.year, then.month)


def _years_ago(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    count = _count(match.group("n"))
    if not count:
        return None
    year = now.year - count
    return match.start(), _days(_on_date(now, year, 1, 1), _on_date(now, year, 12, 31))


def _weekend(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    modifier = (match.group("mod") or "").lower()
    saturday = now + timedelta(days=5 - now.weekday())
    if modifier in ("last", "previous"):
        saturday -= timedelta(days=7)
    elif modifier != "this" and now.weekday() < 5:
        # "over the weekend", said on a weekday: the one that has happened.
        saturday -= timedelta(days=7)
    begin = match.start("mod") if match.group("mod") else match.start("word")
    return begin, _days(saturday, saturday + timedelta(days=1))


def _weekday(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    governed = bool(match.group("gov") or match.group("mod") or match.group("part"))
    before = text[: match.start("day")].rstrip().rsplit(None, 1)
    previous_word = before[-1].lower().strip(".,;:!?\"'") if before else ""
    if previous_word in _HABIT_PREFIXES or match.group("plural"):
        return None
    if not governed:
        following = text[match.end("day"): match.end("day") + 2]
        if previous_word in _WEEKDAY_NAME_PREFIXES or re.match(r"\.\w", following):
            return None
    elif previous_word in _WEEKDAY_NAME_PREFIXES:
        return None
    target = _WEEKDAYS.index(match.group("day").lower())
    modifier = " ".join((match.group("mod") or "").lower().split())
    back = (now.weekday() - target) % 7
    if modifier == "this":
        day = now + timedelta(days=target - now.weekday())
    else:
        day = now - timedelta(days=back or 7)
    begin = match.start("mod") if match.group("mod") else match.start("day")
    return begin, _days(day)


def _this_year(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    return match.start(), _days(_on_date(now, now.year, 1, 1), now)


def _last_year(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    year = now.year - 1
    return match.start(), _days(_on_date(now, year, 1, 1), _on_date(now, year, 12, 31))


def _quarter(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    first_month = 3 * ((now.month - 1) // 3) + 1
    if match.group("mod").lower() == "this":
        return match.start(), _days(_on_date(now, now.year, first_month, 1), now)
    start = _shift_months(now.replace(day=1, month=first_month), 3)
    end = _shift_months(now.replace(day=1, month=first_month), 1)
    return match.start(), (
        _calendar_month(now, start.year, start.month)[0],
        _calendar_month(now, end.year, end.month)[1],
    )


def _counted_months(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    count = int(match.group("n")) * (12 if match.group("unit").lower().startswith("year") else 1)
    if not count:
        return None
    then = _shift_months(now, count)
    if match.group("mod").lower() == "past":
        return match.start(), (then.isoformat(), now.isoformat())
    return match.start(), _days(then, now)


def _past_year(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    return match.start(), (_shift_months(now, 12).isoformat(), now.isoformat())


def _month_name(match: "re.Match[str]", now: datetime, text: str) -> _Resolved:
    name = match.group("month").lower()
    governing = " ".join((match.group("gov") or "").lower().split())
    if name in _AMBIGUOUS_MONTHS and governing not in ("in", "during", "last", "since"):
        return None
    if not governing and not match.group("year"):
        return None
    month = _MONTHS[name]
    if match.group("year"):
        year = int(match.group("year"))
    elif governing == "last":
        year = now.year if month < now.month else now.year - 1
    else:
        # The most recent one that has started: "in December", asked in November, is
        # last year's.
        year = now.year if month <= now.month else now.year - 1
    begin = match.start("gov") if governing in ("last", "this") else match.start("month")
    return begin, _calendar_month(now, year, month)


_MONTH_ALTERNATION = "|".join(sorted(_MONTHS, key=len, reverse=True))

#: Most specific first. A phrase that contains another's words must come before it.
_PHRASES: Tuple[Tuple["re.Pattern[str]", Callable[..., _Resolved]], ...] = (
    (re.compile(r"\b(?:the )?day before yesterday\b", re.I), _day_before_yesterday),
    (re.compile(r"\blast night\b", re.I), _last_night),
    (re.compile(rf"\byesterday (?P<part>{_DAY_PART})\b", re.I), _yesterday_part),
    (re.compile(r"\bthis (?P<part>morning|afternoon|evening)\b", re.I), _today_part),
    (re.compile(r"\btonight\b", re.I), _today_part),
    (re.compile(rf"\b{_COUNT} days? ago\b", re.I), _days_ago),
    (re.compile(rf"\b{_COUNT} weeks? ago\b", re.I), _weeks_ago),
    (re.compile(rf"\b{_COUNT} months? ago\b", re.I), _months_ago),
    (re.compile(rf"\b{_COUNT} years? ago\b", re.I), _years_ago),
    (
        re.compile(
            r"\b(?:(?P<mod>this|last|previous)\s+|(?:over|on|during)\s+the\s+|the\s+)(?P<word>weekend)\b",
            re.I,
        ),
        _weekend,
    ),
    (
        re.compile(
            r"\b(?:(?P<gov>on|since|before|after|from|until|till|by)\s+)?"
            r"(?:(?P<mod>last|this\s+past|this|past|previous)\s+)?"
            rf"(?P<day>{'|'.join(_WEEKDAYS)})(?P<plural>s)?\b"
            rf"(?P<part>\s+{_DAY_PART}\b)?",
            re.I,
        ),
        _weekday,
    ),
    (re.compile(r"\bthis year\b", re.I), _this_year),
    (re.compile(r"\blast year\b", re.I), _last_year),
    (re.compile(r"\b(?P<mod>this|last) quarter\b", re.I), _quarter),
    (re.compile(r"\b(?P<mod>last|past) (?P<n>\d{1,3}) (?P<unit>months|years)\b", re.I), _counted_months),
    (re.compile(r"\bpast year\b", re.I), _past_year),
    (
        re.compile(
            r"\b(?:(?P<gov>in|during|last|this|since|from|for|of|throughout)\s+)?"
            rf"(?P<month>{_MONTH_ALTERNATION})\b"
            r"(?:\s+(?:of\s+)?(?P<year>20\d{2})\b)?"
            r"(?!\.?\s+\d{1,2}(?:st|nd|rd|th)?\b)",
            re.I,
        ),
        _month_name,
    ),
)


def _starts_after(window: Window, now: datetime) -> bool:
    try:
        start = datetime.fromisoformat(window[0])
    except ValueError:
        return False
    if (start.tzinfo is None) != (now.tzinfo is None):
        return False
    return start > now


def find_all(text: str, now: datetime) -> List[PhraseMatch]:
    """Every phrase in `text`, non-overlapping, in the order they were claimed.

    Claimed in `_PHRASES` order, so the more specific phrase wins the characters it
    shares with a less specific one.
    """
    taken: List[Tuple[int, int]] = []
    found: List[PhraseMatch] = []
    for pattern, resolve in _PHRASES:
        for match in pattern.finditer(text or ""):
            if any(match.start() < stop and begin < match.end() for begin, stop in taken):
                continue
            try:
                resolved = resolve(match, now, text)
            except ValueError:
                resolved = None
            if resolved is None:
                continue
            begin, window = resolved
            if _starts_after(window, now):
                # A period that has not begun ("tonight" at noon, "this weekend" on a
                # Thursday, "December 2027"). Those had no window before this module and
                # still have none: a future window is the calendar's business, and the
                # dated-recall path clamps a window's end to now, which would turn one
                # that starts later into a window holding nothing.
                continue
            taken.append((match.start(), match.end()))
            found.append(PhraseMatch(begin=begin, stop=match.end(), window=window))
    return found


def first(text: str, now: datetime) -> Optional[PhraseMatch]:
    """The phrase the planner should resolve when it wants one window."""
    found = find_all(text, now)
    return found[0] if found else None


def blank(text: str, matches: List[PhraseMatch]) -> str:
    """`text` with each phrase replaced by spaces, so positions in it do not move.

    Blanks from where the pattern matched is not known here, only where the period
    starts; the period is what must not be read twice.
    """
    chars = list(text or "")
    for match in matches:
        for index in range(match.begin, min(match.stop, len(chars))):
            chars[index] = " "
    return "".join(chars)


#: Any fixed instant: `strip` needs to know WHERE the phrases are, not which days they name.
_ANY_NOW = datetime(2024, 1, 15, 12, 0, 0)


def strip(text: str) -> str:
    """`text` without the time phrases read here: what is left is the subject of the ask."""
    return blank(text, find_all(text, _ANY_NOW))
