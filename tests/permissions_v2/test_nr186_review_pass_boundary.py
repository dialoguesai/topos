"""NR186: an automatic-review pass keeps one Off-limits boundary for its rows, proven current at each row.

`review_pass_boundary` (switch `TOPOS_PERMISSIONS_V2_REVIEW_PASS_BOUNDARY`, off). What is pinned here:

- **Equivalence.** On an invented corpus (`review_pass_corpus`), a pass with the switch on decides exactly what one
  with it off decides: every `prepare` the pass makes (each row's, and `publish`'s own) equals a `prepare` made at the
  same moment with no pass (a fresh boundary), and the two whole passes, run on byte copies at the same path, make the
  same status updates, show the classifier the same inputs and publish the same assessments. Without a change, and
  with each kind of change between two rows (`review_pass_corpus.CHANGES`).
- **The comparison is not vacuous.** With the currency check skipped, every change that moves a decision is found by
  the same comparison; each change of what the boundary reads makes the pass build again.
- **Freshness.** `unchanged_on` sees each read the construction made; construction reads only through `_table`; the
  token is read before a read's snapshot (a commit landing during the first build is seen at the next row); what
  cannot be read is built afresh; a read not opened inside a row keys no token.
- **Scope.** Off by default (bound and unbound), and then nothing is made; one keeper per pass, on its own thread and
  resolver; at the end nothing is kept (no module, class or function holds a boundary or a protected name; the forms
  memory is emptied and marked ended; a copied context hands out nothing).
"""
from __future__ import annotations

import asyncio
import contextvars
import sqlite3
import threading
import types

import pytest

from tests.permissions_v2 import review_pass_corpus as rc
from tests.permissions_v2.message_search_harness import owner
from topos.permissions_v2 import automatic_message_review as amr
from topos.permissions_v2 import automatic_review_worker as arw
from topos.permissions_v2 import entity_boundary as eb
from topos.permissions_v2 import evidence, review_pass_boundary as rpb, search_index, switches
from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
from topos.permissions_v2.canonical import PolicyError

SWITCH = "TOPOS_PERMISSIONS_V2_REVIEW_PASS_BOUNDARY"
#: Before which scanned row (1-based) a change is written: rows 1-5 run on the boundary as built, 6-12 after it.
AT = 6
#: Changes of what the boundary reads that also move some decision (an unrelated entity moves none: the pass builds
#: again, conservatively, and decides the same).
DETECTING = sorted(rc.BOUNDARY_CHANGES - {"unrelated_entity"})
PROTECTED_WORDS = ("mara example", "zeb quill", "olwen fairly", "old mara name", "marabel.example@example.org")


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")


@pytest.fixture
def keepers(monkeypatch):
    """Every keeper a pass makes, in order."""
    made = []
    original = rpb.ReviewPassBoundary

    class Seen(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)
    monkeypatch.setattr(rpb, "ReviewPassBoundary", Seen)
    return made


@pytest.fixture
def corpus(tmp_path):
    return rc.build(tmp_path / "corpus", preassess=(0, 2, 4))


def _comparable(call):
    """("value", what the decision is made of) or ("raised", the refusal's code), never the exception object."""
    try:
        prepared = call()
    except PolicyError as exc:
        return ("raised", exc.code), None, exc
    except Exception as exc:  # noqa: BLE001 -- compared by type: a non-refusal must match too
        return ("raised", type(exc).__name__), None, exc
    legacy = prepared.get("legacy")
    if legacy is not None:
        try:
            old = legacy()
            legacy = (old["snapshot"].model_dump(), old["context_revision"])
        except PolicyError as exc:
            legacy = ("raised", exc.code)
    return ("value", {"snapshot": prepared["snapshot"].model_dump(), "context_revision": prepared["context_revision"],
                      "owner_review_revision": prepared["owner_review_revision"],
                      "existing_revision": prepared["existing_revision"], "legacy": legacy,
                      "input": prepared["input"]}), prepared, None


def _shadow(monkeypatch, comparisons):
    """Every `prepare` made inside a pass's row is compared with one made just before it with no pass current."""
    real = amr.prepare

    def prepare(resolver, reviews, identity):
        if rpb._CURRENT.get() is None:
            return real(resolver, reviews, identity)
        token = rpb._CURRENT.set(None)
        try:
            reference = _comparable(lambda: real(resolver, reviews, identity))[0]
        finally:
            rpb._CURRENT.reset(token)
        got, prepared, error = _comparable(lambda: real(resolver, reviews, identity))
        comparisons.append((identity.record_id, got, reference))
        if error is not None:
            raise error
        return prepared
    monkeypatch.setattr(amr, "prepare", prepare)      # publish's own prepare
    monkeypatch.setattr(arw, "prepare", prepare)      # the worker's


def run_pass(corpus, *, change=None, at=AT, on_change=None, refresh=False):
    """One worker pass over the corpus's window, with `change` written before scanned row `at`. What it decided, and
    what was current between rows (the classifier's call and the owner pass's index refresh run outside any row)."""
    calls, updates, between = [], [], []

    async def classify(prepared):
        between.append(rpb._CURRENT.get())
        calls.append((prepared["snapshot"].message.identity.record_id, prepared["context_revision"], prepared["input"]))
        return rc.answer(prepared)
    worker = AutomaticReviewWorker(corpus.resolver, corpus.reviews, classifier=classify,
                                   refresh=lambda: between.append(rpb._CURRENT.get()))
    original, scanned = worker._update, [0]

    def update(**counts):
        if counts.get("scanned"):
            scanned[0] += 1
            if change is not None and scanned[0] == at:
                rc.CHANGES[change](corpus.canonical, corpus.natives)
                if on_change is not None:
                    on_change()
        updates.append(tuple(sorted(counts.items())))
        original(**counts)
    worker._update = update
    with owner():
        asyncio.run(worker._process(corpus.request, refresh=refresh))
        status = worker.status()
    published = {}
    with corpus.reviews._db() as db:
        for native in corpus.natives:
            review = corpus.reviews._current_in(db, amr.machine_key(corpus.identity(native.message_id)))
            if review is not None:
                dump = review.model_dump()
                dump.pop("review_id")
                dump.pop("reviewed_at")
                published[native.message_id] = dump
    return types.SimpleNamespace(status=status.model_dump(), updates=updates, calls=calls, published=published,
                                 scanned=scanned[0], between=between)


# --- equivalence -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [None, *rc.CHANGES])
def test_a_pass_with_the_switch_on_decides_as_one_with_it_off(tmp_path, monkeypatch, keepers, change):
    corpus = rc.build(tmp_path / "corpus", preassess=(0, 2, 4))
    kept = rc.keep(corpus, tmp_path / "kept")
    monkeypatch.delenv(SWITCH, raising=False)
    off = run_pass(corpus, change=change)
    assert keepers == []                                   # off: no keeper, every read builds its own
    corpus = rc.restore(corpus, kept)
    monkeypatch.setenv(SWITCH, "true")
    comparisons = []
    _shadow(monkeypatch, comparisons)
    on = run_pass(corpus, change=change)

    # Each prepare of the pass equals a prepare at the same moment with a fresh boundary.
    assert [c for c in comparisons if c[1] != c[2]] == []
    # The whole passes: same updates, same classifier inputs, same published assessments.
    assert on.updates == off.updates
    assert on.calls == off.calls
    assert on.published == off.published
    assert on.status == off.status
    assert set(on.between) == {None}         # nothing of the pass is current between its rows

    # Not vacuous: every scanned row compared, most of them real values, some assessed and published.
    assert off.scanned == len(corpus.natives) == 12
    assert len(comparisons) == on.scanned + len(on.calls)
    assert sum(c[1][0] == "value" for c in comparisons) >= 6
    assert len(on.calls) >= 3 and on.published
    keeper, = keepers
    counts = keeper.counts
    assert counts["built"] + counts["token"] + counts["reads"] == len(comparisons)
    if change in rc.BOUNDARY_CHANGES:
        assert counts["built"] == 2            # once at the start, once at the first row after the change
    else:
        assert counts["built"] == 1
        assert counts["reads"] == (0 if change is None else 1)   # the commit moved the token; the reads proved it
    assert keeper._closed and keeper._kept is None


@pytest.mark.parametrize("change", DETECTING)
def test_with_the_currency_check_skipped_the_comparison_finds_the_change(corpus, monkeypatch, switch_on, keepers,
                                                                          change):
    """The same comparison, against a pass that reuses its boundary whatever changed: it must differ somewhere."""
    monkeypatch.setattr(rpb.ReviewPassBoundary, "_proof", lambda self, conn, token, reads: "token")
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus, change=change)
    assert [c for c in comparisons if c[1] != c[2]], change
    assert keepers[0].counts["built"] == 1


def test_an_off_limits_entry_added_mid_pass_withholds_a_later_row_that_names_it(tmp_path, monkeypatch, switch_on):
    """The decision itself, not only its equality: a row naming the new entry, scanned after the change, is
    withheld, where the same row in a pass without the change is prepared."""
    naming = "imessage:7"   # "Zeb Quill says ...": the 10th row scanned (message ids in text order)
    comparisons, outcomes = [], {}
    _shadow(monkeypatch, comparisons)
    for change in (None, "offlimits_added"):
        comparisons.clear()
        run_pass(rc.build(tmp_path / str(change), preassess=(0, 2, 4)), change=change)
        outcomes[change] = next(got for record, got, _reference in comparisons if record == naming)
        assert [c for c in comparisons if c[1] != c[2]] == []
    assert outcomes[None][0] == "value"
    assert outcomes["offlimits_added"][0] == "raised"


# --- freshness -------------------------------------------------------------------------------------------------------

def _boundary_reads(corpus):
    db = sqlite3.connect(corpus.canonical)
    db.execute("BEGIN")
    boundary = eb.EntityBoundary(db, record=True)
    reads = tuple(boundary._reads)
    db.rollback()
    db.close()
    return boundary, reads


def _unchanged(corpus, reads) -> bool:
    db = sqlite3.connect(corpus.canonical)
    db.row_factory = sqlite3.Row   # as the resolver's reads are
    try:
        db.execute("BEGIN")
        return eb.unchanged_on(db, reads)
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("change", sorted(rc.CHANGES))
def test_unchanged_on_sees_exactly_the_reads_the_boundary_was_built_from(corpus, change):
    _boundary, reads = _boundary_reads(corpus)
    assert _unchanged(corpus, reads)
    tables = {call[0] for call, _fingerprint in reads}
    assert tables == {"entity_blackholes", "entities", "entity_merge_tombstones", "contacts", "contact_identifiers",
                      "entity_mentions"}
    rc.CHANGES[change](corpus.canonical, corpus.natives)
    assert _unchanged(corpus, reads) is (change not in rc.BOUNDARY_CHANGES), change


def test_a_mention_of_someone_unprotected_leaves_the_reads_unchanged(corpus):
    _boundary, reads = _boundary_reads(corpus)
    db = sqlite3.connect(corpus.canonical)
    db.execute("INSERT INTO entity_mentions(entity_id,canonical_table,record_id,source_id) VALUES(?,?,?,?)",
               ("owner-entity", "conversation_messages", corpus.natives[3].message_id, "imessage"))
    db.commit()
    db.close()
    assert _unchanged(corpus, reads)   # the closure reads only the protected ids' mentions, as a fresh one would


@pytest.mark.parametrize("sql", ["ALTER TABLE contacts ADD COLUMN nr186_extra TEXT",
                                 "UPDATE contacts SET display_name=display_name || ' ' WHERE contact_id='protected-contact'",
                                 "DROP TABLE contact_identifiers"])
def test_a_schema_or_value_change_or_an_unreadable_table_is_not_unchanged(corpus, sql):
    _boundary, reads = _boundary_reads(corpus)
    db = sqlite3.connect(corpus.canonical)
    db.execute(sql)
    db.commit()
    db.close()
    assert not _unchanged(corpus, reads)


def test_no_reads_or_a_read_without_a_fingerprint_is_never_unchanged(corpus):
    _boundary, reads = _boundary_reads(corpus)
    assert not _unchanged(corpus, ())
    assert not _unchanged(corpus, (*reads[:-1], (reads[-1][0], None)))
    assert eb._fingerprint(object()) is None   # what cannot be fingerprinted is None, never a raise


def test_the_fingerprint_tells_types_and_rows_apart():
    base = ([("table",)], [(0, "a", "TEXT", 0, None, 0)], ["a"])
    values = [[(1,)], [(1.0,)], [("1",)], [(b"1",)], [(None,)], [], [(1,), (1,)], [("a\nb",)], [("a",), ("b",)],
              [("a\n('b',)",)]]
    found = [eb._fingerprint((*base, rows)) for rows in values]
    assert None not in found and len(set(found)) == len(found)


def test_construction_reads_the_connection_only_through_its_table_reads(corpus):
    """`unchanged_on` replays `_table`'s reads. If construction read the connection any other way, the replay would
    not see it: so the statements a recording construction runs are exactly those its replay runs."""
    db = sqlite3.connect(corpus.canonical)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    built, replayed = [], []
    db.set_trace_callback(built.append)
    boundary = eb.EntityBoundary(db, record=True)
    db.set_trace_callback(replayed.append)
    assert eb.unchanged_on(db, tuple(boundary._reads))
    db.set_trace_callback(None)
    db.rollback()
    db.close()
    assert built and sorted(built) == sorted(replayed)
    assert len(boundary._reads) >= 7


def test_recording_changes_nothing_the_boundary_holds(corpus):
    plain = sqlite3.connect(corpus.canonical)
    plain.execute("BEGIN")
    recorded = eb.EntityBoundary(plain, record=True)
    fresh = eb.EntityBoundary(plain)
    plain.rollback()
    plain.close()
    for name in ("ids", "contacts", "terms", "handles", "name_terms", "identifier_terms", "whole_identifiers",
                 "name_parts", "name_short_words", "_groups", "mentions", "revision", "active"):
        assert getattr(recorded, name) == getattr(fresh, name), name
    assert fresh._reads is None and recorded._reads
    assert recorded.rebind(None)._reads is None


@pytest.mark.parametrize("where", ["after_the_snapshot", "during_the_build"])
def test_a_commit_after_the_first_reads_snapshot_is_seen_at_the_next_row(corpus, monkeypatch, switch_on, keepers,
                                                                         where):
    """The token is read before a read's snapshot is established (`opened`, before `_read`'s first statement). A
    commit that lands after the first row's snapshot (while `_read` checks it, or while the boundary is built on it)
    keys that boundary to the token before the commit, so the next row proves it afresh and builds again. A token
    read any later would equal the next row's and serve the boundary of before the commit."""
    fired = []

    def fire():
        if not fired and rpb._CURRENT.get() is not None:   # the pass's own first read only, not the reference's
            fired.append(1)
            rc.CHANGES["offlimits_alias"](corpus.canonical, corpus.natives)
    if where == "after_the_snapshot":
        original = evidence.EvidenceResolver._keys_pending

        def keys_pending(conn):
            fire()
            return original(conn)
        monkeypatch.setattr(evidence.EvidenceResolver, "_keys_pending", staticmethod(keys_pending))
    else:
        original = eb.EntityBoundary._term_groups

        def term_groups(self):
            if self._reads is not None:
                fire()
            return original(self)
        monkeypatch.setattr(eb.EntityBoundary, "_term_groups", term_groups)
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus)
    assert fired
    assert [c for c in comparisons if c[1] != c[2]] == []
    assert keepers[0].counts["built"] == 2


def test_a_commit_between_a_proven_reads_snapshot_and_its_token_is_seen_at_the_next_row(corpus, monkeypatch,
                                                                                        switch_on, keepers):
    """When the reads prove the boundary on a row's snapshot, it is keyed by that read's token from before its
    snapshot, not by the token `_proof` reads after it: an unrelated commit moves the token before row AT, and an
    Off-limits alias lands between row AT's snapshot and its second token read."""
    armed, real = {}, rpb.ReviewPassBoundary._token

    def token(self):
        if "calls" in armed:
            armed["calls"] += 1
            if armed["calls"] == 2:   # the second token read after the change: `_proof`'s, row AT's snapshot open
                del armed["calls"]
                armed["fired"] = True
                rc.CHANGES["offlimits_alias"](corpus.canonical, corpus.natives)
        return real(self)
    monkeypatch.setattr(rpb.ReviewPassBoundary, "_token", token)
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus, change="unrelated_commit", on_change=lambda: armed.update(calls=0))
    assert armed.get("fired")
    assert [c for c in comparisons if c[1] != c[2]] == []
    counts = keepers[0].counts
    assert counts["built"] == 2 and counts["reads"] == 1


def test_the_owner_pass_refreshes_outside_any_row(corpus, switch_on, keepers):
    result = run_pass(corpus, refresh=True)
    assert len(result.between) > len(result.calls) and set(result.between) == {None}


@pytest.mark.parametrize("change", [None, "entity_merge", "contact_renamed"])
def test_the_equivalence_holds_over_a_spine_of_people(tmp_path, monkeypatch, switch_on, keepers, change):
    corpus = rc.build(tmp_path / "spine", universe=1, preassess=(0, 2, 4))
    assert corpus.universe["entities"] == 300
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus, change=change)
    assert [c for c in comparisons if c[1] != c[2]] == []
    assert sum(c[1][0] == "value" for c in comparisons) >= 6
    assert keepers[0].counts["built"] == (1 if change is None else 2)


def test_what_cannot_be_read_is_built_afresh(corpus, monkeypatch, switch_on, keepers):
    monkeypatch.setattr(search_index.SearchVerification, "canonical_token", lambda self: None)
    monkeypatch.setattr(eb, "unchanged_on", lambda conn, reads: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus)
    assert [c for c in comparisons if c[1] != c[2]] == []
    assert keepers[0].counts == {"built": len(comparisons), "token": 0, "reads": 0}


def test_an_unreadable_token_falls_back_to_the_reads(corpus, monkeypatch, switch_on, keepers):
    monkeypatch.setattr(search_index.SearchVerification, "canonical_token", lambda self: None)
    run_pass(corpus)
    counts = keepers[0].counts
    assert counts["built"] == 1 and counts["token"] == 0 and counts["reads"] > 5


def test_a_read_opened_outside_a_row_keys_no_token(corpus, switch_on):
    keeper = rpb.ReviewPassBoundary(corpus.resolver, corpus.reviews)
    try:
        with owner():
            with corpus.resolver._read() as (conn, _floor):
                with keeper.row():
                    corpus.resolver.entity_boundary(conn)
            assert keeper._kept[1] is None and keeper.counts["built"] == 1
            with keeper.row():
                with corpus.resolver._read() as (conn, _floor):
                    corpus.resolver.entity_boundary(conn)
        # No commit since, but no token was kept: the reads prove it, and now a token is kept.
        assert keeper.counts == {"built": 1, "token": 0, "reads": 1}
        assert keeper._kept[1] is not None
    finally:
        keeper.close()


def test_a_read_outside_a_row_or_a_transaction_or_on_another_resolver_gets_its_own(corpus, tmp_path, switch_on):
    keeper = rpb.ReviewPassBoundary(corpus.resolver, corpus.reviews)
    other = rc.build(tmp_path / "other")
    try:
        with owner():
            with corpus.resolver._read() as (conn, _floor):
                assert rpb.for_read(corpus.resolver, conn) is None          # no row current
            with keeper.row():
                plain = sqlite3.connect(corpus.canonical)
                try:
                    assert rpb.for_read(corpus.resolver, plain) is None    # not in a transaction
                finally:
                    plain.close()
                with other.resolver._read() as (conn, _floor):
                    assert rpb.for_read(other.resolver, conn) is None     # another resolver
        assert keeper.counts == {"built": 0, "token": 0, "reads": 0}
    finally:
        keeper.close()


def test_another_thread_gets_its_own(corpus, switch_on):
    keeper = rpb.ReviewPassBoundary(corpus.resolver, corpus.reviews)
    found = []
    try:
        with keeper.row():
            context = contextvars.copy_context()

        def read():
            with owner():
                with keeper.row(), corpus.resolver._read() as (conn, _floor):
                    found.append(rpb.for_read(corpus.resolver, conn))
        thread = threading.Thread(target=lambda: context.run(read))
        thread.start()
        thread.join(30)
        assert found == [None] and keeper.counts["built"] == 0
    finally:
        keeper.close()


# --- scope -----------------------------------------------------------------------------------------------------------

def test_the_switch_is_off_bound_and_unbound(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    item = switches.REVIEW_PASS_BOUNDARY
    assert (item.unbound, item.bound) == (False, False)
    for bound in (False, True):
        monkeypatch.setattr(switches, "is_bound", lambda env=None, bound=bound: bound)
        assert switches.on(item) is False


def test_off_a_pass_makes_no_keeper_and_builds_a_boundary_per_read(corpus, monkeypatch, keepers):
    monkeypatch.delenv(SWITCH, raising=False)
    built, original = [], eb.EntityBoundary.__init__

    def counted(self, *args, **kwargs):
        built.append(kwargs.get("record", False))
        original(self, *args, **kwargs)
    monkeypatch.setattr(eb.EntityBoundary, "__init__", counted)
    result = run_pass(corpus)
    assert keepers == [] and rpb._CURRENT.get() is None
    assert built and not any(built)
    assert len(built) >= result.scanned + len(result.calls)   # at least one per prepare: none is reused


def test_each_pass_has_its_own_keeper_and_ends_it(corpus, switch_on, keepers):
    run_pass(corpus)
    run_pass(corpus)
    first, second = keepers
    assert first is not second and first.counts["built"] == 1 and second.counts["built"] == 1
    assert first._closed and second._closed and first._kept is None and second._kept is None
    assert rpb._CURRENT.get() is None


def test_a_failed_pass_ends_its_keeper(corpus, monkeypatch, switch_on, keepers):
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(arw, "is_current", broken)
    with owner(), pytest.raises(RuntimeError):
        asyncio.run(AutomaticReviewWorker(corpus.resolver, corpus.reviews)._process(corpus.request, refresh=False))
    assert keepers[0]._closed and keepers[0]._kept is None and keepers[0]._before == {}


def test_at_the_end_the_forms_are_emptied_and_marked_and_a_copied_context_gets_nothing(corpus, switch_on, keepers,
                                                                                      monkeypatch):
    captured = {}
    real = rpb.ReviewPassBoundary.boundary_for

    def boundary_for(self, resolver, conn):
        found = real(self, resolver, conn)
        if "context" not in captured:
            captured.update(context=contextvars.copy_context(), template=self._kept[0], handed=found)
        return found
    monkeypatch.setattr(rpb.ReviewPassBoundary, "boundary_for", boundary_for)
    run_pass(corpus)
    keeper = keepers[0]
    template, handed = captured["template"], captured["handed"]
    assert template._forms is handed._forms and list(template._forms) == [eb._ENDED]
    assert template.conn is None
    # A task started inside a row keeps the context: it finds the pass closed and is handed nothing.
    with owner():
        def later():
            assert rpb._CURRENT.get() is keeper
            with corpus.resolver._read() as (conn, _floor):
                return rpb.for_read(corpus.resolver, conn)
        assert captured["context"].run(later) is None
    assert keeper._kept is None and keeper._before == {} and keeper._verification._closed


def test_a_template_the_pass_stops_using_is_ended_at_once(corpus, switch_on, keepers, monkeypatch):
    templates, real = [], rpb.ReviewPassBoundary.boundary_for

    def boundary_for(self, resolver, conn):
        found = real(self, resolver, conn)
        if self._kept[0] not in templates:
            templates.append(self._kept[0])
        return found
    monkeypatch.setattr(rpb.ReviewPassBoundary, "boundary_for", boundary_for)
    ended = []
    real_end = rpb._end

    def end(template):
        real_end(template)
        ended.append((template, list(template._forms) == [eb._ENDED], keepers[0]._closed))
    monkeypatch.setattr(rpb, "_end", end)
    run_pass(corpus, change="offlimits_added")
    first, second = templates
    assert [(template, marked) for template, marked, _closed in ended] == [(first, True), (second, True)]
    assert [closed for *_rest, closed in ended] == [False, True]   # the first when replaced, the second at the end
    assert first.terms and first.terms < second.terms             # the closures themselves were never emptied


def held_by_modules(predicate) -> list:
    """Where a global of the pass's modules (and what it holds: containers, a function's attributes, defaults and
    closure, a class's attributes, an object's attributes, to a few levels) holds something `predicate` accepts."""
    modules = (rpb, eb, evidence, arw, amr, search_index, switches)
    found, seen = [], {}

    def walk(value, path, depth):
        if id(value) in seen or depth > 6:
            return
        seen[id(value)] = value
        if predicate(value):
            found.append(path)
            return
        if isinstance(value, dict):
            for key, item in list(value.items()):
                walk(key, f"{path}[key]", depth + 1)
                walk(item, f"{path}[]", depth + 1)
        elif isinstance(value, (set, frozenset, tuple, list)):
            for item in list(value):
                walk(item, f"{path}[]", depth + 1)
        elif isinstance(value, types.FunctionType):
            walk(value.__dict__, f"{path}.__dict__", depth + 1)
            walk(value.__defaults__ or (), f"{path}.__defaults__", depth + 1)
            walk(value.__kwdefaults__ or {}, f"{path}.__kwdefaults__", depth + 1)
            for index, cell in enumerate(value.__closure__ or ()):
                try:
                    walk(cell.cell_contents, f"{path}.<closure {index}>", depth + 1)
                except ValueError:
                    pass
        elif isinstance(value, (staticmethod, classmethod)):
            walk(value.__func__, f"{path}.__func__", depth + 1)
        elif isinstance(value, contextvars.ContextVar):
            walk(value.get(None), f"{path}.get()", depth + 1)
        elif hasattr(value, "__dict__") and not isinstance(value, (type, types.ModuleType)):
            walk(dict(vars(value)), f"{path}.__dict__", depth + 1)

    for module in modules:
        for name, value in list(vars(module).items()):
            if name.startswith("__"):
                continue
            if isinstance(value, type) and value.__module__ == module.__name__:
                walk(dict(vars(value)), f"{module.__name__}.{name}.__dict__", 0)
            elif not isinstance(value, types.ModuleType):
                walk(value, f"{module.__name__}.{name}", 0)
    return found


def _holds_a_boundary_or_a_name(value) -> bool:
    if isinstance(value, (eb.EntityBoundary, rpb.ReviewPassBoundary)):
        return True
    return isinstance(value, str) and any(word in value.lower() for word in PROTECTED_WORDS)


def test_after_a_pass_no_module_holds_a_boundary_or_a_protected_name(corpus, switch_on, keepers):
    assert held_by_modules(_holds_a_boundary_or_a_name) == []
    run_pass(corpus, change="offlimits_added")
    assert keepers and keepers[0].counts["built"] == 2
    assert held_by_modules(_holds_a_boundary_or_a_name) == []
