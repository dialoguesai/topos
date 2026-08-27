"""L1 — directed dyadic edges and lifetime dyad stats.

`messenger_social_edges` records that two people share a conversation. It cannot say who
spoke first, who answers, or who has gone quiet — its edges are undirected co-participation,
and its primary key has no room for a direction. This module adds the half that carries
direction, so the questions Rings 6 and 7 actually ask become answerable:

    "In my top relationships, who initiates — me or them?"          (catalog #18)
    "Who am I losing touch with, compared to how often we used to?" (catalog #17)
    "Which friendships have become one-sided in the last months?"   (catalog #19)

Two tables, because they answer at two grains:

  * `messenger_directed_edges` — per period, per connector, per ORDERED pair. Counts,
    session initiations and reply latency, each direction stored separately.
  * `messenger_dyad_stats` — lifetime, per UNORDERED pair. Streaks, gaps, balance and tie
    state are properties of a relationship rather than of a direction, so the dyad is keyed
    canonically (a_key < b_key) and the per-direction totals ride along as columns.

**No content columns, by construction.** Neither table has a column for content, snippet,
subject, body or hash. The only free text stored is handles, ids and enum strings. This is a
schema-level privacy invariant, not a convention, and `test_l1_schema.py` fails if it is ever
violated — a directed-edge table is exactly the shape that tempts someone to cache "the last
thing they said" next to it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MESSENGER_DIRECTED_EDGES_TABLE = "messenger_directed_edges"
MESSENGER_DYAD_STATS_TABLE = "messenger_dyad_stats"

#: The gap that ends a conversation session, so the next message opens one.
#:
#: 6 hours, chosen from the corpus rather than from taste. Measured over 7,392
#: within-conversation gaps the distribution has a clear elbow — p85 = 3.4 h, p90 = 16.7 h, a
#: 5x jump — and 6 h sits inside it, leaving 86.8% of gaps within a session.
#:
#: Deliberately FIXED rather than per-dyad self-calibrated: catalog #18 compares initiation
#: across relationships, and a per-dyad threshold makes those numbers incommensurable. The
#: value is written onto every row (`session_gap_seconds`) so a later recalibration can tell
#: which rows were produced under which threshold.
DEFAULT_SESSION_GAP_SECONDS = 6 * 3600

#: Peers that are not people. Kept rather than dropped (so "what is cluttering my inbox" stays
#: answerable) and filtered at read (so they never pollute a relationship ranking).
PEER_CLASS_HUMAN = "human"
PEER_CLASS_AUTOMATED = "automated"

#: Rooms larger than this mint no broadcast edges — fan-out is quadratic in the roster, and
#: a hundreds-strong room is an announcement channel, not a set of relationships.
MAX_BROADCAST_ROSTER = 32

#: What produced a directed edge. In the PRIMARY KEY on purpose.
#:
#: A single message to a 10-person room would otherwise mint 9 directed edges, and group
#: broadcast would swamp DM signal in every ranking that reads this table — the same failure
#: the undirected lane already has, where co-participation in a big thread outweighs a real
#: correspondence. Keeping the kinds in separate rows means volume conservation stays
#: checkable per kind, and a reader can ask for correspondence without asking for noise.
EDGE_KIND_DM = "dm"              # a two-person conversation: unambiguous direction
EDGE_KIND_GROUP_REPLY = "group_reply"      # reply_to_message_id: a HARD directed edge
EDGE_KIND_GROUP_BROADCAST = "group_broadcast"  # sender -> room: soft, and kept apart


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_peer(key: str) -> str:
    """Shortcodes are carriers, 2FA and delivery notices — 29 of 179 DM peers on the first
    live corpus checked. A 5-digit 'sender' is not a relationship."""
    k = str(key or "").strip().lstrip("+")
    if k.isdigit() and len(k) <= 6:
        return PEER_CLASS_AUTOMATED
    return PEER_CLASS_HUMAN


def dyad_key(a: str, b: str) -> tuple:
    """A dyad is an unordered pair, ordered canonically so it has one row.

    Returned as (a_key, b_key) with a_key <= b_key. Streaks, gaps and tenure belong to the
    relationship; storing them twice (once per direction) would let the two copies disagree.
    """
    x, y = str(a or ""), str(b or "")
    return (x, y) if x <= y else (y, x)


def _add_directed_column_if_missing(conn: Any, column: str, col_type: str) -> None:
    cols = {row[1] for row in
            conn.execute(f"PRAGMA table_info({MESSENGER_DIRECTED_EDGES_TABLE})").fetchall()}
    if column not in cols:
        conn.execute(
            f"ALTER TABLE {MESSENGER_DIRECTED_EDGES_TABLE} ADD COLUMN {column} {col_type}")


def create_directed_tables(conn: Any) -> None:
    """DDL for both L1 tables.

    Called from `_create_messenger_analytics_tables`, which `ensure_messenger_analytics_tables`
    already wraps in the write gate. Deliberately NOT a registry migration: every messenger_*
    table is feature-owned and created this way, and routing this through the registry would
    bump `user_version` past what an installed engine understands and fence the node out of
    every write — which is what happened on 2026-08-25.
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_DIRECTED_EDGES_TABLE} (
            dataset_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            connector TEXT NOT NULL,
            edge_kind TEXT NOT NULL DEFAULT 'dm',
            from_key TEXT NOT NULL,
            to_key TEXT NOT NULL,
            msgs INTEGER NOT NULL DEFAULT 0,
            sessions_initiated INTEGER NOT NULL DEFAULT 0,
            replies INTEGER NOT NULL DEFAULT 0,
            median_reply_latency_s REAL,
            first_ts TEXT,
            last_ts TEXT,
            session_gap_seconds INTEGER NOT NULL,
            from_person_id TEXT,
            to_person_id TEXT,
            affect_counts_json TEXT,
            affect_coverage REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, period_key, connector, edge_kind, from_key, to_key)
        )
        """
    )
    # Additive upgrade for a table that already exists. CREATE TABLE IF NOT EXISTS is a
    # no-op on an existing table, so a column added later would be missing forever on every
    # node that ran an earlier build — the silent-partial-schema failure P0-4 hit on
    # messenger_social_edges. Same reasoning as there: additive ALTER, never a registry
    # migration, so user_version is untouched.
    _add_directed_column_if_missing(conn, "affect_counts_json", "TEXT")
    _add_directed_column_if_missing(conn, "affect_coverage", "REAL")
    # G6 — topic mix on the edge, same shape as affect: counts plus the coverage that
    # keeps the mix honest. Populated from message_topics, which for iMessage fills only
    # when the owner runs the topics backfill — enrolling it in every sync would put an
    # LLM generation on every message, a cost the source registry declines on purpose.
    _add_directed_column_if_missing(conn, "topic_counts_json", "TEXT")
    _add_directed_column_if_missing(conn, "topic_coverage", "REAL")

    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_DIRECTED_EDGES_TABLE}_period
        ON {MESSENGER_DIRECTED_EDGES_TABLE}(dataset_id, period_key, connector)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_DIRECTED_EDGES_TABLE}_from
        ON {MESSENGER_DIRECTED_EDGES_TABLE}(dataset_id, from_key)
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_DYAD_STATS_TABLE} (
            dataset_id TEXT NOT NULL,
            a_key TEXT NOT NULL,
            b_key TEXT NOT NULL,
            involves_self INTEGER NOT NULL DEFAULT 0,
            peer_class TEXT NOT NULL DEFAULT '{PEER_CLASS_HUMAN}',
            total_msgs INTEGER NOT NULL DEFAULT 0,
            a_to_b INTEGER NOT NULL DEFAULT 0,
            b_to_a INTEGER NOT NULL DEFAULT 0,
            balance REAL,
            first_ts TEXT,
            last_ts TEXT,
            active_periods INTEGER NOT NULL DEFAULT 0,
            reciprocal_periods INTEGER NOT NULL DEFAULT 0,
            longest_contact_streak_months INTEGER NOT NULL DEFAULT 0,
            longest_contact_streak_weeks INTEGER NOT NULL DEFAULT 0,
            longest_reciprocal_streak_months INTEGER NOT NULL DEFAULT 0,
            longest_reciprocal_streak_weeks INTEGER NOT NULL DEFAULT 0,
            max_gap_days REAL,
            median_gap_days REAL,
            recent_gap_days REAL,
            drift_ratio REAL,
            tie_state TEXT,
            a_person_id TEXT,
            b_person_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, a_key, b_key)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_DYAD_STATS_TABLE}_self
        ON {MESSENGER_DYAD_STATS_TABLE}(dataset_id, involves_self, peer_class)
        """
    )
    # G3 — ONE labeler. tie_state (fixed thresholds) and the warmth kernel (calibrated)
    # were two answers to "what state is this relationship", disagreeing at band
    # boundaries on 9 of 35 live dyads. The calibrated band is authoritative and is
    # STORED beside the legacy column; tie_state survives for old readers but new code
    # reads warmth_band. Additive ALTER for tables that predate the column.
    cols = {row[1] for row in
            conn.execute(f"PRAGMA table_info({MESSENGER_DYAD_STATS_TABLE})").fetchall()}
    if "warmth_band" not in cols:
        conn.execute(f"ALTER TABLE {MESSENGER_DYAD_STATS_TABLE} ADD COLUMN warmth_band TEXT")


# --------------------------------------------------------------------------- extraction

#: The owner's endpoint. A literal rather than an entity id, so L1 never blocks on L0 —
#: `from_person_id` is the nullable column L0 backfills once the spine exists.
SELF_KEY = "self"


def _parse_ts(value: Any) -> Any:
    """Parse an event timestamp into an AWARE datetime, always.

    Three shapes broke the first version, all found by adversarial testing:
      * a tz-less string parses NAIVE, and one naive value crashes every aware
        comparison after it — a single odd connector row zeroed the whole lane;
      * epoch seconds appear as digit strings on the live corpus and silently
        contributed nothing;
      * TEXT ORDER BY sorts '+02:00' offsets lexicographically, not temporally.
    Naive values are stamped UTC — the storage convention — because dropping them
    would un-count real messages while guessing a local zone would invent times.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.replace(".", "", 1).isdigit():
        try:
            secs = float(text)
            if secs > 1e12:  # milliseconds
                secs /= 1000.0
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _period_of(dt: Any) -> str:
    return dt.strftime("%Y-%m")


class _Acc:
    """One ordered pair, in one period, from one connector, of one kind."""

    __slots__ = ("msgs", "sessions_initiated", "replies", "latencies", "first_ts", "last_ts")

    def __init__(self) -> None:
        self.msgs = 0
        self.sessions_initiated = 0
        self.replies = 0
        self.latencies: list = []
        self.first_ts = None
        self.last_ts = None

    def observe(self, dt) -> None:
        self.msgs += 1
        if self.first_ts is None or dt < self.first_ts:
            self.first_ts = dt
        if self.last_ts is None or dt > self.last_ts:
            self.last_ts = dt


def _median(xs: list):
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    return float(ys[n // 2]) if n % 2 else float((ys[n // 2 - 1] + ys[n // 2]) / 2)


def load_messages(conn: Any, dataset_id: str, connector: Any = None) -> list:
    """Ordered by conversation then time — the order the session walk depends on.

    Rows with no timestamp are dropped rather than defaulted: a message with no time cannot
    be placed in a period, cannot open or continue a session, and defaulting it to now would
    silently invent a conversation that happened at derivation time.
    """
    sql = ("SELECT conversation_id, message_id, sender_id, event_at, is_from_self,"
           " source_id, reply_to_message_id FROM conversation_messages"
           " WHERE dataset_id = ? AND event_at IS NOT NULL")
    args = [dataset_id]
    if connector:
        sql += " AND source_id = ?"
        args.append(connector)
    sql += " ORDER BY conversation_id, event_at"
    rows = conn.execute(sql, args).fetchall()
    # TEXT ORDER BY sorts timezone OFFSETS lexicographically: '09:00+02:00' orders after
    # '08:30+00:00' even though it happened first. Adversarial run measured the result —
    # negative reply latencies and initiations credited to the responder. Re-sort by the
    # parsed instant; the SQL ORDER BY remains only to make ties deterministic.
    return sorted(rows, key=lambda r: (str(r[0] or ""), _parse_ts(r[3]) or datetime.min.replace(tzinfo=timezone.utc)))


def classify_conversations(rows: list) -> dict:
    """DM iff exactly one distinct non-self sender ever spoke in it.

    Measured on the first live corpus: 181 of 202 conversations are DM-shaped. Note this is
    a property of the CORPUS, not of a room's roster — a group where only one other person
    ever spoke reads as a DM here, which is the honest answer for a direction metric built
    from messages rather than from membership.
    """
    senders: dict = {}
    for conv, _mid, sender, _ea, is_self, _src, _rt in rows:
        # A non-self sender whose id equals the owner sentinel is unattributable: counting
        # it would merge a stranger into the owner's own node (the token 'self' exists as a
        # sender_id on the live corpus, so this is a real row shape, not a hypothetical).
        if not is_self and sender and str(sender) != SELF_KEY:
            senders.setdefault(conv, set()).add(str(sender))
    return {conv: (EDGE_KIND_DM if len(s) == 1 else EDGE_KIND_GROUP_BROADCAST)
            for conv, s in senders.items()}


def extract_directed_dyadic_edges(
    conn: Any,
    dataset_id: str,
    *,
    session_gap_seconds: int = DEFAULT_SESSION_GAP_SECONDS,
    connector: Any = None,
) -> dict:
    """Single pass over the corpus producing directed per-period aggregates.

    Sessions, initiations and replies all fall out of one walk, because they are three
    readings of the same fact — where the silences are:

      * a gap >= threshold ends a session, so the next message OPENS one and its sender is
        credited with an initiation;
      * a message from a different speaker than the previous one, inside a session, is a
        REPLY, and the elapsed time is its latency;
      * a message from the same speaker is neither — consecutive texts from one person are
        one turn, and counting each as an initiation is how "who initiates" becomes a
        measure of who is chattiest.

    Returns {(period, connector, edge_kind, from_key, to_key): _Acc}.
    """
    rows = load_messages(conn, dataset_id, connector)
    kinds = classify_conversations(rows)
    peers: dict = {}
    for conv, _mid, sender, _ea, is_self, _src, _rt in rows:
        if not is_self and sender and str(sender) != SELF_KEY:
            peers.setdefault(conv, set()).add(str(sender))

    acc: dict = {}
    by_id: dict = {}

    def bucket(period, conn_id, kind, a, b):
        key = (period, conn_id, kind, a, b)
        if key not in acc:
            acc[key] = _Acc()
        return acc[key]

    cur_conv = None
    last_dt = None
    last_speaker = None
    for conv, mid, sender, ea, is_self, src, reply_to in rows:
        dt = _parse_ts(ea)
        if dt is None:
            continue
        by_id[str(mid)] = (str(sender or ""), bool(is_self), dt)
        speaker = SELF_KEY if is_self else str(sender or "")
        if not speaker:
            continue
        if not is_self and speaker == SELF_KEY:
            continue  # unattributable: see classify_conversations
        if conv != cur_conv:
            cur_conv, last_dt, last_speaker = conv, None, None

        opens_session = last_dt is None or (dt - last_dt).total_seconds() >= session_gap_seconds
        is_reply = (not opens_session) and last_speaker is not None and last_speaker != speaker
        latency = (dt - last_dt).total_seconds() if (is_reply and last_dt) else None

        period = _period_of(dt)
        kind = kinds.get(conv, EDGE_KIND_DM)
        conv_peers = peers.get(conv) or set()

        if kind == EDGE_KIND_DM:
            peer = next(iter(conv_peers)) if conv_peers else ""
            if peer:
                a, b = (SELF_KEY, peer) if is_self else (peer, SELF_KEY)
                e = bucket(period, src, EDGE_KIND_DM, a, b)
                e.observe(dt)
                if opens_session:
                    e.sessions_initiated += 1
                if is_reply:
                    e.replies += 1
                    if latency is not None:
                        e.latencies.append(latency)
        else:
            # A hard directed edge exists only where the corpus states one.
            if reply_to and str(reply_to) in by_id:
                tgt_sender, tgt_self, _tdt = by_id[str(reply_to)]
                target = SELF_KEY if tgt_self else tgt_sender
                if target and target != speaker:
                    e = bucket(period, src, EDGE_KIND_GROUP_REPLY, speaker, target)
                    e.observe(dt)
                    e.replies += 1
                    if latency is not None:
                        e.latencies.append(latency)
            # Broadcast: speaker -> everyone else who has ever spoken in the room. Soft by
            # construction, kept in its own rows so it can be excluded wholesale.
            #
            # Bounded: fan-out is quadratic in the roster (a 500-speaker room mints 250k
            # rows per period), and a room that size is an announcement channel, not a set
            # of relationships — minting nothing is the honest reading of it.
            if len(conv_peers) > MAX_BROADCAST_ROSTER:
                last_dt, last_speaker = dt, speaker
                continue
            room = set(conv_peers) | {SELF_KEY}
            for other in room:
                if other == speaker:
                    continue
                e = bucket(period, src, EDGE_KIND_GROUP_BROADCAST, speaker, other)
                e.observe(dt)
                if opens_session:
                    e.sessions_initiated += 1

        last_dt, last_speaker = dt, speaker
    return acc


def rows_for_persist(acc: dict, dataset_id: str, session_gap_seconds: int,
                     affect: Any = None, topics: Any = None) -> list:
    now = _utc_now()
    affect = affect or {}
    topics = topics or {}
    out = []
    for key, e in acc.items():
        period, conn_id, kind, a, b = key
        af = affect.get(key) or {}
        tp = topics.get(key) or {}
        out.append((dataset_id, period, str(conn_id or ""), kind, a, b, e.msgs,
                    e.sessions_initiated, e.replies, _median(e.latencies),
                    e.first_ts.isoformat() if e.first_ts else None,
                    e.last_ts.isoformat() if e.last_ts else None,
                    int(session_gap_seconds), None, None,
                    af.get("affect_counts_json"), af.get("affect_coverage"),
                    tp.get("topic_counts_json"), tp.get("topic_coverage"), now, now))
    return out


# --------------------------------------------------------------------------- persistence

def persist_directed_edges(conn: Any, dataset_id: str, rows: list, periods: Any = None) -> int:
    """Replace the directed rows for the periods this pass computed.

    Pruning is scoped to the periods actually recomputed, never a blanket delete: a partial
    pass (one connector, one month) must not silently erase everything else. The write and
    its commit both sit inside the gate — taking SQLite's write lock outside it is the
    lock-order inversion the write gate exists to prevent.
    """
    from ..storage.db.write_gate import batched_writes

    touched = set(periods) if periods is not None else {r[1] for r in rows}
    with batched_writes(conn):
        for period in sorted(touched):
            conn.execute(
                f"DELETE FROM {MESSENGER_DIRECTED_EDGES_TABLE}"
                " WHERE dataset_id = ? AND period_key = ?", (dataset_id, period))
        conn.executemany(
            f"""INSERT OR REPLACE INTO {MESSENGER_DIRECTED_EDGES_TABLE}
                (dataset_id, period_key, connector, edge_kind, from_key, to_key, msgs,
                 sessions_initiated, replies, median_reply_latency_s, first_ts, last_ts,
                 session_gap_seconds, from_person_id, to_person_id,
                 affect_counts_json, affect_coverage, topic_counts_json, topic_coverage,
                 created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def persist_dyad_stats(conn: Any, dataset_id: str, rows: list) -> int:
    """The lifetime rollup is a full replacement for this dataset — every row is derived
    from the whole corpus, so a partial update would leave rows describing a corpus that no
    longer exists."""
    from ..storage.db.write_gate import batched_writes

    with batched_writes(conn):
        conn.execute(f"DELETE FROM {MESSENGER_DYAD_STATS_TABLE} WHERE dataset_id = ?",
                     (dataset_id,))
        conn.executemany(
            f"""INSERT OR REPLACE INTO {MESSENGER_DYAD_STATS_TABLE}
                (dataset_id, a_key, b_key, involves_self, peer_class, total_msgs, a_to_b,
                 b_to_a, balance, first_ts, last_ts, active_periods, reciprocal_periods,
                 longest_contact_streak_months, longest_contact_streak_weeks,
                 longest_reciprocal_streak_months, longest_reciprocal_streak_weeks,
                 max_gap_days, median_gap_days, recent_gap_days, drift_ratio, tie_state,
                 a_person_id, b_person_id, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


# --------------------------------------------------------------------------- dyad rollup

TIE_ACTIVE = "active"
TIE_COOLING = "cooling"
TIE_DORMANT = "dormant"
TIE_ONE_SIDED = "one_sided"
TIE_BROADCAST_ONLY = "broadcast_only"


def _longest_run(buckets: set, universe: list) -> int:
    """Longest run of CONSECUTIVE occupied buckets, measured against the corpus's own
    calendar rather than against the dyad's — a dyad that starts in month three cannot have
    a five-month streak, and indexing off its own first bucket would say it could."""
    idx = {b: i for i, b in enumerate(universe)}
    xs = sorted(idx[b] for b in buckets if b in idx)
    best = cur = 0
    prev = None
    for x in xs:
        cur = cur + 1 if prev is not None and x == prev + 1 else 1
        best = max(best, cur)
        prev = x
    return best


def build_dyad_stats(conn: Any, dataset_id: str, *, now: Any = None,
                     recent_window_days: int = 30) -> list:
    """Lifetime rollup, one row per unordered pair.

    Streaks are stored at BOTH grains and in BOTH flavours, which is not indecision. On a
    4.5-month corpus a monthly streak maxes at 5 — too coarse to see drift — while weekly
    carries 21 buckets at the same peer coverage. And the contrast between *contact* (either
    direction) and *reciprocal* (both directions in the bucket) is the whole of catalog #19,
    "which friendships have become one-sided": one number cannot express it.

    `drift_ratio` compares the dyad against ITS OWN baseline rate, never against a global
    one. A monthly correspondent is not drifting because a daily one exists.
    """
    rows = load_messages(conn, dataset_id)
    kinds = classify_conversations(rows)
    peers: dict = {}
    for conv, _m, s, _e, sf, _src, _rt in rows:
        if not sf and s and str(s) != SELF_KEY:
            peers.setdefault(conv, set()).add(str(s))

    per: dict = {}
    all_months: set = set()
    all_weeks: set = set()
    for conv, _mid, sender, ea, is_self, _src, _rt in rows:
        if kinds.get(conv) != EDGE_KIND_DM:
            continue
        dt = _parse_ts(ea)
        if dt is None:
            continue
        cp = peers.get(conv) or set()
        if not cp:
            continue
        peer = next(iter(cp))
        a, b = dyad_key(SELF_KEY, peer)
        d = per.setdefault((a, b), {
            "times": [], "a_to_b": 0, "b_to_a": 0, "months": set(), "weeks": set(),
            "m_out": set(), "m_in": set(), "w_out": set(), "w_in": set()})
        speaker = SELF_KEY if is_self else peer
        mo, wk = dt.strftime("%Y-%m"), dt.strftime("%G-W%V")
        all_months.add(mo)
        all_weeks.add(wk)
        d["times"].append(dt)
        d["months"].add(mo)
        d["weeks"].add(wk)
        if speaker == a:
            d["a_to_b"] += 1
            d["m_out"].add(mo)
            d["w_out"].add(wk)
        else:
            d["b_to_a"] += 1
            d["m_in"].add(mo)
            d["w_in"].add(wk)

    months = sorted(all_months)
    weeks = sorted(all_weeks)
    # The reference instant is NOW, not the corpus maximum. Deriving it from the data let
    # one future-dated message mark every other dyad dormant — the poisoned row set the
    # clock for everyone. A caller may still pass `now` (tests do); production gets wall time.
    ref = now or datetime.now(timezone.utc)
    stamp = _utc_now()
    out = []
    for (a, b), d in per.items():
        times = sorted(d["times"])
        total = len(times)
        gaps = [(times[i] - times[i - 1]).total_seconds() / 86400.0
                for i in range(1, len(times))]
        peer = b if a == SELF_KEY else a
        # Clamped at zero: a future-dated LAST message yields a negative gap, which every
        # downstream comparison reads as "extremely recent". The poison stays confined to
        # the dyad that carries the bad row.
        recent_gap = max(0.0, (ref - times[-1]).total_seconds() / 86400.0) if ref else None
        # own-baseline drift: recent rate vs this dyad's own lifetime rate, never a global one
        span_days = max((times[-1] - times[0]).total_seconds() / 86400.0, 1.0)
        baseline = total / span_days
        recent = sum(1 for t in times
                     if ref and (ref - t).total_seconds() / 86400.0 <= recent_window_days)
        # Divide by the OBSERVABLE window, not the nominal one. A dyad three weeks old has
        # only three weeks in which recent messages could have happened; dividing those by 30
        # understates its rate and reports every new relationship as already cooling.
        observable = recent_window_days
        if ref is not None:
            age_days = (ref - times[0]).total_seconds() / 86400.0
            observable = max(min(recent_window_days, age_days), 1.0)
        drift = round(min(max((recent / observable) / baseline, 0.0), 10.0), 4) if baseline > 0 else None
        recip_m = d["m_out"] & d["m_in"]
        recip_w = d["w_out"] & d["w_in"]
        # BALANCE IS OWNER-RELATIVE, and it has to be stated because the naive definition is
        # silently wrong. `dyad_key` sorts canonically, so for a phone peer ('+1512…' < 's')
        # the owner is b_key, while for an email peer ('zoe@…' > 's') the owner is a_key.
        # Defining balance as (a_to_b - b_to_a) therefore FLIPS SIGN depending on how the
        # counterparty's identifier happens to sort — and "who is one-sided" would invert
        # between two peers for no reason but their phone number.
        #
        # Positive means the OWNER sends more. For a dyad with no owner in it the pair has no
        # privileged side, so the a->b convention stands and a_key/b_key are stored alongside.
        if not total:
            balance = None
        elif a == SELF_KEY:
            balance = round((d["a_to_b"] - d["b_to_a"]) / total, 4)
        elif b == SELF_KEY:
            balance = round((d["b_to_a"] - d["a_to_b"]) / total, 4)
        else:
            balance = round((d["a_to_b"] - d["b_to_a"]) / total, 4)

        if not recip_m:
            tie = TIE_BROADCAST_ONLY
        elif recent_gap is not None and recent_gap > 90:
            tie = TIE_DORMANT
        elif abs(balance or 0) >= 0.6:
            tie = TIE_ONE_SIDED
        elif drift is not None and drift < 0.5:
            tie = TIE_COOLING
        else:
            tie = TIE_ACTIVE

        out.append((
            dataset_id, a, b, 1 if SELF_KEY in (a, b) else 0, classify_peer(peer),
            total, d["a_to_b"], d["b_to_a"], balance,
            times[0].isoformat(), times[-1].isoformat(),
            len(d["months"]), len(recip_m),
            _longest_run(d["months"], months), _longest_run(d["weeks"], weeks),
            _longest_run(recip_m, months), _longest_run(recip_w, weeks),
            round(max(gaps), 4) if gaps else None,
            round(_median(gaps), 4) if gaps else None,
            round(recent_gap, 4) if recent_gap is not None else None,
            drift, tie, None, None, stamp, stamp))
    return out


def ensure_directed_tables_present(conn: Any) -> None:
    """Idempotent DDL for read paths.

    A read surface must not 500 because a write pass has never run. The tables are created
    empty and the endpoint returns an empty list, which is the honest answer to "what are my
    relationships" on a node that has not computed any yet.
    """
    from ..storage.db.write_gate import commit_connection, with_db_write

    with with_db_write():
        create_directed_tables(conn)
        commit_connection(conn)


# --------------------------------------------------------------------------- L3: affect

#: L3 asks for topic mix, affect and co-produced artifacts ON the edge. Measured on the live
#: corpus, only ONE of the three has any messaging coverage at all:
#:
#:   message_topics     2,594 rows — 0 of them match a conversation message (all ChatGPT)
#:   message_sentiment  1,792 rows — likewise, entirely ChatGPT ingestion
#:   message_emotions   4,889 rows — 4,234 of them iMessage
#:
#: So "what do we talk about" is not buildable: the enrichment does not exist for messages.
#: "How does it feel" is, and this is that. Recording the gap in code rather than only in a
#: plan is deliberate — the next person to reach for topic-on-edge should find out here.
def _message_labels(conn: Any, sql: str) -> dict:
    try:
        return {}.__class__((str(r[0]), str(r[1])) for r in conn.execute(sql).fetchall()
                            if r[1] is not None)
    except Exception:  # noqa: BLE001 — a node without the enrichment simply has none
        return {}


def attach_affect(conn: Any, dataset_id: str, acc: dict) -> dict:
    """Fold per-message emotion labels onto the directed edges that carry those messages.

    `affect_coverage` rides beside the counts because a distribution over three labelled
    messages and a distribution over three hundred look identical once normalised, and a
    reader that cannot tell them apart will rank relationships by how much of them happened
    to get enriched. Coverage is the number that makes the mix honest.
    """
    import json as _json
    from collections import Counter

    try:
        labels = {str(r[0]): str(r[1]) for r in conn.execute(
            "SELECT message_id, emotion_label FROM message_emotions"
            " WHERE emotion_label IS NOT NULL").fetchall()}
    except Exception:  # noqa: BLE001 — a node without the enrichment simply has no affect
        return {}
    if not labels:
        return {}

    rows = load_messages(conn, dataset_id)
    kinds = classify_conversations(rows)
    peers: dict = {}
    for conv, _m, s, _e, sf, _src, _rt in rows:
        if not sf and s and str(s) != SELF_KEY:
            peers.setdefault(conv, set()).add(str(s))

    tally: dict = {}
    for conv, mid, sender, ea, is_self, src, _rt in rows:
        if kinds.get(conv) != EDGE_KIND_DM:
            continue
        dt = _parse_ts(ea)
        cp = peers.get(conv) or set()
        if dt is None or not cp:
            continue
        peer = next(iter(cp))
        a, b = (SELF_KEY, peer) if is_self else (peer, SELF_KEY)
        key = (_period_of(dt), str(src or ""), EDGE_KIND_DM, a, b)
        if key not in acc:
            continue
        t = tally.setdefault(key, {"counts": Counter(), "n": 0})
        t["n"] += 1
        lab = labels.get(str(mid))
        if lab:
            t["counts"][lab] += 1
    return {k: {"affect_counts_json": _json.dumps(dict(v["counts"])),
                "affect_coverage": round(sum(v["counts"].values()) / v["n"], 4) if v["n"] else 0.0}
            for k, v in tally.items()}


# --------------------------------------------------------------------------- L1-8: identity

def resolve_peer_identities(conn: Any, peer_keys: list) -> dict:
    """peer_key -> (contact_id, entity_id, display_name|None), by identifier match.

    The write-time half of the person spine that L1 carries nullable columns for. Ambiguity
    abstains: a key matching two contacts fills nothing, because a wrong person id on a
    relationship row is worse than a missing one — it survives joins silently.

    Measured before building (the reason this does NOT normalize formats): full digit-suffix
    normalization gains ZERO names on the live corpus. The unnamed majority fails because
    the address book never ingested a name for them at all (584 of 1,386 contacts carry
    one), not because formats disagree. What this resolves, it resolves exactly.
    """
    out: dict = {}
    if not peer_keys:
        return out
    try:
        ident_rows = conn.execute(
            "SELECT ci.identifier, ci.contact_id, c.display_name FROM contact_identifiers ci"
            " LEFT JOIN contacts c ON c.contact_id = ci.contact_id").fetchall()
        ent_rows = conn.execute(
            "SELECT contact_id, entity_id FROM entities"
            " WHERE contact_id IS NOT NULL AND entity_type='person'").fetchall()
    except Exception:  # noqa: BLE001 — a corpus without these tables has no identities
        return out
    by_ident: dict = {}
    for ident, cid, dn in ident_rows:
        k = str(ident or "").strip().lower()
        if k:
            by_ident.setdefault(k, set()).add((str(cid), str(dn or "") or None))
    ent_by_contact: dict = {}
    for cid, eid in ent_rows:
        ent_by_contact.setdefault(str(cid), []).append(str(eid))
    for peer in peer_keys:
        k = str(peer or "").strip().lower()
        hits = by_ident.get(k) or set()
        cids = {c for c, _ in hits}
        if len(cids) != 1:
            continue  # unknown, or ambiguous — abstain either way
        cid = next(iter(cids))
        ents = ent_by_contact.get(cid) or []
        eid = ents[0] if len(ents) == 1 else None
        dn = next((d for _, d in hits if d), None)
        out[peer] = (cid, eid, dn)
    return out


def backfill_person_ids(conn: Any, dataset_id: str) -> dict:
    """Fill the nullable person-id columns on both L1 tables from resolved identities."""
    from ..storage.db.write_gate import batched_writes

    try:
        keys = {r[0] for r in conn.execute(
            f"SELECT DISTINCT from_key FROM {MESSENGER_DIRECTED_EDGES_TABLE}"
            f" WHERE dataset_id=? AND from_key != ?", (dataset_id, SELF_KEY))}
        keys |= {r[0] for r in conn.execute(
            f"SELECT DISTINCT to_key FROM {MESSENGER_DIRECTED_EDGES_TABLE}"
            f" WHERE dataset_id=? AND to_key != ?", (dataset_id, SELF_KEY))}
    except Exception:  # noqa: BLE001
        return {"resolved": 0, "edges_updated": 0, "dyads_updated": 0}
    ids = resolve_peer_identities(conn, sorted(keys))
    edges = dyads = 0
    with batched_writes(conn):
        for peer, (cid, eid, _dn) in ids.items():
            if not eid:
                continue
            cur = conn.execute(
                f"UPDATE {MESSENGER_DIRECTED_EDGES_TABLE} SET from_person_id=?"
                f" WHERE dataset_id=? AND from_key=?", (eid, dataset_id, peer))
            edges += cur.rowcount
            cur = conn.execute(
                f"UPDATE {MESSENGER_DIRECTED_EDGES_TABLE} SET to_person_id=?"
                f" WHERE dataset_id=? AND to_key=?", (eid, dataset_id, peer))
            edges += cur.rowcount
            cur = conn.execute(
                f"UPDATE {MESSENGER_DYAD_STATS_TABLE} SET a_person_id=?"
                f" WHERE dataset_id=? AND a_key=?", (eid, dataset_id, peer))
            dyads += cur.rowcount
            cur = conn.execute(
                f"UPDATE {MESSENGER_DYAD_STATS_TABLE} SET b_person_id=?"
                f" WHERE dataset_id=? AND b_key=?", (eid, dataset_id, peer))
            dyads += cur.rowcount
    return {"resolved": len(ids), "edges_updated": edges, "dyads_updated": dyads}


def attach_topics(conn: Any, dataset_id: str, acc: dict) -> dict:
    """Fold per-message topics onto the directed edges that carry those messages.

    Same contract as `attach_affect`: counts plus coverage, because a topic mix over three
    labelled messages and one over three hundred look identical once normalised. Reads
    whatever `message_topics` holds — zero for iMessage until the owner runs the topics
    backfill, and the coverage field says exactly that rather than hiding it.
    """
    import json as _json
    from collections import Counter

    labels = _message_labels(
        conn, "SELECT COALESCE(message_id, record_id), topic FROM message_topics"
              " WHERE topic IS NOT NULL")
    if not labels:
        return {}
    rows = load_messages(conn, dataset_id)
    kinds = classify_conversations(rows)
    peers: dict = {}
    for conv, _m, s, _e, sf, _src, _rt in rows:
        if not sf and s and str(s) != SELF_KEY:
            peers.setdefault(conv, set()).add(str(s))
    tally: dict = {}
    for conv, mid, sender, ea, is_self, src, _rt in rows:
        if kinds.get(conv) != EDGE_KIND_DM:
            continue
        dt = _parse_ts(ea)
        cp = peers.get(conv) or set()
        if dt is None or not cp:
            continue
        peer = next(iter(cp))
        a, b = (SELF_KEY, peer) if is_self else (peer, SELF_KEY)
        key = (_period_of(dt), str(src or ""), EDGE_KIND_DM, a, b)
        if key not in acc:
            continue
        t = tally.setdefault(key, {"counts": Counter(), "n": 0})
        t["n"] += 1
        lab = labels.get(str(mid))
        if lab:
            t["counts"][lab] += 1
    return {k: {"topic_counts_json": _json.dumps(dict(v["counts"].most_common(8))),
                "topic_coverage": round(sum(v["counts"].values()) / v["n"], 4) if v["n"] else 0.0}
            for k, v in tally.items() if v["counts"]}
