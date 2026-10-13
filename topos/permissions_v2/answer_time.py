"""A question's time scope, for answers mode (lane TR162, A2A-4 amendment 12).

On the owner's live node (13 Oct 2026) "What has <owner> been working on this week?" was answered from items one to
two months old: retrieval ranked the whole grant window by words alone, and neither the writer nor the judge knew
what day it was. This module is the closed, deterministic part of the fix:

- `question_scope` reads a pinned list of time expressions in the question (`EXPRESSIONS`, below) and turns them into
  a window relative to the job's clock. No expression, no scope: the behaviour before this lane. Several: their
  union. Every edge is a UTC midnight, or the grant window's own upper edge (now), so a scope is never finer than a
  day, and it is computed only when the grant releases event time at all: under `release_event_time` "none" the
  grant withholds times, so a scope (and every rule that reads one) is off. The records door admits a window only
  at the grant's precision (the control plane's `search_window_precision`: under "day" both edges on a UTC midnight,
  under "none" no window); this door is never finer than that, and under "second" it stays at whole days.
- `plan`: the windows retrieval tries, in order. An explicit expression (today, this week, last month, the past N
  days, coming up) keeps its exact window and, when that releases fewer than `SCOPE_FLOOR` items, is widened once
  (`widen`: three times its span in whole days, ending where it ended). The recent class (lately, recently, recent,
  these days, currently, now) climbs a ladder instead, `RECENT_RUNGS_DAYS` = 3, 7, 14, 30 days: the app's own-AI turn
  has climbed 3, 7, 14 since 28 Sep (topos-react-app `features/home/recencyLadder.ts`: two distinct records fill a
  rung, the last rung needs one, and the answer says which rung it used), and the node adds a fourth. Retrieval stops
  at the first rung that fills; the writer and the judge are told which; on the last rung a sentence must say when.
  The grant window still bounds every window (the caller intersects).
- `in_scope` and `dated_sentence`: the deterministic guard's two reads (`answer_generation.sieve_answer`).
- `time_lines`: what the writer and the judge are told about today and the scope, at the grant's precision.

Closed lists, pinned by test: adding a phrase is an amendment. Nothing here reads a record, a store or the clock.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass

from . import switches

DAY = 86_400
#: "lately", "recently", "recent", "these days", "currently", "now": the recency ladder. A rung is today and the N days
#: before it (the app's count: `recentCount` in recencyLadder.ts), tried narrowest first.
RECENT_RUNGS_DAYS = (3, 7, 14, 30)
#: Released items that fill a rung; the last rung needs only this many.
RUNG_ENOUGH = 2
LAST_RUNG_ENOUGH = 1
#: The widest rung that still reads as recent: the recent class's window when no rung was climbed (retrieval off, or
#: beside another expression), and what the guard holds a sentence to on the last rung.
RECENT_DAYS = RECENT_RUNGS_DAYS[-2]
#: A future-facing question ("coming up", "upcoming", "next week", "tomorrow", "plans") reads the items of the last
#: 14 days: plans are written before they happen, and a plan written two months ago is stale or done.
FUTURE_DAYS = 14
#: Fewer released items than this in the scope: widen once.
SCOPE_FLOOR = 2
#: The widened scope spans this many times the scope's whole days, ending where the scope ended.
WIDEN_FACTOR = 3
#: The longest "past N days" read; a larger N is read as this (the grant window bounds it in any case).
MAX_DAYS = 366
#: Precisions under which a scope exists. "none": the grant withholds event times, so no scope.
PRECISIONS = ("day", "second")
VERSION = "topos-answer-time/v1"

# The closed receipt words for a scope (`TimeScope.kind`).
KINDS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "past_days", "recent", "upcoming",
         "mixed")

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                 "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14, "thirty": 30, "couple": 2, "few": 3}
_UNITS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30}
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

#: The fixed expressions, as whole-token sequences of the case-folded question, and what each reads as. Longest
#: first within a start token, so "the last week" is read before "last week". "past/last N days|weeks|months" is
#: read by `_counted` (a number word or ASCII digits, "a couple of", "a few").
EXPRESSIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("today",), "today"), (("tonight",), "today"), (("this", "morning"), "today"),
    (("this", "afternoon"), "today"), (("this", "evening"), "today"),
    (("yesterday",), "yesterday"),
    (("the", "last", "week"), "this_week"), (("the", "past", "week"), "this_week"), (("past", "week"), "this_week"),
    (("this", "week"), "this_week"),
    (("last", "week"), "last_week"),
    (("the", "last", "month"), "past_month"), (("the", "past", "month"), "past_month"),
    (("past", "month"), "past_month"),
    (("this", "month"), "this_month"),
    (("last", "month"), "last_month"),
    (("lately",), "recent"), (("recently",), "recent"), (("recent",), "recent"), (("these", "days"), "recent"),
    (("currently",), "recent"), (("now",), "recent"),
    (("coming", "up"), "upcoming"), (("upcoming",), "upcoming"), (("next", "week"), "upcoming"),
    (("next", "month"), "upcoming"), (("tomorrow",), "upcoming"), (("soon",), "upcoming"),
    (("plans",), "upcoming"), (("plan",), "upcoming"), (("planning",), "upcoming"), (("planned",), "upcoming"),
)

#: The guard's closed list (D, widened answers): a sentence citing only items outside the question's scope stands
#: only when it says it is about an earlier time, by a month name, an ISO date, or one of these phrases.
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
EARLIER_PHRASES = ("earlier", "previously", "ago", "before that", "last month", "last week", "back in",
                   "in the past", "weeks before", "months before", "at the time", "back then")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


@dataclass(frozen=True)
class TimeScope:
    """The window a question asks about, in UTC seconds, half open: `lower <= t < upper`."""
    kind: str
    lower: int
    upper: int
    future: bool
    now: int
    #: The rung of the recency ladder retrieval filled (`RECENT_RUNGS_DAYS`), or 0: no ladder, or no rung filled.
    rung_days: int = 0

    def bounds_us(self, lower_us: int, upper_us: int) -> tuple[int, int]:
        """This scope inside the grant's window (microseconds, both edges inclusive), never wider than it."""
        return max(lower_us, self.lower * 1_000_000), min(upper_us, self.upper * 1_000_000 - 1)

    def contains(self, seconds: int) -> bool:
        return self.lower <= seconds < self.upper


def enabled(env=None) -> bool:
    """A: the question's time scope is read (`ANSWER_TIME_SCOPE`). Off: no scope, and B, D and C's scope line idle."""
    return switches.on(switches.ANSWER_TIME_SCOPE, env)


def retrieval_on(env=None) -> bool:
    """B: retrieval ranks and walks inside the scope, widening once (`ANSWER_TIME_RETRIEVAL`)."""
    return switches.on(switches.ANSWER_TIME_RETRIEVAL, env)


def prompt_on(env=None) -> bool:
    """C: the writer is told today's date and the scope (`ANSWER_TIME_PROMPT`)."""
    return switches.on(switches.ANSWER_TIME_PROMPT, env)


def guard_on(env=None) -> bool:
    """D: the deterministic out-of-scope guard (`ANSWER_TIME_GUARD`)."""
    return switches.on(switches.ANSWER_TIME_GUARD, env)


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(unicodedata.normalize("NFKC", text).casefold())


def _midnight(seconds: int) -> int:
    return seconds // DAY * DAY


def _month_start(seconds: int, back: int = 0) -> int:
    day = dt.datetime.fromtimestamp(seconds, dt.timezone.utc).date().replace(day=1)
    for _ in range(back):
        day = (day - dt.timedelta(days=1)).replace(day=1)
    return int(dt.datetime(day.year, day.month, 1, tzinfo=dt.timezone.utc).timestamp())


def _counted(tokens: list[str], start: int) -> tuple[int, int] | None:
    """"past|last [the] N days|weeks|months" at `start`: (days, tokens read), or None."""
    at = start
    if at < len(tokens) and tokens[at] == "the":
        at += 1
    if at >= len(tokens) or tokens[at] not in ("past", "last"):
        return None
    at += 1
    if at < len(tokens) and tokens[at] == "a":                 # "a couple of", "a few"
        at += 1
    if at >= len(tokens):
        return None
    word = tokens[at]
    if word.isascii() and word.isdigit():
        count = int(word)
    elif word in _NUMBER_WORDS:
        count = _NUMBER_WORDS[word]
    else:
        return None
    at += 1
    if at < len(tokens) and tokens[at] == "of":                # "a couple of weeks"
        at += 1
    if at >= len(tokens) or tokens[at] not in _UNITS or count < 1:
        return None
    return min(MAX_DAYS, count * _UNITS[tokens[at]]), at + 1 - start


def expressions(question: str) -> list[tuple[str, int]]:
    """The closed expressions the question carries, in order: (kind, days for "past_days" else 0)."""
    tokens = _tokens(question)
    found, at = [], 0
    while at < len(tokens):
        counted = _counted(tokens, at)
        if counted is not None:
            found.append(("past_days", counted[0]))
            at += counted[1]
            continue
        for phrase, kind in EXPRESSIONS:
            if tuple(tokens[at:at + len(phrase)]) == phrase:
                found.append((kind, 0))
                at += len(phrase)
                break
        else:
            at += 1
    return found


def _window(kind: str, days: int, now: int) -> tuple[int, int]:
    today = _midnight(now)
    end = now + 1
    if kind == "today":
        return today, end
    if kind == "yesterday":
        return today - DAY, today
    if kind == "this_week":
        return today - 6 * DAY, end
    if kind == "last_week":
        return today - 13 * DAY, today - 6 * DAY
    if kind == "past_month":
        return today - 29 * DAY, end
    if kind == "this_month":
        return _month_start(now), end
    if kind == "last_month":
        return _month_start(now, 1), _month_start(now)
    if kind == "past_days":
        return today - (days - 1) * DAY, end
    if kind == "recent":
        return today - RECENT_DAYS * DAY, end
    if kind == "upcoming":
        return today - (FUTURE_DAYS - 1) * DAY, end
    raise ValueError(kind)


def question_scope(question: str, *, now: int, precision: str) -> TimeScope | None:
    """The window the question asks about at `now` (UTC seconds), or None: no expression, or a grant that withholds
    event times (`precision` "none", or anything not in `PRECISIONS`). Several expressions: their union."""
    if precision not in PRECISIONS or not isinstance(question, str):
        return None
    found = expressions(question)
    if not found:
        return None
    windows = [_window(kind, days, now) for kind, days in found]
    kinds = {"past_days" if kind == "past_month" else kind for kind, _ in found}     # the receipt's closed words
    word = next(iter(kinds)) if len(kinds) == 1 else "mixed"
    return TimeScope(word, min(low for low, _ in windows), max(high for _, high in windows),
                     any(kind == "upcoming" for kind, _ in found), now)


def widen(scope: TimeScope) -> TimeScope:
    """The scope once widened: WIDEN_FACTOR times its span in whole days, ending where it ended. The lower edge stays
    on a UTC midnight."""
    span_days = max(1, -(-(scope.upper - scope.lower) // DAY))
    lower = _midnight(scope.lower) - (WIDEN_FACTOR - 1) * span_days * DAY
    return TimeScope(scope.kind, lower, scope.upper, scope.future, scope.now)


@dataclass(frozen=True)
class Step:
    """One window retrieval tries: `window` is ranked and walked; `enough` released items fill it; when it is the
    one used, the guard and the lines about time read `scope`, and `widened` says a sentence that rests only on items
    outside `scope` must place itself in time."""
    window: TimeScope
    enough: int
    scope: TimeScope
    widened: bool


def plan(scope: TimeScope) -> tuple[Step, ...]:
    """The windows retrieval tries for this scope, in order; it stops at the first that fills.

    The recent class alone climbs the ladder: each rung is today and the N days before it, the last rung is filled
    by one item, and on the last rung the scope a sentence is held to stays the widest rung that still reads as
    recent, so what is older must say when. Every other scope, a union with a recent word included, keeps its exact
    window and the single widening."""
    if scope.kind != "recent":
        return (Step(scope, SCOPE_FLOOR, scope, False), Step(widen(scope), SCOPE_FLOOR, scope, True))
    today = _midnight(scope.now)
    steps = []
    for number, days in enumerate(RECENT_RUNGS_DAYS):
        last = number == len(RECENT_RUNGS_DAYS) - 1
        window = TimeScope(scope.kind, today - days * DAY, scope.upper, scope.future, scope.now, days)
        held = TimeScope(scope.kind, today - RECENT_DAYS * DAY, scope.upper, scope.future, scope.now, days) if last else window
        steps.append(Step(window, LAST_RUNG_ENOUGH if last else RUNG_ENOUGH, held, last))
    return tuple(steps)


def in_scope(event_at, scope: TimeScope) -> bool:
    """An item's released time against the scope. An item with no released time is never read as outside it."""
    if type(event_at) is not int:
        return True
    return scope.contains(event_at)


def dated_sentence(sentence: str) -> bool:
    """The sentence says it is about an earlier time: a month name, an ISO date, or an `EARLIER_PHRASES` phrase."""
    if _ISO_DATE.search(sentence):
        return True
    tokens = _tokens(sentence)
    if set(tokens) & set(MONTHS):
        return True
    for phrase in EARLIER_PHRASES:
        parts = phrase.split()
        if any(tokens[at:at + len(parts)] == parts for at in range(len(tokens) - len(parts) + 1)):
            return True
    return False


def _day(seconds: int) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime("%Y-%m-%d")


def time_lines(*, now: int | None, precision: str, scope: TimeScope | None) -> str:
    """What the writer and the judge are told about time, at the grant's precision: today's date (UTC; a date under
    "second" too, never a time of day), the scope's dates, and the rule. Nothing under "none"."""
    if precision not in PRECISIONS or type(now) is not int:
        return ""
    lines = [f"Today is {_day(now)} (UTC). Each item's date is the day it was written."]
    if scope is not None and scope.rung_days:
        # The recency ladder: one fixed line naming the rung retrieval filled, as the app's answer names its window.
        line = f"The question asks about recent days; these items are from the last {scope.rung_days} days."
        if scope.rung_days == RECENT_RUNGS_DAYS[-1]:
            line += " Little is recent: say when each thing happened."
        lines.append(line)
    elif scope is not None:
        last = max(scope.lower, scope.upper - 1)
        if scope.future:
            lines.append(f"The question asks what is coming up; the items shown were written from {_day(scope.lower)} "
                         f"to {_day(last)}.")
        else:
            lines.append(f"The question asks about {_day(scope.lower)} to {_day(last)}.")
    lines.append("If something an item says happened outside the dates the question asks about, say when it "
                 "happened. An item dated before today is in the past: never call what it says upcoming unless the "
                 "item itself names a later date for it.")
    return "\n".join(lines)
