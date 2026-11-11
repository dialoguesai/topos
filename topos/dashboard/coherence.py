"""Versioned, owner-only descriptive consistency. No model calls or truth-label writes.

V1 deliberately measures literal statement agreement, not truthfulness, character,
or independent corroboration. It misses paraphrases. Each comparable proposition
and period gets one vote, so repeating a sentence cannot swamp a disagreement.
"""
import json
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

METHOD = "literal-claim-consistency-v1"
WINDOW_DAYS = 30
MAX_ROWS = 50_000
SOURCES = {"conversation_messages": ("message_id", "event_at"),
           "journal_entries": ("entry_id", "entry_at"),
           "transcript_segments": ("segment_id", "event_at")}


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except ValueError:
        return None


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def read_claims(conn):
    """Read within the dashboard's snapshot; require surviving source records.

    Return None for unsupported/oversized stores, not a misleading partial score.
    SQL identifiers are closed constants. No source prose, notes or label payloads
    are read. No extra persisted copies means erasure immediately removes a vote.
    """
    columns = _columns(conn, "truth_checks")
    required = {"check_id", "record_table", "record_id", "speaker_kind", "category",
                "claim_text", "verdict", "created_at"}
    if not required <= columns:
        return None
    optional = {"checked_at", "reading_json", "group_override"} & columns
    fields = sorted(required | optional)
    selected = ",".join(f"c.{f}" for f in fields)
    labels = _columns(conn, "truth_labels")
    dismissed = _columns(conn, "truth_dismissals")
    label_join = " LEFT JOIN truth_labels l ON l.check_id=c.check_id" if {"check_id", "label"} <= labels else ""
    dismiss_join = " LEFT JOIN truth_dismissals d ON d.check_id=c.check_id" if "check_id" in dismissed else ""
    extra = (",l.label" if label_join else ",NULL") + (",d.check_id" if dismiss_join else ",NULL")
    result = []
    for table, (pk, clock) in SOURCES.items():
        if not {pk, clock} <= _columns(conn, table):
            continue
        cursor = conn.execute(
            f"SELECT {selected},s.{clock}{extra} FROM truth_checks c "
            f"JOIN {table} s ON c.record_table=? AND c.record_id=s.{pk}"
            f"{label_join}{dismiss_join} WHERE c.speaker_kind='owner' AND c.claim_text IS NOT NULL LIMIT ?",
            (table, MAX_ROWS + 1))
        for raw in cursor:
            result.append(dict(zip([*fields, "source_at", "label", "dismissed"], raw)))
            if len(result) > MAX_ROWS:
                return None
    return result


def _identity(row, zone):
    text = row.get("claim_text")
    at = _stamp(row.get("source_at"))
    if not isinstance(text, str) or not text.strip() or at is None:
        return None
    # Preserve numbers, word order and internal punctuation. No bag-of-words key.
    text = " ".join(unicodedata.normalize("NFKC", text).casefold().split()).rstrip(".!")
    period = ("utterance_day", at.astimezone(zone).date().isoformat())
    reading = row.get("reading")
    if reading is None and row.get("reading_json"):
        try:
            reading = json.loads(row["reading_json"])
        except (ValueError, TypeError):
            return None
    main = None
    if reading is not None:
        if not isinstance(reading, dict) or not isinstance(reading.get("claims"), list) or not reading["claims"]:
            return None
        main = reading["claims"][0]
        if (not isinstance(main, dict) or main.get("subject") != "owner"
                or main.get("reported_speech") is not False or main.get("type") == "intention"):
            return None
        lo, hi = main.get("date_from"), main.get("date_to")
        if lo or hi:
            try:
                if date.fromisoformat(lo).isoformat() != lo or date.fromisoformat(hi).isoformat() != hi or lo > hi:
                    return None
            except (TypeError, ValueError):
                return None  # An open/unknown interval is not a shared time period.
            period = ("explicit_period", lo, hi)
    # Only a narrowly matched direct assertion with a closed, explicit period can
    # produce a potential tension. A missing polarity field is never guessed from
    # a keyword anywhere in the sentence. Everything else is exact repetition only.
    polarity, mode = 1, "literal"
    if main and main.get("type") == "did_or_made" and period[0] == "explicit_period":
        m = re.fullmatch(r"(i|the owner) (have|had|was) (not )?([a-z0-9][a-z0-9 ,'-]*)", text)
        if m and not re.search(r"\b(no|not|never|if|unless|but|before|after|then|maybe|might|could|would|will)\b", m[4]):
            # Keep all words, including actor, tense, values and named entities.
            text = f"{m[1]} {m[2]} {m[4]}"
            polarity, mode = (-1 if m[3] else 1), "explicit_negation"
    return (row.get("category"), period, mode, text), polarity, at


def coherence_series(rows, start, end, *, now=None, zone=timezone.utc):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first.isoformat() != start or last.isoformat() != end or not 0 < (last - first).days <= 366:
        raise ValueError("Expected 1–366 days and an exclusive end")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    prepared, seen = [], set()
    for row in rows or []:
        ref = row.get("record_table"), row.get("record_id")
        if not all(ref) or ref in seen or row.get("speaker_kind") != "owner" or not row.get("claim_text"):
            continue
        seen.add(ref)
        checked = _stamp(row.get("checked_at") or row.get("created_at"))
        source_at = _stamp(row.get("source_at"))
        if checked is None or source_at is None or max(checked, source_at) > now:
            continue
        excluded = (row.get("verdict") not in ("supported", "no_evidence")
                    or bool(row.get("dismissed") or row.get("group_override"))
                    or row.get("label") in ("false", "partly") or row.get("category") == "plan")
        identity = None if excluded else _identity(row, zone)
        prepared.append((max(checked, source_at), identity))
    buckets, day = [], first
    while day < last:
        end_at = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        start_at = datetime.combine(day - timedelta(days=WINDOW_DAYS-1), time.min, zone).astimezone(timezone.utc)
        in_future = day > now.astimezone(zone).date()
        candidates = [identity for at, identity in prepared if start_at <= at < end_at and at <= now] if not in_future else []
        groups = defaultdict(dict)
        eligible = 0
        for identity in candidates:
            if identity is not None:
                key, polarity, at = identity
                eligible += 1
                # Same statement at the same source instant is a duplicate import,
                # irrespective of which canonical table received it.
                groups[key][(at, polarity)] = polarity
        aligned = tension = compared = 0
        for observations in groups.values():
            if len({at for at, _ in observations}) < 2:
                continue
            compared += len(observations)
            if len(set(observations.values())) == 1:
                aligned += 1
            else:
                tension += 1
        n = aligned + tension
        available = rows is not None and not in_future
        buckets.append({"date": day.isoformat(), "status": "unavailable" if not available else "provisional" if n else "insufficient",
                        "owner_claims": len(candidates), "eligible_claims": eligible, "compared_claims": compared,
                        "aligned_groups": aligned, "tension_groups": tension,
                        "score": aligned / n if available and n else None})
        day += timedelta(days=1)
    return {"method": METHOD, "window_days": WINDOW_DAYS, "history_basis": "current_retained_checks",
            "population": "owner_literal_claim_groups", "buckets": buckets}
