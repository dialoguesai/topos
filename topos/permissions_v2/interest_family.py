"""Browser activity as derived interests: monthly topic-cluster aggregates, never pages (OD-52 P7).

The owner's rule for browsing is fixed: a recipient may learn *what the owner has been
interested in, by month*, and nothing that identifies a page. A released interest is
``{label, period (month), strength band, source "browsing"}``, cited by cluster and month.
No URL, title, host name or person ever appears in it (JOURNAL_AND_BROWSER_SOURCES_DESIGN.md
§4.6, §8.1, §9 item 3).

This module builds the objects (IF-5 §1.3) and stores them as ``signal_objects`` of type
``browsing_interest``; it releases nothing. An object is one (topic cluster, calendar month)
pair, computed from the visits (``activity_events`` rows of the ``browser_visits`` source)
that the clustering placed in the cluster. A visit counts only when its time is explicit UTC
and not in the future, and every one of these holds, checked in this order:

1. it is not from a private window (the flat table's ``incognito`` flag, or an incognito
   key in the row's metadata; P1 also withholds them at the canonical write);
2. it is not NSFW-flagged (``is_record_nsfw``);
3. it is not excluded: neither the visit nor any entity it mentions is tombstoned;
4. it is the owner's own capture (``capture_receipts.proven_rows``): the owner's attested
   capture app wrote it, or a live owner receipt lists it at its current revision.

A cluster-month qualifies with at least ``MIN_VISITS`` counted visits on at least
``MIN_DAYS`` distinct UTC days: one page seen once is a record, not an interest. Then, in
order: the month is browsing (more than half of the cluster's members in that month are
visits; a member whose month is unknown counts against every month); the label has the form
of a short topic name (no URL, path, domain or handle, not the clustering's "topic cluster"
fallback); it names no host of any of the cluster's visits; it does not echo a page title
(the clustering's fallback label is a title prefix); it names no person entity (any person's
whole name; every word of the names of persons a visit mentions) and no excluded entity; the
cluster itself is not tombstoned or opted out; and neither the label nor any of the month's
visits (every column, and mention links) touches an Off-limits entity. That last check is
wider than IF-5's minimum (the label and the counted visits' titles): a label is computed
from every member, counted or not. The machine assessment of the label (``interest_review``)
is the last gate, applied where the index admits members.

**Period.** A month is UTC ``[first instant, first instant of the next month)``. The current
month is its elapsed part, ``[first instant, now]``, so a new interest can reach a grant the
day it crosses the threshold rather than a month later; ``interest_index`` admits that open
month only under a grant that releases day-level time (IF-5 Q&A I1). A grant's window admits a period
only when the whole period is inside it (:func:`period_inside`): the elapsed part of the
current month, or a whole past month.

**Revisions.** ``label_revision`` binds the label an assessment was made of (cluster id and
label text); the assessment is stale the moment either changes. ``content_revision`` binds
everything a member stands for (label revision, month, band, completeness, and the digest of
the counted visits at their receipt revision), so any change to them drops the member.

Nothing here reads or writes outside the one read connection it is given. Counts and
digests are the only outputs meant for reports; labels never leave the node except through
release.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .canonical import PolicyError, digest
from .entity_boundary import normalized, skeleton
from .fact_eligibility import canonical_utc_microseconds

VERSION = "topos-interest-objects/v1"
SOURCE_ID = "browser_visits"
TABLE = "activity_events"
RELEASED_SOURCE = "browsing"
MIN_VISITS = 5
MIN_DAYS = 3
# IF-5 §1.3: three steps from the month's counted visits, (band, lowest count). The first band
# starts at the qualifying threshold, so a qualifying month always has a band.
BANDS = (("low", MIN_VISITS), ("medium", 15), ("high", 50))
OBJECT_TYPE = "browsing_interest"
SIGNAL_DIMENSION = "interests"
MAX_LABEL_CHARS = 64
MAX_LABEL_WORDS = 8
DAY_US = 86_400 * 1_000_000
_INCOGNITO_KEYS = ("incognito", "is_incognito", "isIncognito")
_INCOGNITO_TEXT = frozenset({"1", "true", "yes", "on"})
# Host labels too generic to name a site. Everything else in a member visit's host is a
# name the label may not carry.
_GENERIC_HOST_LABELS = frozenset({
    "www", "www2", "m", "mobile", "amp", "app", "apps", "web", "en", "com", "org", "net", "edu", "gov", "mil",
    "int", "io", "co", "uk", "us", "de", "fr", "ca", "au", "jp", "info", "biz", "dev", "ai", "me", "tv"})
_URLISH = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|\bwww\.|[^\s/]/[^\s/]|@|\b[\w-]+\.(?:[a-z]{2,24})\b)")
_WORDS = re.compile(r"[^\W_]+")

# The label and visit checks, in the order they apply. A report counts survivors after each.
VISIT_CHECKS = ("incognito", "nsfw", "excluded", "provenance")
LABEL_CHECKS = ("browsing", "label_form", "label_host", "label_title", "label_person", "excluded_label",
                "opted_out", "offlimits")


@dataclass(frozen=True)
class Visit:
    event_id: str
    at_us: int
    month: str
    day: int
    incognito: bool
    nsfw: bool
    excluded: bool
    proven: bool
    revision: str


@dataclass(frozen=True)
class Candidate:
    """One (cluster, month) pair with at least one browser visit. Private to the node."""
    cluster_id: str
    month: str
    period_start_us: int
    period_end_us: int  # exclusive; the build instant for the current month
    complete: bool
    #: visits and distinct days after each visit check: key "all" then each of VISIT_CHECKS.
    visits: dict
    days: dict
    #: the first failing label check (LABEL_CHECKS order), or None.
    label_withheld: Optional[str]
    label: Optional[str]
    member_revision: str

    def qualifies(self, stage: str = VISIT_CHECKS[-1]) -> bool:
        return self.visits[stage] >= MIN_VISITS and self.days[stage] >= MIN_DAYS


@dataclass(frozen=True)
class InterestObject:
    """A cluster-month that passed every deterministic check. Assessment is still owed."""
    interest_id: str
    cluster_id: str
    month: str
    period_start_us: int
    period_end_us: int
    complete: bool
    label: str
    label_revision: str
    band: str
    visits: int
    days: int
    content_revision: str


@dataclass
class Build:
    built_at_us: int
    candidates: list = field(default_factory=list)
    objects: list = field(default_factory=list)
    #: visit-level counts (no ids, no text): unknown or future time, visits outside every cluster.
    visit_counts: Counter = field(default_factory=Counter)
    schema: str = "ok"


def band_for(visits: int) -> Optional[str]:
    found = None
    for name, lowest in BANDS:
        if visits >= lowest:
            found = name
    return found


def month_of(at_us: int) -> str:
    moment = datetime.fromtimestamp(at_us // 1_000_000, tz=timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


def month_span(month: str) -> tuple:
    """[start, end) of a UTC calendar month, in microseconds since the epoch."""
    if not isinstance(month, str) or re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) is None:
        raise PolicyError("interest_period_invalid")
    year, number = int(month[:4]), int(month[5:])
    start = datetime(year, number, 1, tzinfo=timezone.utc)
    end = datetime(year + (number == 12), 1 if number == 12 else number + 1, 1, tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (int((start - epoch).total_seconds()) * 1_000_000, int((end - epoch).total_seconds()) * 1_000_000)


def period_inside(*, period_start_us: int, period_end_us: int, now_us: int, max_age_seconds: int) -> bool:
    """The grant window's rule at month granularity: the whole period lies in [now - max_age, now].

    ``period_end_us`` is exclusive. A period that has not begun, or whose start is older than
    the window, is out. Unknown values withhold.
    """
    values = (period_start_us, period_end_us, now_us, max_age_seconds)
    if any(type(value) is not int for value in values) or max_age_seconds < 0 or period_end_us <= period_start_us:
        return False
    return now_us - max_age_seconds * 1_000_000 <= period_start_us and period_end_us <= now_us + 1


def interest_id(cluster_id: str, month: str) -> str:
    """The object's key and the record id its opaque release id is minted from (IF-5 §3)."""
    return f"interest:{cluster_id}:{month}"


def opt_out_key(cluster_id: str) -> str:
    """The review store's opt-out key for every month of one cluster's interest."""
    return "interest-cluster:" + digest({"version": VERSION, "cluster_id": cluster_id})


def label_revision(cluster_id: str, label: str) -> str:
    return digest({"version": VERSION, "cluster_id": cluster_id, "label": label})


def content_revision(*, label_rev: str, month: str, band: str, complete: bool, member_revision: str) -> str:
    return digest({"version": VERSION, "label_revision": label_rev, "month": month, "band": band,
                   "complete": complete, "members": member_revision})


def _table_columns(conn, table: str) -> set:
    found = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchmany(2)
    if len(found) != 1 or found[0][0] != "table":
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return isinstance(value, str) and value.strip().lower() in _INCOGNITO_TEXT


def _metadata_incognito(raw: Any) -> bool:
    if raw is None or raw == "":
        return False
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return True  # unreadable metadata cannot prove the visit was not private
    if not isinstance(value, dict):
        return True
    return any(_truthy(value.get(key)) for key in _INCOGNITO_KEYS)


def _words(text: str) -> list:
    return _WORDS.findall(normalized(text))


def _name_keys(names: Iterable[str]) -> tuple:
    """(whole-name skeletons, name words of four letters or more) for a set of names."""
    whole, parts = set(), set()
    for name in names:
        if not isinstance(name, str):
            continue
        key = skeleton(name)
        if key:
            whole.add(key)
        parts.update(skeleton(word) for word in _words(name) if len(skeleton(word)) >= 4)
    return frozenset(whole), frozenset(parts)


def names_any(label: str, keys: tuple) -> bool:
    """Whether the label carries any of these names: a whole name (substring when four letters or
    more, else a whole word) or any word of four letters or more of one."""
    whole, parts = keys
    tokens = {skeleton(word) for word in _words(label)}
    compact = skeleton(label)
    return (any(key in compact if len(key) >= 4 else key in tokens for key in whole)
            or bool(parts.intersection(tokens)))


def label_form_ok(label: Any) -> bool:
    """A short topic name: no URL, path, domain, handle or fallback text."""
    if not isinstance(label, str) or label != label.strip() or not label:
        return False
    if len(label) > MAX_LABEL_CHARS or len(label.split()) > MAX_LABEL_WORDS:
        return False
    if skeleton(label) in {"", "topiccluster"} or not any(ch.isalpha() for ch in label):
        return False
    return _URLISH.search(label) is None


def _host_keys(hosts: Iterable[str]) -> frozenset:
    keys = set()
    for host in hosts:
        if not isinstance(host, str) or not host.strip():
            continue
        parts = [skeleton(part) for part in host.strip().lower().split(".")]
        keys.update(part for part in parts if len(part) >= 3 and part not in _GENERIC_HOST_LABELS)
        significant = "".join(part for part in parts if part and part not in _GENERIC_HOST_LABELS)
        if len(significant) >= 5:
            keys.add(significant)
    return frozenset(keys)


def names_host(label: str, host_keys: frozenset) -> bool:
    tokens = {skeleton(word) for word in _words(label)}
    compact = skeleton(label)
    return any(key in compact if len(key) >= 5 else key in tokens for key in host_keys)


def echoes_title(label: str, titles: Iterable[str]) -> bool:
    """Whether the label reads as a page title: equal to one, a (possibly cut) prefix of one of
    at least two words, or sharing four consecutive words with one."""
    words = _words(label)
    if not words:
        return False
    shingles = {tuple(words[i:i + 4]) for i in range(len(words) - 3)}
    for title in titles:
        if not isinstance(title, str) or not title:
            continue
        other = _words(title)
        if other == words:
            return True
        if (len(words) >= 2 and len(other) >= len(words) and other[:len(words) - 1] == words[:-1]
                and other[len(words) - 1].startswith(words[-1])):
            return True
        if shingles and any(tuple(other[i:i + 4]) in shingles for i in range(len(other) - 3)):
            return True
    return False


def _receipt_row(row: dict) -> dict:
    """The fields a receipt's content revision reads for a visit (capture_receipts FAMILIES)."""
    return {"event_id": row["event_id"], "source_id": row["source_id"], "url": row.get("url"),
            "occurred_at": row.get("occurred_at"), "writer_class": row.get("writer_class"),
            "writer_app_id": row.get("writer_app_id"), "writer_dataset_id": row.get("writer_dataset_id")}


def build(conn, *, owner_id: str, now_us: int, boundary=None, opt_outs: frozenset = frozenset(),
          clusters: Optional[Iterable[str]] = None) -> Build:
    """Every candidate cluster-month and every object that passed the deterministic checks.

    ``conn`` is one read snapshot of the canonical database. ``boundary`` is an
    ``EntityBoundary`` over that same snapshot (built here when absent). ``opt_outs`` is the
    review store's opt-out key set. ``clusters`` limits the build to those clusters (a release
    re-derives one); every check reads the same rows either way. Missing schema yields an empty
    build, never a guess.
    """
    from topos.disclosure.content_policy import is_record_nsfw

    from . import capture_receipts
    from .exclusion_floor import exclusions

    out = Build(built_at_us=now_us)
    activity = _table_columns(conn, TABLE)
    clusters_cols = _table_columns(conn, "topic_clusters")
    members_cols = _table_columns(conn, "topic_cluster_members")
    if (not {"event_id", "occurred_at", "source_id", "url", "title"} <= activity
            or not {"cluster_id", "label"} <= clusters_cols
            or not {"cluster_id", "record_id", "source_id"} <= members_cols):
        out.schema = "unavailable"
        return out
    if boundary is None:
        from .entity_boundary import EntityBoundary
        boundary = EntityBoundary(conn)
    tombstones = exclusions(conn)

    optional = [c for c in ("hostname", "source_record_id", "metadata_json", "content_nsfw", "writer_class",
                            "writer_app_id", "writer_dataset_id") if c in activity]
    selected = ["event_id", "occurred_at", "source_id", "url", "title", *optional]
    preview = "m.text_preview" if "text_preview" in members_cols else "NULL"
    only = None if clusters is None else sorted({c for c in clusters if isinstance(c, str)})
    if only == []:
        return out
    scope = "" if only is None else f" AND m.cluster_id IN ({','.join('?' * len(only))})"
    rows = conn.execute(
        f"SELECT m.cluster_id, {preview}, {', '.join('a.' + c for c in selected)} "
        f"FROM topic_cluster_members m JOIN {TABLE} a ON a.event_id = m.record_id AND a.source_id = m.source_id "
        f"WHERE m.source_id = ?{scope} ORDER BY m.cluster_id, a.event_id", (SOURCE_ID, *(only or ()))).fetchall()
    visits_by_id: dict = {}
    clusters_of: dict = defaultdict(list)
    previews: dict = defaultdict(list)
    for cluster_id, text_preview, *values in rows:
        row = dict(zip(selected, values))
        if not isinstance(cluster_id, str) or not isinstance(row["event_id"], str):
            continue
        visits_by_id[row["event_id"]] = row
        clusters_of[cluster_id].append(row["event_id"])
        if isinstance(text_preview, str):
            previews[cluster_id].append(text_preview)

    flat_incognito = set()
    if {"record_id", "incognito"} <= _table_columns(conn, "browser_visits"):
        flat_incognito = {r[0] for r in conn.execute("SELECT record_id, incognito FROM browser_visits")
                          if _truthy(r[1])}
    mentions: dict = defaultdict(set)
    if {"record_id", "entity_id", "canonical_table"} <= _table_columns(conn, "entity_mentions"):
        for record_id, entity_id in conn.execute(
                "SELECT record_id, entity_id FROM entity_mentions WHERE canonical_table = ?", (TABLE,)):
            if record_id in visits_by_id:
                mentions[record_id].add(entity_id)
    # IF-5: the label names no person entity. Every person's whole name (and aliases) is checked;
    # the persons a visit mentions also by each word of their names, since the label was drawn
    # from those very pages. Excluded entities are checked the same way.
    person_names, mentioned_names, excluded_names = [], [], []
    entity_cols = _table_columns(conn, "entities")
    if {"entity_id", "entity_type", "canonical_name"} <= entity_cols:
        mentioned = set().union(*mentions.values()) if mentions else set()
        alias_col = "aliases_json" if "aliases_json" in entity_cols else "NULL"
        for entity_id, entity_type, name, aliases in conn.execute(
                f"SELECT entity_id, entity_type, canonical_name, {alias_col} FROM entities"):
            is_person, is_excluded = entity_type == "person", entity_id in tombstones["entity"]
            if not is_person and not is_excluded:
                continue
            names = [name]
            try:
                decoded = json.loads(aliases) if isinstance(aliases, str) else []
            except ValueError:
                decoded = []
            if isinstance(decoded, list):
                names.extend(alias for alias in decoded if isinstance(alias, str))
            if is_person:
                person_names.extend(names)
                if entity_id in mentioned:
                    mentioned_names.extend(names)
            if is_excluded:
                excluded_names.extend(names)
    person_keys = (_name_keys(person_names)[0], _name_keys(mentioned_names)[1])
    excluded_keys = _name_keys(excluded_names)

    proven_ids = capture_receipts.proven_rows(conn, owner_id=owner_id, table=TABLE, source_id=SOURCE_ID,
                                              rows=[_receipt_row(r) for r in visits_by_id.values()])
    visits: dict = {}
    for event_id, row in visits_by_id.items():
        at_us = canonical_utc_microseconds(row["occurred_at"])
        if at_us is None:
            out.visit_counts["time_unknown"] += 1
            continue
        if at_us > now_us:
            out.visit_counts["time_future"] += 1
            continue
        visits[event_id] = Visit(
            event_id=event_id, at_us=at_us, month=month_of(at_us), day=at_us // DAY_US,
            incognito=row.get("source_record_id") in flat_incognito or _metadata_incognito(row.get("metadata_json")),
            nsfw=is_record_nsfw(row),
            excluded=event_id in tombstones["record"] or bool(mentions.get(event_id, set()) & tombstones["entity"]),
            proven=event_id in proven_ids,
            revision=capture_receipts.content_revision(TABLE, _receipt_row(row)))

    labels = dict(conn.execute("SELECT cluster_id, label FROM topic_clusters"))
    browsing, others, unplaced = _month_mix(conn, clusters_of, visits_by_id)

    for cluster_id in sorted(clusters_of):
        label = labels.get(cluster_id)
        member_ids = [v for v in clusters_of[cluster_id] if v in visits]
        rows_all = [visits_by_id[v] for v in clusters_of[cluster_id]]
        cluster_check = _label_check(
            cluster_id, label, rows_all, previews[cluster_id], person_keys, excluded_keys, tombstones, opt_outs,
            boundary)
        by_month: dict = defaultdict(list)
        for event_id in member_ids:
            by_month[visits[event_id].month].append(visits[event_id])
        for month in sorted(by_month):
            month_visits = by_month[month]
            start, end = month_span(month)
            complete = end <= now_us
            counted, counts, days = list(month_visits), {"all": len(month_visits)}, {
                "all": len({v.day for v in month_visits})}
            for check in VISIT_CHECKS:
                counted = [v for v in counted if _VISIT_PASSES[check](v)]
                counts[check], days[check] = len(counted), len({v.day for v in counted})
            members_in_month = browsing[cluster_id][month] + others[cluster_id][month] + unplaced[cluster_id]
            withheld = "browsing" if browsing[cluster_id][month] * 2 <= members_in_month else cluster_check
            if withheld is None and _visits_protected(boundary, [visits_by_id[v.event_id] for v in month_visits]):
                withheld = "offlimits"
            member_rev = digest({"version": VERSION, "visits": sorted([v.event_id, v.revision] for v in counted)})
            candidate = Candidate(cluster_id=cluster_id, month=month, period_start_us=start,
                                  period_end_us=end if complete else now_us, complete=complete, visits=counts,
                                  days=days, label_withheld=withheld, label=label if isinstance(label, str) else None,
                                  member_revision=member_rev)
            out.candidates.append(candidate)
            if withheld is None and candidate.qualifies():
                band = band_for(counts[VISIT_CHECKS[-1]])
                label_rev = label_revision(cluster_id, label)
                out.objects.append(InterestObject(
                    interest_id=interest_id(cluster_id, month), cluster_id=cluster_id, month=month,
                    period_start_us=start, period_end_us=candidate.period_end_us, complete=complete, label=label,
                    label_revision=label_rev, band=band, visits=counts[VISIT_CHECKS[-1]], days=days[VISIT_CHECKS[-1]],
                    content_revision=content_revision(label_rev=label_rev, month=month, band=band,
                                                      complete=complete, member_revision=member_rev)))
    return out


_VISIT_PASSES = {"incognito": lambda v: not v.incognito, "nsfw": lambda v: not v.nsfw,
                 "excluded": lambda v: not v.excluded, "provenance": lambda v: v.proven}
#: Where a non-browsing member's time is read, by the member's record id.
_MEMBER_TIMES = (("activity_events", "event_id", "occurred_at"), ("ai_chat_messages", "message_id", "event_at"),
                 ("conversation_messages", "message_id", "event_at"))


def _month_mix(conn, clusters_of: dict, visits_by_id: dict) -> tuple:
    """Per cluster and month: browser visits, other members placed in that month, and members of the
    cluster that cannot be placed in any month (counted against every month).

    IF-5 §1.3: a cluster-month is browsing only when more than half its members in the month are
    browser visits. A member whose time is unknown might be in any month, so it counts in all of them;
    a visit with an unknown time is such a member too, never a browser visit of some month.
    """
    browsing: dict = defaultdict(Counter)
    others: dict = defaultdict(Counter)
    unplaced: Counter = Counter()
    for cluster_id, event_ids in clusters_of.items():
        for event_id in event_ids:
            at_us = canonical_utc_microseconds(visits_by_id[event_id]["occurred_at"])
            if at_us is None:
                unplaced[cluster_id] += 1
            else:
                browsing[cluster_id][month_of(at_us)] += 1
    wanted = sorted(clusters_of)
    rest: list = []
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        rest.extend(conn.execute(
            f"SELECT cluster_id, record_id FROM topic_cluster_members WHERE source_id != ? AND cluster_id IN "
            f"({','.join('?' * len(chunk))})", (SOURCE_ID, *chunk)).fetchall())
    times: dict = {}
    ids = sorted({record_id for _cluster, record_id in rest if isinstance(record_id, str)})
    for table, id_column, time_column in _MEMBER_TIMES:
        if not {id_column, time_column} <= _table_columns(conn, table):
            continue
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for record_id, value in conn.execute(
                    f"SELECT {id_column}, {time_column} FROM {table} WHERE {id_column} IN ({','.join('?' * len(chunk))})",
                    chunk):
                times.setdefault(record_id, set()).add(value)
    for cluster_id, record_id in rest:
        found = times.get(record_id, set())
        instants = {canonical_utc_microseconds(value) for value in found}
        if len(instants) != 1 or None in instants:
            unplaced[cluster_id] += 1  # unknown, or ambiguous across tables: might be in any month
        else:
            others[cluster_id][month_of(next(iter(instants)))] += 1
    return browsing, others, unplaced


def _visits_protected(boundary, rows) -> bool:
    """Any of the month's visits touches an Off-limits entity (url, title, host, mention link).
    A boundary that cannot decide withholds."""
    try:
        return any(boundary.legacy_veto(TABLE, row) for row in rows)
    except PolicyError:
        return True


def _label_check(cluster_id, label, rows_all, previews, person_keys, excluded_keys, tombstones, opt_outs,
                 boundary) -> Optional[str]:
    """The first LABEL_CHECKS failure for this cluster after "browsing", or None. Browsing and
    visit-level Off-limits are decided per month."""
    if not label_form_ok(label):
        return "label_form"
    if names_host(label, _host_keys([r.get("hostname") for r in rows_all] + [_url_host(r.get("url"))
                                                                                 for r in rows_all])):
        return "label_host"
    if echoes_title(label, [r.get("title") for r in rows_all] + list(previews)):
        return "label_title"
    if names_any(label, person_keys):
        return "label_person"
    if cluster_id in tombstones["record"] or names_any(label, excluded_keys):
        return "excluded_label"
    if opt_out_key(cluster_id) in opt_outs:
        return "opted_out"
    if boundary.mentions_protected(label):
        return "offlimits"
    return None


def _url_host(url: Any) -> Optional[str]:
    if not isinstance(url, str):
        return None
    from urllib.parse import urlsplit
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def payload(obj: InterestObject) -> dict:
    """The stored object (IF-5 §1.3), plus the two revisions that bind its assessment and its membership."""
    return {"label": obj.label, "month": obj.month, "strength": obj.band, "cluster_id": obj.cluster_id,
            "visit_count": obj.visits, "day_count": obj.days, "label_revision": obj.label_revision,
            "content_revision": obj.content_revision}


def _iso(at_us: int) -> str:
    return datetime.fromtimestamp(at_us / 1_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def persist(conn, result: Build) -> dict:
    """Make the stored ``browsing_interest`` objects exactly ``result.objects``. Counts only; the caller holds
    the write gate and commits.

    An object whose content revision changed, or that no longer qualifies, is closed (``valid_to``), never
    edited in place; a changed one is inserted again under the same key. Nothing else in ``signal_objects``
    is touched. Inserting a non-fact object does not move the protection clock.
    """
    if result.schema != "ok":
        return {"inserted": 0, "closed": 0, "unchanged": 0}
    now = _iso(result.built_at_us)
    wanted = {obj.interest_id: obj for obj in result.objects}
    active = conn.execute(
        "SELECT object_id, object_key, payload_json FROM signal_objects WHERE signal_dimension=? AND object_type=? "
        "AND valid_to IS NULL", (SIGNAL_DIMENSION, OBJECT_TYPE)).fetchall()
    counts = {"inserted": 0, "closed": 0, "unchanged": 0}
    kept = set()
    for object_id, key, stored in active:
        obj = wanted.get(key)
        try:
            same = obj is not None and json.loads(stored or "{}").get("content_revision") == obj.content_revision
        except ValueError:
            same = False
        if same and key not in kept:
            kept.add(key)
            counts["unchanged"] += 1
            continue
        conn.execute("UPDATE signal_objects SET valid_to=?, updated_at=? WHERE object_id=?", (now, now, object_id))
        counts["closed"] += 1
    for key, obj in sorted(wanted.items()):
        if key in kept:
            continue
        object_id = "bi_" + digest({"key": key, "revision": obj.content_revision, "at": now})[:32]
        conn.execute(
            "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, confidence, "
            "source_refs_json, valid_from, extractor_version, period_start, period_end, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (object_id, SIGNAL_DIMENSION, OBJECT_TYPE, key, json.dumps(payload(obj), sort_keys=True), 1.0,
             json.dumps([{"table": "topic_clusters", "id": obj.cluster_id, "month": obj.month}]), now, VERSION,
             _iso(obj.period_start_us), _iso(obj.period_end_us), now, now))
        counts["inserted"] += 1
    return counts
