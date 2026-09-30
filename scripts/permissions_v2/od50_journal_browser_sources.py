"""OD-50 / OD-51 Phase 1: journal entries, browser visits and every other canonical table as grant sources.
Owner-local, on a keyless census copy, counts only.

Plan: audits/2026-09-14-permissions/latency-coverage-2026-09-28/OWNER_DECISIONS_2026-09-28.md OD-50, OD-51.

Against the copy it counts, per canonical table: rows, rows inside the 30/90/365-day windows, the identity and
writer columns the table carries, how its rows were written (writer class, source, install posture), the role the
node's own rule gives them (`features.provenance.roles.record_role`), and the derived items that cite them (facts,
goals, `pursues` edges, entity mentions, embeddings, other signal objects). For journal entries it then runs the
release guards a permitted message goes through today (Off-limits boundary, owner-only, exclusions, NSFW, copies,
window, disclosure, predicate class, subject, value, grounding: fullmatch, verbatim, OD-38 guards) over the facts,
goals and relationships that cite them, and sizes what the search index would hold. For browser visits it counts
what a raw record would release and what is derived from visits.

`--assess N|all` runs the node's own machine-assessment rubric (pinned local model, loopback only) over journal
entries and tallies labels; it is run only inside a local-model window WS0 grants. Nothing is written to any store.

Strings that leave this script are code identifiers only: catalog source ids, table names, predicate names and
fixed codes. Any other string is replaced by its length. No content, value, name or identifier is printed or
written.

Run (zsh; every flag its own token):
  export TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env
  PYTHONPATH=$PWD <engine venv>/bin/python3 scripts/permissions_v2/od50_journal_browser_sources.py \\
      --copy <candidates>/census-copy/<run-id> --out <LC>/runs/<run-id>-od50/od50-measure.json
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import census_support as cs  # noqa: E402

WINDOWS = {"d30": 30, "d90": 90, "d365": 365}
# Canonical tables: (id column, time column, source column). A None source column means the table name is the
# source id (the browser flat tables).
TABLES = {
    "conversation_messages": ("message_id", "event_at", "source_id"),
    "ai_chat_messages": ("message_id", "event_at", "source_id"),
    "journal_entries": ("entry_id", "entry_at", "source_id"),
    "grow_journal_sessions": ("record_id", "starts_at", "source_id"),
    "browser_visits": ("record_id", "visited_at", None),
    "browser_events": ("record_id", "visited_at", None),
    "activity_events": ("event_id", "occurred_at", "source_id"),
    "location_events": ("event_id", "event_at", "source_id"),
    "contacts": ("contact_id", None, "source_id"),
    "calendar_events": ("event_id", "starts_at", "source_id"),
    "profile_records": ("record_id", None, "source_id"),
    "financial_transactions": ("transaction_id", "posted_at", "source_id"),
    "documents": ("doc_id", "modified_at", "source_id"),
}
IDENTITY_COLUMNS = ("owner_user_id", "dataset_id", "source_id", "writer_class", "writer_app_id", "writer_dataset_id",
                    "actor_role", "is_from_self", "sender_type", "content", "content_nsfw", "metadata_json",
                    "conversation_id", "incognito")
WRITER_CLASSES = ("owner_app", "owner_import", "local_legacy", "cp_relay", "third_party", "owner_automation")
ROLES = ("authored", "addressed", "participated", "observed", "ambient")
# Journal source ids are catalog literals in the engine (analytics/luck_surface.py, features/complexity) but not
# bundled sources; they are printed because the code names them.
CODE_SOURCES = ("grow_journal", "grow_data_file", "demo_journal_file")


# --- print vocabulary ------------------------------------------------------------------------

class Vocabulary:
    """Strings allowed out: code identifiers. Everything else leaves as its length."""
    _TOKEN = re.compile(r"^[A-Za-z0-9_.:/\-]{1,64}$")

    def __init__(self):
        from topos.features.facts.store import KNOWN_PREDICATES
        from topos.permissions_v2.knowledge_projections import PREDICATE_TEXT
        from topos.permissions_v2.predicate_classes import CLASSES
        from topos.sources.registry import BUNDLED_REGISTRY
        self.sources = set(BUNDLED_REGISTRY) | set(CODE_SOURCES)
        self.words = set(self.sources) | set(PREDICATE_TEXT) | set(CLASSES) | set(KNOWN_PREDICATES)
        self.words |= set(TABLES) | set(IDENTITY_COLUMNS) | set(WRITER_CLASSES) | set(ROLES) | set(WINDOWS)
        self.words |= {"personal", "mixed", "ambient", "none", "special", "unknown", "present", "fact", "goal",
                       "relationship", "message", "pursues", "located_at", "true", "false", "n/a", "custom",
                       "signal_objects", "user_goals", "entity_edges", "entity_mentions", "signal_embeddings",
                       "topic_cluster_members", "original_message", "third_party_quote", "work", "plans", "hobbies",
                       "home", "family", "finance", "relationships", "health"}

    def source(self, value) -> str:
        return value if isinstance(value, str) and value in self.sources else ("custom" if value else "none")

    def clean(self, value):
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value if value in self.words and self._TOKEN.match(value) else f"<len={len(value)}>"
        if isinstance(value, dict):
            return {str(self.clean(k)): self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.clean(v) for v in value]
        return f"<{type(value).__name__}>"

    def allow(self, *labels):
        self.words |= set(labels)


def _tables_present(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


TIME_PARSE = collections.Counter()   # per table: canonical (the node's rule) vs lenient (naive text read as UTC)


def _age_days(now_s: int, stamp, *, table: str = "") -> float | None:
    """Age in days. The node's window rule accepts only `canonical_utc_microseconds`; a naive ISO stamp is
    'missing_or_ambiguous: withhold' there. It is read here as UTC so the windows can be sized, and counted."""
    from datetime import datetime, timezone
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    us = canonical_utc_microseconds(stamp)
    if us is not None:
        TIME_PARSE[f"{table}:canonical"] += 1
        return (now_s * 1_000_000 - us) / 86_400_000_000
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            TIME_PARSE[f"{table}:unparseable"] += 1
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        TIME_PARSE[f"{table}:lenient_naive_as_utc"] += 1
        return (now_s - parsed.timestamp()) / 86_400
    TIME_PARSE[f"{table}:missing"] += 1
    return None


def _window_flags(age):
    return {name: (age is not None and 0 <= age <= days) for name, days in WINDOWS.items()}


# --- per-table inventory (OD-51) ---------------------------------------------------------------

def _install_posture(conn, source_id, bundled) -> dict:
    """The three posture inputs `evidence._source_posture` reads, per source. Counts and enum values only."""
    bundled_posture = getattr(bundled, "posture", None) if bundled is not None else None
    installs = conn.execute("SELECT scope_key, is_active, status, source_definition_json FROM source_runtime_installs "
                            "WHERE source_id=?", (source_id,)).fetchall()
    active, concrete, install_posture = 0, 0, []
    for scope_key, is_active, status, definition in installs:
        if is_active == 1 and status in ("installed", "active", "ready"):
            active += 1
            try:
                scope = json.loads(scope_key) if isinstance(scope_key, str) else {}
                definition = json.loads(definition) if isinstance(definition, str) else {}
            except ValueError:
                scope, definition = {}, {}
            if isinstance(scope, dict) and scope.get("dataset_id") not in (None, "*"):
                concrete += 1
            install_posture.append(definition.get("posture") if isinstance(definition, dict) else None)
    overrides = [r[0] for r in conn.execute("SELECT posture FROM user_ingestion_sources WHERE source_id=?", (source_id,))]
    effective = (install_posture[0] if install_posture and install_posture[0] else None) or bundled_posture or "mixed"
    if effective == "mixed" and bundled_posture not in (None, "mixed"):
        effective = bundled_posture
    explicit = [p for p in overrides if p]
    return {"bundled_posture": bundled_posture, "installs": len(installs), "active_installs": active,
            "active_install_dataset_scoped": concrete, "install_posture": install_posture,
            "override_rows": len(overrides), "override_postures": explicit, "effective_default": effective,
            # A datasetless identity against a dataset-scoped install: `_source_posture` refuses (RD5's failure).
            "datasetless_identity_would_refuse": active == 1 and concrete == 1,
            "multiple_active_installs_refuse": active > 1}


def inventory(conn, now_s: int, vocab: Vocabulary) -> dict:
    from topos.features.provenance.roles import record_role
    from topos.sources.registry import BUNDLED_REGISTRY
    present = _tables_present(conn)
    out = {}
    for table, (id_col, time_col, source_col) in TABLES.items():
        if table not in present:
            out[table] = {"present": False}
            continue
        columns = _columns(conn, table)
        entry = {"present": True, "id_column": id_col, "time_column": time_col,
                 "columns": {c: (c in columns) for c in IDENTITY_COLUMNS},
                 "rows": conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]}
        select = [id_col] + ([time_col] if time_col else []) + [c for c in IDENTITY_COLUMNS if c in columns]
        windows = collections.Counter()
        writers = collections.Counter()
        roles = collections.Counter()
        per_source = collections.defaultdict(collections.Counter)
        undated = future = 0
        posture_cache: dict = {}
        for row in conn.execute(f"SELECT {', '.join(select)} FROM {table}"):
            row = dict(zip(select, row))
            source = row.get(source_col) if source_col else table
            skey = vocab.source(source)
            age = _age_days(now_s, row.get(time_col), table=table) if time_col else None
            flags = _window_flags(age)
            if time_col and age is None:
                undated += 1
            elif time_col and age < 0:
                future += 1
            for name, hit in flags.items():
                if hit:
                    windows[name] += 1
                    per_source[skey][name] += 1
            per_source[skey]["rows"] += 1
            writer = row.get("writer_class") if "writer_class" in columns else "column_absent"
            writers[writer if writer in WRITER_CLASSES or writer == "column_absent" else ("null" if writer is None else "other")] += 1
            # The node's own role rule, with the source's effective default posture (per-dataset overrides aside).
            if source not in posture_cache:
                posture_cache[source] = (_install_posture(conn, source, BUNDLED_REGISTRY.get(source))["effective_default"]
                                         if isinstance(source, str) else None)
            roles[record_role({**row, "_table": table}, table=table, posture=posture_cache[source])] += 1
        entry.update({"windows": dict(windows), "undated": undated, "future": future,
                      "writer_class": dict(writers), "record_role": dict(roles),
                      "sources": {s: dict(c) for s, c in per_source.items()},
                      "source_posture": {s: _install_posture(conn, s, BUNDLED_REGISTRY.get(s))
                                         for s in {r[0] for r in conn.execute(f"SELECT DISTINCT {source_col} FROM {table}")}
                                         if isinstance(s, str)} if source_col else
                      {table: _install_posture(conn, table, BUNDLED_REGISTRY.get(table))}})
        entry["source_posture"] = {vocab.source(s): v for s, v in entry["source_posture"].items()}
        out[table] = entry
    return out


def ids_of(conn, table) -> set:
    id_col = TABLES[table][0]
    return {r[0] for r in conn.execute(f"SELECT {id_col} FROM {table}")}


def derived_citations(conn, id_sets: dict) -> dict:
    """Facts, goals, edges, mentions, embeddings and other signal objects that cite each table's rows."""
    present = _tables_present(conn)
    out = {t: collections.Counter() for t in id_sets}
    ref_tables = collections.Counter()
    for otype, refs, valid_to in conn.execute("SELECT object_type, source_refs_json, valid_to FROM signal_objects"):
        try:
            refs = json.loads(refs or "[]")
        except ValueError:
            continue
        if not isinstance(refs, list):
            continue
        rids = {str(r.get("record_id")) for r in refs if isinstance(r, dict)}
        rtabs = {r.get("table") for r in refs if isinstance(r, dict)}
        state = "current" if valid_to is None else "closed"
        for table, ids in id_sets.items():
            if rids & ids or table in rtabs:
                key = "fact" if otype == "fact" else "other_signal_object"
                out[table][f"{key}:{state}"] += 1
                if otype != "fact":
                    out[table][f"other:{otype if otype in ('AvailabilityWindow','PlaceContext','RelationshipEdge','Goal','person_reading','activity_tags','ExperienceNode','SkillNode') else 'misc'}"] += 1
        for t in rtabs:
            ref_tables[t if t in TABLES or t in ("signal_objects", "entities", "entity_edges") else "other"] += 1
    if "user_goals" in present:
        goal_ids = {}
        for goal_id, rid in conn.execute("SELECT goal_id, record_id FROM user_goals"):
            for table, ids in id_sets.items():
                if rid in ids:
                    out[table]["goal"] += 1
                    goal_ids.setdefault(table, set()).add(goal_id)
        if "entity_edges" in present:
            for meta, valid_to in conn.execute("SELECT metadata_json, valid_to FROM entity_edges WHERE edge_type='pursues'"):
                try:
                    src = json.loads(meta or "{}").get("source_object_id")
                except (ValueError, AttributeError):
                    continue
                for table, gids in goal_ids.items():
                    if src in gids and valid_to is None:
                        out[table]["pursues_edge"] += 1
    if "entity_mentions" in present:
        for ctab, n in conn.execute("SELECT canonical_table, count(*) FROM entity_mentions GROUP BY 1"):
            if ctab in out:
                out[ctab]["entity_mentions"] = n
        for ctab, n in conn.execute("SELECT canonical_table, count(DISTINCT record_id) FROM entity_mentions GROUP BY 1"):
            if ctab in out:
                out[ctab]["records_with_mentions"] = n
    if "message_entities" in present:
        for rid, in conn.execute("SELECT DISTINCT record_id FROM message_entities"):
            for table, ids in id_sets.items():
                if rid in ids:
                    out[table]["records_with_message_entities"] += 1
    if "signal_embeddings" in present:
        for rid, model in conn.execute("SELECT DISTINCT record_id, model FROM signal_embeddings"):
            for table, ids in id_sets.items():
                if rid in ids:
                    out[table]["records_with_vectors"] += 1
    return {t: dict(c) for t, c in out.items()}, dict(ref_tables)


# --- journal: guards over records, facts, goals, relationships -----------------------------------

def _boundary_hit(boundary, table, row, record_id, source_id) -> str:
    """'hit', 'clear' or 'unavailable': the Off-limits closure's own row match plus the mention link."""
    from topos.permissions_v2.canonical import PolicyError
    if not boundary.active:
        return "inactive"
    try:
        if boundary._hits(row) or boundary._linked(record_id, table, source_id):
            return "hit"
        return "clear"
    except PolicyError:
        return "unavailable"


def journal_records(conn, now_s: int, boundary, vocab: Vocabulary, index_model: str | None) -> dict:
    from topos.disclosure.content_policy import is_record_nsfw
    from topos.permissions_v2.entailment_grounding import SPECIAL, tokens, stem
    from topos.permissions_v2.evidence import _COPY_COUNT
    from topos.permissions_v2.search_index import tokenize
    columns = _columns(conn, "journal_entries")
    rows = [dict(zip(columns, r)) for r in conn.execute("SELECT * FROM journal_entries")]
    per_source = collections.defaultdict(collections.Counter)
    gates = collections.defaultdict(collections.Counter)
    lengths, token_counts = collections.defaultdict(list), collections.defaultdict(list)
    content_hashes = collections.Counter(cs.sha256_text(r.get("content") or "") for r in rows)
    vectors = {}
    if index_model and "signal_embeddings" in _tables_present(conn):
        for rid, n in conn.execute("SELECT record_id, count(*) FROM signal_embeddings WHERE model=? AND source_id IN (?,?,?) GROUP BY 1",
                                   (index_model, *CODE_SOURCES)):
            vectors[rid] = n
    index_members = {name: {"members": 0, "tokens": 0, "distinct_terms": 0, "vector_chunks": 0, "content_bytes": 0}
                     for name in WINDOWS}
    for row in rows:
        s = vocab.source(row.get("source_id"))
        c = per_source[s]
        c["rows"] += 1
        age = _age_days(now_s, row.get("entry_at"), table="journal_entries")
        flags = _window_flags(age)
        for name, hit in flags.items():
            c[name] += hit
        content = row.get("content")
        text_ok = isinstance(content, str) and content.strip() and len(content) <= 100_000
        lengths[s].append(len(content) if isinstance(content, str) else 0)
        toks = tokenize(content) if isinstance(content, str) else []
        token_counts[s].append(len(toks))
        # The gates a permitted message passes today, applied to the journal row (labels excluded: no assessment yet).
        g = {"dated": age is not None, "text": bool(text_ok), "not_nsfw": not is_record_nsfw(row),
             "over_8000": isinstance(content, str) and len(content) > 8000}
        hit = _boundary_hit(boundary, "journal_entries", row, row.get("entry_id"), row.get("source_id"))
        g["offlimits_" + hit] = True
        g["offlimits_clear"] = hit in ("clear", "inactive")
        g["owner_only"] = conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table='journal_entries' AND record_id=?",
                                       (row.get("entry_id"),)).fetchone() is not None
        g["excluded"] = conn.execute("SELECT 1 FROM intelligence_exclusions WHERE artifact_type='record' AND artifact_key=?",
                                     (row.get("entry_id"),)).fetchone() is not None
        copies_here = content_hashes[cs.sha256_text(content or "")] if isinstance(content, str) else 0
        copies_msgs = 0
        if isinstance(content, str) and content:
            for t in ("conversation_messages", "ai_chat_messages"):
                copies_msgs += conn.execute(_COPY_COUNT.format(table=t), (content,)).fetchone()[0]
        g["independent_copy"] = copies_here > 1 or copies_msgs > 0
        words = tokens(content) if isinstance(content, str) else []
        g["special_cue"] = bool(SPECIAL & set(words) or SPECIAL & {stem(w) for w in words})
        g["home_cue"] = bool(isinstance(content, str) and re.search(r"\b(?:my|our|the) (?:rent|mortgage|apartment|housing)\b", content, re.I))
        g["question_mark"] = isinstance(content, str) and "?" in content
        candidate = (g["dated"] and g["text"] and g["not_nsfw"] and not g["over_8000"] and g["offlimits_clear"]
                     and not g["owner_only"] and not g["excluded"] and not g["independent_copy"])
        for key, value in g.items():
            gates[s][key] += bool(value)
        for name, inside in flags.items():
            if inside:
                gates[s][f"candidate_no_label:{name}"] += candidate
                if candidate:
                    m = index_members[name]
                    m["members"] += 1
                    m["tokens"] += len(toks)
                    m["distinct_terms"] += len(set(toks))
                    m["vector_chunks"] += vectors.get(row.get("entry_id"), 0)
                    m["content_bytes"] += len(content.encode("utf-8")) if isinstance(content, str) else 0
        gates[s]["has_vector"] += row.get("entry_id") in vectors
    stats = {}
    for s in per_source:
        L, T = lengths[s], token_counts[s]
        stats[s] = {"chars_mean": round(statistics.mean(L), 1) if L else 0, "chars_median": statistics.median(L) if L else 0,
                    "chars_max": max(L) if L else 0, "tokens_mean": round(statistics.mean(T), 1) if T else 0,
                    "tokens_max": max(T) if T else 0}
    return {"per_source": {s: dict(c) for s, c in per_source.items()}, "gates": {s: dict(c) for s, c in gates.items()},
            "text_stats": stats, "index_if_members": index_members,
            "distinct_content_hashes": len(content_hashes), "vector_model": index_model}


def journal_typed(conn, now_s: int, boundary, vocab: Vocabulary) -> dict:
    """Facts, goals and relationships citing journal entries through today's release guards (labels excluded)."""
    from topos.disclosure.content_policy import is_record_nsfw
    from topos.permissions_v2 import entailment_grounding as eg
    from topos.permissions_v2.canonical import PolicyError
    from topos.permissions_v2.evidence import SHAREABLE_DISCLOSURES, implicit_labels
    from topos.permissions_v2.exclusion_floor import exclusions, fact_excluded
    from topos.permissions_v2.identity import ATTESTED_CONTRACT, attested_self, permit_subjects, restriction_subjects
    from topos.permissions_v2.knowledge_projections import PREDICATE_TEXT, _goal_stated
    from topos.permissions_v2.native_claim_grounding import explicitly_states_claim
    from topos.permissions_v2.permitted_derivation import refusal as lane_refusal, Spec
    from topos.permissions_v2.predicate_classes import CLASSES, excluded_reason, scalar
    columns = _columns(conn, "journal_entries")
    journal = {}
    for r in conn.execute("SELECT * FROM journal_entries"):
        row = dict(zip(columns, r))
        journal[row["entry_id"]] = row
    attested = permit_subjects(conn, contract=ATTESTED_CONTRACT)
    owner_spellings = restriction_subjects(conn)
    self_attested = attested_self(conn) is not None
    tombstones = exclusions(conn)

    def journal_ok(row):
        age = _age_days(now_s, row.get("entry_at"), table="journal_entries")
        flags = _window_flags(age)
        hit = _boundary_hit(boundary, "journal_entries", row, row.get("entry_id"), row.get("source_id"))
        return flags, {"not_nsfw": not is_record_nsfw(row), "offlimits_clear": hit in ("clear", "inactive"),
                       "owner_only": conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table='journal_entries' AND record_id=?",
                                                  (row.get("entry_id"),)).fetchone() is not None,
                       "content_le_8000": isinstance(row.get("content"), str) and len(row["content"]) <= 8000}

    # ---- facts -----------------------------------------------------------------------------
    facts = collections.Counter()
    fact_class = collections.Counter()
    fact_predicates = collections.Counter()
    guard_codes = collections.Counter()
    fact_by_window = {name: collections.Counter() for name in WINDOWS}
    for object_id, payload_json, refs_json, dim in conn.execute(
            "SELECT object_id, payload_json, source_refs_json, signal_dimension FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"):
        try:
            payload, refs = json.loads(payload_json or "{}"), json.loads(refs_json or "[]")
        except ValueError:
            continue
        cited = [journal[str(r.get("record_id"))] for r in refs if isinstance(r, dict) and str(r.get("record_id")) in journal]
        if not cited:
            continue
        facts["cites_journal"] += 1
        predicate = payload.get("predicate")
        pname = predicate if predicate in vocab.words else "free_form"
        fact_predicates[pname] += 1
        domains, sensitivity = implicit_labels(payload, dim)
        reason = excluded_reason(predicate) if isinstance(predicate, str) else None
        klass = ("supported_not_special" if predicate in PREDICATE_TEXT and sensitivity in ("none", "personal") and "health" not in domains
                 else "supported_special" if predicate in PREDICATE_TEXT
                 else f"excluded_{reason}" if reason else "unsupported")
        fact_class[klass] += 1
        value = scalar(predicate, payload) if predicate in CLASSES else payload.get("object_value")
        subject = payload.get("subject_entity_id")
        fact_row = {"object_id": object_id, "payload_json": payload_json, "source_refs_json": refs_json, "object_type": "fact"}
        try:
            fact_offlimits = boundary.active and boundary.legacy_veto("signal_objects", fact_row)
        except PolicyError:
            fact_offlimits = True
        try:
            excluded = fact_excluded(payload, tombstones["fact"], owner_spellings)
        except PolicyError:
            excluded = True
        g = {"disclosure": payload.get("disclosure") in SHAREABLE_DISCLOSURES,
             "predicate_supported": predicate in PREDICATE_TEXT,
             "class_releasable": klass == "supported_not_special",
             "subject_attested_today": subject in attested,
             "subject_is_owner_spelling": subject in owner_spellings,
             "value_text": isinstance(value, str),
             "fact_offlimits_clear": not fact_offlimits, "not_excluded": not excluded,
             "asserted_by_owner": payload.get("asserted_by") == "owner",
             "one_cited_journal_row": len(cited) == 1}
        # Lane admission shape (permitted_derivation.refusal), as a proxy for "atomic scalar value".
        code = lane_refusal(Spec("fact", predicate if isinstance(predicate, str) else "", value if isinstance(value, str) else ""), boundary) \
            if predicate in CLASSES else "predicate_unclassed"
        g["lane_shape_ok"] = code is None
        for key, ok in g.items():
            facts[key] += bool(ok)
        base = all(g[k] for k in ("disclosure", "predicate_supported", "class_releasable", "value_text",
                                  "fact_offlimits_clear", "not_excluded", "asserted_by_owner"))
        for row in cited:
            flags, jg = journal_ok(row)
            content = row.get("content")
            fullmatch = isinstance(value, str) and explicitly_states_claim(content, predicate, value)
            verbatim = isinstance(value, str) and isinstance(content, str) and value.casefold() in content.casefold()
            claim = eg.fact_claim(predicate, value) if predicate in PREDICATE_TEXT and isinstance(value, str) else None
            gcode = eg.guard_failure(claim, content, author_is_owner=True, subject_attested=True, boundary=boundary,
                                     waive=eg.OWNER_WAIVABLE) if claim is not None else "no_claim"
            guard_codes[gcode or "pass"] += 1
            support = jg["not_nsfw"] and jg["offlimits_clear"] and not jg["owner_only"] and jg["content_le_8000"]
            for name, inside in flags.items():
                if not inside:
                    continue
                c = fact_by_window[name]
                c["cited_in_window"] += 1
                c["support_ok"] += support
                c["base_and_support"] += base and support
                c["releasable_today:fullmatch"] += base and support and fullmatch and g["subject_attested_today"]
                c["with_attestation:fullmatch"] += base and support and fullmatch and g["subject_is_owner_spelling"]
                c["with_attestation:verbatim"] += base and support and verbatim and g["subject_is_owner_spelling"]
                c["with_attestation:od38_guards_pass"] += base and support and gcode is None and g["subject_is_owner_spelling"]
                c["with_attestation:od38_guards_pass_or_fullmatch"] += base and support and (gcode is None or fullmatch) and g["subject_is_owner_spelling"]
                c["class_releasable_in_window"] += g["class_releasable"]
                c["special_class_in_window"] += klass == "supported_special" or klass == "excluded_special_category"
    # ---- goals -----------------------------------------------------------------------------
    goals = collections.Counter()
    goal_by_window = {name: collections.Counter() for name in WINDOWS}
    goal_guards = collections.Counter()
    goal_sources = collections.Counter()
    passing_goals = {name: set() for name in WINDOWS}
    present = _tables_present(conn)
    if "user_goals" in present:
        for goal_id, rid, source_id, text in conn.execute("SELECT goal_id, record_id, source_id, goal_text FROM user_goals"):
            row = journal.get(rid)
            if row is None:
                continue
            goals["cites_journal"] += 1
            goal_sources[vocab.source(source_id)] += 1
            flags, jg = journal_ok(row)
            content = row.get("content")
            stated = _goal_stated(content, text)
            verbatim = isinstance(text, str) and isinstance(content, str) and text.casefold() in content.casefold()
            shape = lane_refusal(Spec("goal", "goal", text if isinstance(text, str) else ""), boundary)
            claim = eg.goal_claim(text) if isinstance(text, str) else None
            gcode = (eg.guard_failure(claim, content, author_is_owner=True, subject_attested=True, boundary=boundary,
                                      waive=eg.OWNER_WAIVABLE) if claim is not None else "no_claim")
            goal_guards[gcode or "pass"] += 1
            goals["shape_ok"] += shape is None
            goals["fullmatch"] += bool(stated)
            goals["verbatim"] += bool(verbatim)
            support = jg["not_nsfw"] and jg["offlimits_clear"] and not jg["owner_only"] and jg["content_le_8000"]
            for name, inside in flags.items():
                if not inside:
                    continue
                c = goal_by_window[name]
                c["cited_in_window"] += 1
                c["support_ok"] += support
                c["releasable_today:fullmatch"] += support and stated and self_attested
                c["with_attestation:fullmatch"] += support and stated
                c["with_attestation:verbatim"] += support and verbatim
                c["with_attestation:od38_guards_pass"] += support and gcode is None
                c["with_attestation:od38_guards_pass_or_fullmatch"] += support and (gcode is None or stated)
                if support and (gcode is None or stated):
                    passing_goals[name].add(goal_id)
    relationships = {name: collections.Counter() for name in WINDOWS}
    if "entity_edges" in present and "user_goals" in present:
        journal_goal_ids = {gid for gid, rid in conn.execute("SELECT goal_id, record_id FROM user_goals") if rid in journal}
        for meta, valid_to in conn.execute("SELECT metadata_json, valid_to FROM entity_edges WHERE edge_type='pursues'"):
            try:
                src = json.loads(meta or "{}").get("source_object_id")
            except (ValueError, AttributeError):
                continue
            for name in WINDOWS:
                relationships[name]["from_journal_goal"] += src in journal_goal_ids and valid_to is None
                relationships[name]["with_attestation:od38_guards_pass_or_fullmatch"] += src in passing_goals[name] and valid_to is None
    return {"facts": dict(facts), "fact_predicates": dict(fact_predicates), "fact_class": dict(fact_class),
            "fact_od38_guard_codes": dict(guard_codes), "fact_by_window": {k: dict(v) for k, v in fact_by_window.items()},
            "goals": dict(goals), "goal_sources": dict(goal_sources), "goal_od38_guard_codes": dict(goal_guards),
            "goal_by_window": {k: dict(v) for k, v in goal_by_window.items()},
            "relationships": {k: dict(v) for k, v in relationships.items()},
            "attested_subjects_ledger": int(self_attested), "permit_subjects_attested_contract": len(attested),
            "owner_spellings": len(owner_spellings)}


# --- browser visits ---------------------------------------------------------------------------

def browser(conn, now_s: int, boundary, vocab: Vocabulary) -> dict:
    present = _tables_present(conn)
    out = {"raw_record_columns": _columns(conn, "browser_visits") if "browser_visits" in present else []}
    vocab.allow(*out["raw_record_columns"])   # schema column names are code identifiers
    if "browser_visits" not in present:
        return out
    hosts, visits_in = collections.Counter(), collections.Counter()
    distinct = {name: {"url": set(), "hostname": set(), "title": set()} for name in WINDOWS}
    offlimits = collections.Counter()
    for r in conn.execute("SELECT record_id, url, hostname, title, visited_at, incognito FROM browser_visits"):
        rid, url, host, title, at, incognito = r
        flags = _window_flags(_age_days(now_s, at, table="browser_visits"))
        hosts[host] += 1
        for name, inside in flags.items():
            if inside:
                visits_in[name] += 1
                distinct[name]["url"].add(url)
                distinct[name]["hostname"].add(host)
                distinct[name]["title"].add(title)
        offlimits[_boundary_hit(boundary, "browser_visits", {"url": url, "hostname": host, "title": title}, rid, "browser_visits")] += 1
    out.update({"visits": dict(visits_in), "distinct_in_window": {n: {k: len(v) for k, v in d.items()} for n, d in distinct.items()},
                "hosts_total": len(hosts), "hosts_ge_10_visits": sum(1 for v in hosts.values() if v >= 10),
                "hosts_ge_100_visits": sum(1 for v in hosts.values() if v >= 100),
                "top_host_share": round(max(hosts.values()) / max(sum(hosts.values()), 1), 3) if hosts else 0,
                "incognito": list(conn.execute("SELECT sum(incognito=1), sum(incognito IS NULL) FROM browser_visits").fetchone()),
                "offlimits_title_url": dict(offlimits)})
    visit_ids = {r[0] for r in conn.execute("SELECT event_id FROM activity_events WHERE source_id='browser_visits'")} if "activity_events" in present else set()
    derived = collections.Counter()
    for otype, refs, valid_to in conn.execute("SELECT object_type, source_refs_json, valid_to FROM signal_objects WHERE valid_to IS NULL"):
        try:
            refs = json.loads(refs or "[]")
        except ValueError:
            continue
        if isinstance(refs, list) and any(isinstance(x, dict) and (str(x.get("record_id")) in visit_ids or x.get("table") == "activity_events") for x in refs):
            derived[otype if otype in ("activity_tags", "fact", "top_topics", "interest_profile") else "other"] += 1
    if "topic_cluster_members" in present:
        derived["topic_cluster_members"] = conn.execute("SELECT count(*) FROM topic_cluster_members WHERE source_id='browser_visits'").fetchone()[0]
        derived["topic_clusters_with_visits"] = conn.execute("SELECT count(DISTINCT cluster_id) FROM topic_cluster_members WHERE source_id='browser_visits'").fetchone()[0] \
            if "cluster_id" in _columns(conn, "topic_cluster_members") else -1
    if "entity_mentions" in present:
        derived["entity_mentions"] = conn.execute("SELECT count(*) FROM entity_mentions WHERE canonical_table='activity_events'").fetchone()[0]
        derived["records_with_mentions"] = conn.execute("SELECT count(DISTINCT record_id) FROM entity_mentions WHERE canonical_table='activity_events'").fetchone()[0]
        derived["distinct_entities_mentioned"] = conn.execute("SELECT count(DISTINCT entity_id) FROM entity_mentions WHERE canonical_table='activity_events'").fetchone()[0]
        derived["person_entities_mentioned"] = conn.execute(
            "SELECT count(DISTINCT m.entity_id) FROM entity_mentions m JOIN entities e ON e.entity_id=m.entity_id "
            "WHERE m.canonical_table='activity_events' AND e.entity_type='person'").fetchone()[0]
    if "signal_embeddings" in present:
        derived["records_with_vectors"] = conn.execute("SELECT count(DISTINCT record_id) FROM signal_embeddings WHERE source_id='browser_visits'").fetchone()[0]
    if "user_goals" in present:
        derived["goals"] = sum(1 for (rid,) in conn.execute("SELECT record_id FROM user_goals") if rid in visit_ids)
    out["derived"] = dict(derived)
    return out


# --- index sizing ---------------------------------------------------------------------------------

def index_sizing(copy_root: Path) -> dict:
    import glob
    out = {}
    for path in glob.glob(str(copy_root / "permissions-v2" / "message-search" / "grant-*.db")):
        conn = cs.ro(Path(path), immutable=True)
        try:
            meta = conn.execute("SELECT state, model, dims, member_count FROM meta WHERE singleton=1").fetchone()
            terms = conn.execute("SELECT sum(length(terms_json)), sum(length(sealed)), sum(doc_len) FROM members").fetchone()
            vectors = conn.execute("SELECT count(*), sum(length(vector)) FROM vectors").fetchone()
            out = {"state": meta[0], "model": meta[1], "dims": meta[2], "members": meta[3], "file_bytes": Path(path).stat().st_size,
                   "terms_json_bytes": terms[0], "sealed_bytes": terms[1], "doc_len_sum": terms[2],
                   "vector_rows": vectors[0], "vector_bytes": vectors[1],
                   "bytes_per_member": round(Path(path).stat().st_size / max(meta[3], 1))}
        finally:
            conn.close()
    return out


# --- optional machine assessment (local model window only) -------------------------------------

def assess(conn, boundary, *, limit, stop_file: Path | None, max_seconds: int, vocab: Vocabulary, now_s: int) -> dict:
    """The node's rubric over journal text: PROMPT + rubric, pinned model at the loopback host, no neighbours.
    Tallies only. Nothing is written."""
    import asyncio
    from topos.permissions_v2.automatic_message_review import PROMPT, classification_rubric, apply_floors
    from topos.permissions_v2.message_evidence import DOMAINS
    from topos.permissions_v2.shadow_labeler_local import MODEL, ORIGIN, open_transport, MAX_TEXT_CHARS
    from topos.permissions_v2.canonical import parse_json, PolicyError
    from topos.permissions_v2.message_review_contract import MessageClassification
    terms = sorted(boundary.terms | boundary.handles) if boundary.active else []
    columns = _columns(conn, "journal_entries")
    rows = [dict(zip(columns, r)) for r in conn.execute("SELECT * FROM journal_entries ORDER BY entry_at DESC")]
    if limit != "all":
        rows = rows[:int(limit)]
    tally = {"domains": collections.Counter(), "sensitivity": collections.Counter(), "speech": collections.Counter(),
             "protected_content": collections.Counter(), "policy_clark": collections.Counter(),
             "by_source_sensitivity": collections.Counter(), "by_window_sensitivity": collections.Counter(),
             "outcome": collections.Counter(), "special_by_domain": collections.Counter()}
    started = time.monotonic()

    class _Labels:  # the fields apply_floors reads; a MessageClassification needs a message identity a journal row lacks
        def __init__(self, d): self.domains, self.sensitivity, self.protected_content = d["domains"], d["sensitivity"], d["protected_content"]
        def model_copy(self, update): return _Labels({"domains": update["domains"], "sensitivity": update["sensitivity"], "protected_content": update["protected_content"]})

    async def run():
        client = open_transport(base_url=ORIGIN)
        failures = 0
        try:
            await client.verify()
            for row in rows:
                if stop_file is not None and stop_file.exists():
                    tally["outcome"]["stopped"] += 1
                    break
                if time.monotonic() - started > max_seconds:
                    tally["outcome"]["timed_out"] += 1
                    break
                content = row.get("content")
                if not isinstance(content, str) or not content.strip() or len(content) > MAX_TEXT_CHARS:
                    tally["outcome"]["skipped_shape"] += 1
                    continue
                try:
                    response = await client.client.post(client.base_url + "/api/chat", timeout=60, json={
                        "model": MODEL, "stream": False, "think": False, "format": "json",
                        "options": {"temperature": 0, "num_predict": 512},
                        "messages": [{"role": "system", "content": PROMPT + "\n" + classification_rubric()},
                                     {"role": "user", "content": json.dumps({"target": content, "before": [], "after": [],
                                                                             "protected_terms": terms}, ensure_ascii=False)}]})
                    response.raise_for_status()
                    body = response.json()
                    if body.get("model") != MODEL or body.get("done") is not True:
                        raise PolicyError("machine_classification_incomplete")
                    value = parse_json((body.get("message") or {}).get("content"))
                    if (not isinstance(value, dict) or set(value) != {"domains", "sensitivity", "speech", "protected_content"}
                            or not isinstance(value["domains"], list) or not value["domains"]
                            or any(d not in DOMAINS for d in value["domains"])
                            or value["sensitivity"] not in ("none", "personal", "special", "unknown")
                            or value["speech"] not in ("original_message", "third_party_quote", "mixed", "unknown")
                            or value["protected_content"] not in ("none", "present", "unknown")):
                        raise PolicyError("machine_classification_invalid")
                    labels = apply_floors(_Labels(value), {"target": content, "before": [], "after": [], "protected_terms": terms})
                    failures = 0
                except Exception:  # noqa: BLE001 -- counted, never printed
                    tally["outcome"]["failed"] += 1
                    failures += 1
                    if failures >= 3:
                        tally["outcome"]["classifier_unavailable"] += 1
                        break
                    continue
                tally["outcome"]["assessed"] += 1
                for d in labels.domains:
                    tally["domains"][d] += 1
                    if labels.sensitivity == "special":
                        tally["special_by_domain"][d] += 1
                tally["sensitivity"][labels.sensitivity] += 1
                tally["speech"][value["speech"]] += 1
                tally["protected_content"][labels.protected_content] += 1
                permit = labels.sensitivity in ("none", "personal") and labels.protected_content == "none" and value["speech"] == "original_message"
                tally["policy_clark"]["permit" if permit else "withhold"] += 1
                s = vocab.source(row.get("source_id"))
                tally["by_source_sensitivity"][f"{s}:{labels.sensitivity}"] += 1
                for name, inside in _window_flags(_age_days(now_s, row.get("entry_at"), table="journal_entries")).items():
                    if inside:
                        tally["by_window_sensitivity"][f"{name}:{labels.sensitivity}"] += 1
        finally:
            await client.client.aclose()
    asyncio.run(run())
    return {k: dict(v) for k, v in tally.items()} | {"rows_offered": len(rows), "protected_terms": len(terms),
                                                     "seconds": round(time.monotonic() - started, 1)}


# --- main -----------------------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--copy", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--assess", default=None, help="N or all: run the rubric over journal entries (local-model window only)")
    parser.add_argument("--stop-file", type=Path, default=None)
    parser.add_argument("--max-seconds", type=int, default=2400)
    args = parser.parse_args(argv)
    cs.require_scratch_environment()
    copy_root = cs.refuse_live(args.copy.expanduser().absolute())
    manifest = json.loads((copy_root / "census-copy-manifest.json").read_text())
    if not manifest["consistency"]["consistent"]:
        raise cs.CensusRefused("copy_not_consistent")
    now_s = int(manifest["copied_at"])
    vocab = Vocabulary()
    from topos.permissions_v2.entity_boundary import EntityBoundary
    conn = cs.ro(copy_root / "database.db", immutable=True)
    try:
        boundary = EntityBoundary(conn)
        report = {"schema": "od50-journal-browser/v1", "copy": {"run_id": manifest["run_id"], "copied_at_utc": manifest["copied_at_utc"]},
                  "offlimits_boundary": {"active": boundary.active, "ids": len(boundary.ids), "terms": len(boundary.terms),
                                         "handles": len(boundary.handles), "contacts": len(boundary.contacts)}}
        if args.assess:
            report["assessment"] = assess(conn, boundary, limit=args.assess, stop_file=args.stop_file,
                                          max_seconds=args.max_seconds, vocab=vocab, now_s=now_s)
        else:
            report["tables"] = inventory(conn, now_s, vocab)
            present = _tables_present(conn)
            id_sets = {t: ids_of(conn, t) for t in TABLES if t in present}
            id_sets["browser_visits_as_activity"] = {r[0] for r in conn.execute("SELECT event_id FROM activity_events WHERE source_id='browser_visits'")} if "activity_events" in present else set()
            vocab.allow("browser_visits_as_activity")
            report["derived_by_table"], report["fact_reference_tables"] = derived_citations(conn, id_sets)
            sizing = index_sizing(copy_root)
            report["index_now"] = sizing
            if sizing.get("model"):
                vocab.allow(sizing["model"])   # the node's embedding model name, a configuration constant
            report["journal_records"] = journal_records(conn, now_s, boundary, vocab, sizing.get("model"))
            report["journal_typed"] = journal_typed(conn, now_s, boundary, vocab)
            report["browser"] = browser(conn, now_s, boundary, vocab)
            report["time_parse"] = dict(TIME_PARSE)
    finally:
        conn.close()
    vocab.allow("schema", "copy", "run_id", "copied_at_utc", "od50-journal-browser/v1")
    cleaned = _clean_values_keep_keys(report, vocab)
    out = cs.refuse_live(args.out.expanduser().absolute())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cleaned, sort_keys=True, indent=1))
    print(json.dumps({"written": str(out.name), "schema": cleaned["schema"], "assessment": bool(args.assess)}))
    return 0


def _clean_values_keep_keys(value, vocab: Vocabulary):
    """Keys are this script's labels or vocabulary words (sources are mapped through vocab.source before use);
    values go through the vocabulary. A key that is not a vocabulary word is replaced by its length."""
    if isinstance(value, dict):
        return {(k if (k in vocab.words or Vocabulary._TOKEN.match(k) and _label_like(k)) else f"<len={len(k)}>"):
                _clean_values_keep_keys(v, vocab) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean_values_keep_keys(v, vocab) for v in value]
    return vocab.clean(value)


def _label_like(key: str) -> bool:
    """Script labels: snake_case words, `levers:...`, `<name>:<value>` pairs of vocabulary words, window names."""
    parts = re.split(r"[:]", key)
    return len(key) <= 48 and all(re.match(r"^[A-Za-z0-9_.\-]+$", p) for p in parts)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except cs.CensusRefused as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        sys.exit(2)
