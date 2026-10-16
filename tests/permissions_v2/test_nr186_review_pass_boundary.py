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
import gc
import sqlite3
import threading
import types
from collections import Counter

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
#: Every invented name and address the corpus protects or can come to protect (`review_pass_corpus`).
INVENTED = ("Mara Example", "mara@example.org", "Zeb Quill", "Olwen Fairly", "Old Mara Quenby",
            "marabel.example@example.org")


class FakeClock:
    """Monotonic seconds that move only when a test moves them."""

    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def keepers(monkeypatch, clock):
    """Every keeper a pass makes, in order, each on `clock` (which stands still unless a test moves it)."""
    made = []
    original = rpb.ReviewPassBoundary

    class Seen(original):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("clock", clock)
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


def _shadow(monkeypatch, comparisons, *, reference_first=True):
    """Every `prepare` made inside a pass's row is compared with one made just before it (or just after it) with no
    pass current: the same moment, when nothing commits in between."""
    real = amr.prepare

    def fresh(resolver, reviews, identity):
        token = rpb._CURRENT.set(None)
        try:
            return _comparable(lambda: real(resolver, reviews, identity))[0]
        finally:
            rpb._CURRENT.reset(token)

    def prepare(resolver, reviews, identity):
        if rpb._CURRENT.get() is None:
            return real(resolver, reviews, identity)
        reference = fresh(resolver, reviews, identity) if reference_first else None
        got, prepared, error = _comparable(lambda: real(resolver, reviews, identity))
        if not reference_first:
            reference = fresh(resolver, reviews, identity)
        comparisons.append((identity.record_id, got, reference))
        if error is not None:
            raise error
        return prepared
    monkeypatch.setattr(amr, "prepare", prepare)      # publish's own prepare
    monkeypatch.setattr(arw, "prepare", prepare)      # the worker's


def run_pass(corpus, *, change=None, at=AT, on_change=None, refresh=False, hooks=None):
    """One worker pass over the corpus's window, with `change` written before scanned row `at` (and each of `hooks`,
    {row: callable}, called before its row). What it decided, and what was current between rows (the classifier's
    call and the owner pass's index refresh run outside any row)."""
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
            if hooks and scanned[0] in hooks:
                hooks[scanned[0]]()
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
              [("a\n('b',)",)],
              # WS0's review (W2): an empty text is not a NULL, nor a NULL's name; 0, 0.0 and -0.0 are three values.
              [("",)], [(0.0,)], [(-0.0,)], [(0,)], [("None",)], [(b"",)]]
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
    counts = keepers[0].counts
    assert (counts["built"], counts["token"], counts["reads"]) == (len(comparisons), 0, 0)


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
            assert keeper._kept.token is None and keeper.counts["built"] == 1
            with keeper.row():
                with corpus.resolver._read() as (conn, _floor):
                    corpus.resolver.entity_boundary(conn)
        # No commit since, but no token was kept: the reads prove it, and now a token is kept.
        assert (keeper.counts["built"], keeper.counts["token"], keeper.counts["reads"]) == (1, 0, 1)
        assert keeper._kept.token is not None
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
        assert (keeper.counts["built"], keeper.counts["token"], keeper.counts["reads"]) == (0, 0, 0)
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
            captured.update(context=contextvars.copy_context(), template=self._kept.template, handed=found)
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
        if self._kept.template not in templates:
            templates.append(self._kept.template)
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


def _modules():
    """Every loaded module of sharing and of the lifecycle (Off-limits) code, the pass's own included."""
    import sys
    return sorted({module for name, module in list(sys.modules.items())
                   if module is not None and name.startswith(("topos.permissions_v2", "topos.features.lifecycle"))}
                  | {rpb, eb, evidence, arw, amr, search_index, switches}, key=lambda module: module.__name__)


def held_by_modules(predicate) -> list:
    """(path, value) for every value `predicate` accepts that a module of `_modules` holds: its globals, the classes it
    defines (their dicts), and what they hold to a few levels (containers, an object's attributes and slots, a
    function's attributes, default arguments and closure, a context variable's value). Containers and objects are
    walked once; strings are leaves and are reported at every place they are held."""
    found, seen = [], {}

    def walk(value, path, depth):
        if depth > 6:
            return
        if predicate(value):
            found.append((path, value if isinstance(value, str) else type(value).__name__))
            return
        if isinstance(value, (str, bytes, int, float, type(None))) or id(value) in seen:
            return
        seen[id(value)] = value   # kept alive for the walk, so no id is handed to another object
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
        elif isinstance(value, property):
            walk((value.fget, value.fset, value.fdel), f"{path}.<accessors>", depth + 1)
        elif isinstance(value, contextvars.ContextVar):
            walk(value.get(None), f"{path}.get()", depth + 1)
        elif not isinstance(value, (type, types.ModuleType)):
            if hasattr(value, "__dict__"):
                walk(dict(vars(value)), f"{path}.__dict__", depth + 1)
            for klass in type(value).__mro__:
                for slot in getattr(klass, "__slots__", ()):
                    if isinstance(slot, str) and hasattr(value, slot):
                        walk(getattr(value, slot), f"{path}.{slot}", depth + 1)

    for module in _modules():
        for name, value in list(vars(module).items()):
            if name.startswith("__"):
                continue
            if isinstance(value, type) and value.__module__ == module.__name__:
                walk(dict(vars(value)), f"{module.__name__}.{name}.__dict__", 0)
            elif not isinstance(value, types.ModuleType):
                walk(value, f"{module.__name__}.{name}", 0)
    return found


def _runs(names, length=4):
    """Every run of `length` or more characters of each name, squashed (the boundary's skeleton: no spaces or
    punctuation) and split into its words, case folded."""
    import re
    runs = set()
    for name in names:
        for form in (eb.skeleton(name), *re.findall(r"[^\W_]+", name.casefold())):
            runs.update(form[start:end] for start in range(len(form)) for end in range(start + length, len(form) + 1))
    return frozenset(runs)


def _is_a_boundary(value) -> bool:
    return isinstance(value, (eb.EntityBoundary, rpb.ReviewPassBoundary, rpb._Kept))


def _holding(runs):
    import re
    pattern = re.compile("|".join(map(re.escape, sorted(runs, key=len, reverse=True))))

    def holds(value):
        return _is_a_boundary(value) or (isinstance(value, str) and pattern.search(value.casefold()) is not None)
    return holds


def _closure_vocabulary(canonical) -> frozenset:
    """Every string the boundary on this database holds (terms, name parts, short words, handles, the protected
    mentions' texts) of three characters or more."""
    db = sqlite3.connect(canonical)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    try:
        built = eb.EntityBoundary(db)
    finally:
        db.rollback()
        db.close()
    vocabulary = set(built.terms) | set(built.name_parts) | set(built.name_short_words) | set(built.handles)
    vocabulary |= {mention["surface_text"] for mention in built.mentions if mention["surface_text"]}
    return frozenset(value for value in vocabulary if len(value) >= 3)


def test_the_runs_cover_the_squashed_and_split_forms():
    runs = _runs(["Mara Example"])
    assert {"mara", "maraexample", "example", "raex", "exam"} <= runs and not any(len(run) < 4 for run in runs)
    assert _holding(runs)("marAEXample") and not _holding(runs)("compiler")


def _fresh_name() -> str:
    """An invented person's name no other test in this process has used (random syllables): a store an earlier pass
    left behind in the same process cannot already hold its runs, so "new after the pass" is never masked by "there
    before it"."""
    import secrets
    syllables = ("vex", "qor", "thad", "mir", "uln", "zaph", "krel", "osk", "bry", "wint", "dolv", "plim")

    def word():
        return "".join(secrets.choice(syllables) for _ in range(3)).capitalize()
    return f"{word()} {word()}"


def _protect(name):
    """A change: an Off-limits entry for `name`, a person of their own with their first name as an alias."""
    import json

    def change(path, natives):
        rc._write(path, [
            ("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,aliases_json) VALUES(?,?,?,?,?)",
             ("fresh-entity", "person", name, name.lower(), json.dumps([name.split()[0]]))),
            ("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
             "VALUES('bh-fresh','fresh-entity',?,?,'complete')", (name, name.lower()))])
    return change


def _no_new_holdings(corpus, monkeypatch, tmp_path, holds_for):
    """Run a pass whose change protects a fresh name; what the modules hold (`holds_for(name, vocabulary)`) before the
    pass and after it. The fresh name is in the boundary the pass builds after its change (non-vacuity)."""
    name = _fresh_name()
    monkeypatch.setitem(rc.CHANGES, "nr186_fresh", _protect(name))
    copy = rc.keep(corpus, tmp_path / "changed")
    rc.CHANGES["nr186_fresh"](copy / "canonical.db", corpus.natives)
    vocabulary = _closure_vocabulary(corpus.canonical) | _closure_vocabulary(copy / "canonical.db")
    assert {eb.skeleton(name), *(eb.skeleton(word) for word in name.split())} <= vocabulary
    holds = holds_for(name, vocabulary)
    before = Counter(held_by_modules(holds))
    assert not [entry for entry in before if eb.skeleton(name) in str(entry[1]) or entry[1] in (
        "EntityBoundary", "ReviewPassBoundary", "_Kept")]
    result = run_pass(corpus, change="nr186_fresh")
    after = Counter(held_by_modules(holds))
    return name, before, after, result


def test_after_a_pass_no_module_gains_any_run_of_a_protected_name_or_any_boundary(corpus, monkeypatch, tmp_path,
                                                                                  switch_on, keepers):
    """WS0's review of NR186: the boundary keeps squashed forms ("maraexample") and single words ("mara"), so the
    walk looks for every run of four or more characters of each name, squashed and split, and compares what the
    modules hold before the pass with what they hold after it: a constant that happens to hold a run (a list of given
    names) is there before as after; anything the pass left behind is new. The pass protects a fresh name, so nothing
    an earlier pass left can hide what this one leaves. No boundary is held anywhere."""
    name, before, after, _result = _no_new_holdings(
        corpus, monkeypatch, tmp_path, lambda name, vocabulary: _holding(_runs([name, *INVENTED])))
    assert keepers and keepers[0].counts["built"] == 2
    assert after - before == Counter()


def test_after_a_pass_no_module_gains_any_of_the_closures_vocabulary(corpus, monkeypatch, tmp_path, switch_on,
                                                                     keepers):
    """The same differential over the exact strings the pass's two boundaries hold (before and after its change; the
    second read on a copy with the change made), short name parts of three letters included, which a run of four does
    not cover."""
    def holds_for(name, vocabulary):
        def holds(value):
            return _is_a_boundary(value) or (isinstance(value, str) and value in vocabulary)
        return holds
    _name, before, after, _result = _no_new_holdings(corpus, monkeypatch, tmp_path, holds_for)
    assert keepers[0].counts["built"] == 2
    assert after - before == Counter()


@pytest.mark.parametrize("on", [False, True])
def test_after_a_pass_no_boundary_is_alive_anywhere(corpus, monkeypatch, on):
    """Not only the modules: nothing in the process holds a boundary or a keeper's template once the pass is over."""
    if on:
        monkeypatch.setenv(SWITCH, "true")
    else:
        monkeypatch.delenv(SWITCH, raising=False)
    made = []
    original = rpb.ReviewPassBoundary

    class Seen(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)
    monkeypatch.setattr(rpb, "ReviewPassBoundary", Seen)
    run_pass(corpus, change="offlimits_added")
    gc.collect()
    alive = [item for item in gc.get_objects() if isinstance(item, (eb.EntityBoundary, rpb._Kept))]
    assert alive == []
    assert len(made) == int(on) and all(keeper._closed and keeper._kept is None for keeper in made)


# --- WS0's review (16 Oct): the token's two parts, more interleavings, the replay premise --------------------------

def test_the_canonical_token_moves_on_data_version_alone(corpus, monkeypatch):
    """With the file stats frozen, a commit by another connection still moves the token: the probe is one connection
    kept for the verification, so its data_version moves (a probe opened per read would repeat its first value)."""
    monkeypatch.setattr(search_index.SearchVerification, "_files", staticmethod(lambda path: ("frozen",)))
    verification = search_index.SearchVerification(corpus.resolver, corpus.reviews)
    try:
        first = verification.canonical_token()
        rc.CHANGES["unrelated_commit"](corpus.canonical, corpus.natives)
        second = verification.canonical_token()
    finally:
        verification.close()
    assert first is not None and second is not None and first != second


def test_the_pass_keeps_one_probe_for_its_whole_length(corpus, monkeypatch, switch_on, keepers):
    seen = []
    real = rpb.ReviewPassBoundary._token

    def token(self):
        found = real(self)
        seen.append(tuple(map(id, self._verification._probes.values())))
        return found
    monkeypatch.setattr(rpb.ReviewPassBoundary, "_token", token)
    run_pass(corpus, change="unrelated_commit")
    assert len(seen) > 20 and len(set(seen)) == 1 and len(seen[0]) == 1
    assert keepers[0]._verification._closed and keepers[0]._verification._probes == {}


def test_an_in_place_rewrite_with_its_mtime_put_back_is_seen_by_its_ctime(corpus, monkeypatch, tmp_path, switch_on,
                                                                          keepers):
    """The database's bytes are replaced in place (same inode and size, its WAL empty since the row before), with an
    Off-limits alias, and its mtime put back: of the whole token only the main file's ctime moves (the probe's
    data_version does not, nor the WAL's or the journal's state), so the token moves and the reads find the alias.
    Both parts of the token matter: without the ctime this pass is served stale."""
    import os
    seen = {}

    def checkpoint():
        db = sqlite3.connect(corpus.canonical)
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        finally:
            db.close()

    def rewrite():
        path = corpus.canonical
        side = tmp_path / "side.db"
        side.write_bytes(path.read_bytes())
        other = sqlite3.connect(side)
        other.execute("UPDATE entity_blackholes SET aliases_json=? WHERE blackhole_id='bh'", ('["Olwen Fairly"]',))
        other.commit()
        other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        other.close()
        keeper = keepers[0]
        token, before = keeper._token(), os.stat(path)
        with open(path, "r+b") as handle:
            handle.write(side.read_bytes())
            handle.truncate()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = keeper._token()
        main_before, main_after = token[2][0], after[2][0]
        seen.update(kept=keeper._kept.token == token, version=token[1] == after[1],
                    sidecars=token[2][1:] == after[2][1:],
                    main_but_ctime=[a for a, b in zip(main_before, main_after) if a != b] == [main_before[4]])
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus, hooks={AT - 1: checkpoint, AT: rewrite})
    assert seen == {"kept": True, "version": True, "sidecars": True, "main_but_ctime": True}
    assert [c for c in comparisons if c[1] != c[2]] == []
    assert keepers[0].counts["built"] == 2


def test_a_commit_between_opened_and_the_first_statement_is_proven_at_the_next_row(corpus, monkeypatch, switch_on,
                                                                                   keepers):
    """An Off-limits alias lands after `opened` read the token and before the read's first statement: the template is
    built on the newer snapshot and keyed by the older token, so the next row proves it by its reads, never by the
    token."""
    fired = []
    real = rpb.ReviewPassBoundary.opened

    def opened(self, resolver, conn):
        real(self, resolver, conn)
        if not fired and self._usable(resolver):
            fired.append(1)
            rc.CHANGES["offlimits_alias"](corpus.canonical, corpus.natives)
    monkeypatch.setattr(rpb.ReviewPassBoundary, "opened", opened)
    comparisons = []
    _shadow(monkeypatch, comparisons, reference_first=False)   # the change lands inside the pass's own first read
    run_pass(corpus)
    assert fired and [c for c in comparisons if c[1] != c[2]] == []
    counts = keepers[0].counts
    assert counts["built"] == 1 and counts["reads"] == 1


def test_a_commit_from_another_process_is_seen(corpus, monkeypatch, switch_on, keepers):
    import subprocess
    import sys
    code = ("import sqlite3,sys,json; db=sqlite3.connect(sys.argv[1]); "
            "db.execute(\"UPDATE entity_blackholes SET aliases_json=? WHERE blackhole_id='bh'\", "
            "(json.dumps(['Olwen Fairly']),)); db.commit(); db.close()")
    monkeypatch.setitem(rc.CHANGES, "nr186_process", lambda path, natives: subprocess.run(
        [sys.executable, "-I", "-c", code, str(path)], check=True, timeout=60))
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus, change="nr186_process")
    assert [c for c in comparisons if c[1] != c[2]] == []
    assert keepers[0].counts["built"] == 2


@pytest.mark.parametrize("sql", ["ALTER TABLE entity_blackholes ADD COLUMN nr186_extra TEXT",
                                 "CREATE INDEX nr186_index ON entities(canonical_name)",
                                 "ALTER TABLE contacts RENAME COLUMN display_name TO nr186_name",
                                 "PRAGMA wal_checkpoint(TRUNCATE)"])
def test_a_schema_change_or_a_checkpoint_between_rows_decides_as_a_fresh_boundary(corpus, monkeypatch, switch_on,
                                                                                  keepers, sql):
    def change(path, natives):
        db = sqlite3.connect(path)
        try:
            db.execute(sql).fetchall()
            db.commit()
        finally:
            db.close()
    monkeypatch.setitem(rc.CHANGES, "nr186_schema", change)
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus, change="nr186_schema")
    assert [c for c in comparisons if c[1] != c[2]] == []


def test_construction_reads_no_environment_clock_or_file(corpus):
    """The replay's premise beyond the connection: construction reads no environment variable, no clock and opens no
    file, so the same reads build the same boundary."""
    import os
    import sys
    import time
    db = sqlite3.connect(corpus.canonical)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    environment, clocks, events, armed = [], [], [], [False]

    class Recording(dict):
        def __getitem__(self, key):
            environment.append(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            environment.append(key)
            return super().get(key, default)

        def __contains__(self, key):
            environment.append(key)
            return super().__contains__(key)

    def hook(event, args):
        if armed[0] and event in ("open", "os.listdir", "os.scandir", "subprocess.Popen", "sqlite3.connect",
                                  "socket.connect", "os.stat"):
            events.append(event)
    sys.addaudithook(hook)
    with pytest.MonkeyPatch.context() as patch:
        for name in ("time", "monotonic", "perf_counter", "process_time", "time_ns"):
            real = getattr(time, name)
            patch.setattr(time, name, lambda *a, _real=real, _name=name: (clocks.append(_name), _real(*a))[1])
        patch.setattr(os, "environ", Recording(os.environ))
        armed[0] = True
        try:
            boundary = eb.EntityBoundary(db, record=True)
        finally:
            armed[0] = False
    db.rollback()
    db.close()
    assert boundary.active and (environment, clocks, events) == ([], [], [])


def test_every_switch_flipped_builds_the_same_closure(corpus, monkeypatch):
    def closure():
        db = sqlite3.connect(corpus.canonical)
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        try:
            built = eb.EntityBoundary(db)
            return built.revision, frozenset(built.terms), frozenset(built.ids), frozenset(built.name_parts), built._groups
        finally:
            db.rollback()
            db.close()
    baseline, flipped = closure(), 0
    for item in switches.SWITCHES:
        if item.kind == "bool":
            monkeypatch.setenv(item.name, "false" if item.unbound else "true")
            flipped += 1
    assert flipped > 20 and closure() == baseline


def test_a_cancelled_pass_ends_its_keeper(corpus, monkeypatch, switch_on, keepers):
    captured = {}
    real = rpb.ReviewPassBoundary.boundary_for

    def boundary_for(self, resolver, conn):
        found = real(self, resolver, conn)
        captured.setdefault("template", self._kept.template)
        return found
    monkeypatch.setattr(rpb.ReviewPassBoundary, "boundary_for", boundary_for)

    async def main():
        reached = asyncio.Event()

        async def classify(prepared):
            reached.set()
            await asyncio.sleep(3600)
        worker = AutomaticReviewWorker(corpus.resolver, corpus.reviews, classifier=classify)
        task = asyncio.ensure_future(worker._process(corpus.request, refresh=False))
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with owner():
        asyncio.run(main())
    keeper = keepers[0]
    assert keeper._closed and keeper._kept is None and keeper._before == {} and keeper._verification._closed
    assert keeper._idle is None and list(captured["template"]._forms) == [eb._ENDED]


# --- the switch at every row (WS0's review, item 4) ----------------------------------------------------------------

def _turn(monkeypatch, value):
    return lambda: monkeypatch.setenv(SWITCH, value)


def test_the_switch_turned_off_mid_pass_drops_the_template_at_the_next_row(corpus, monkeypatch, switch_on, keepers):
    built = []
    real = evidence.EvidenceResolver._new_entity_boundary

    def factory(conn, *, record=False):
        built.append(record)
        return real(conn, record=record)
    monkeypatch.setattr(evidence.EvidenceResolver, "_new_entity_boundary", staticmethod(factory))
    comparisons = []
    _shadow(monkeypatch, comparisons)
    seen = {}
    result = run_pass(corpus, hooks={AT: _turn(monkeypatch, "false"),
                                     AT + 1: lambda: seen.update(kept=keepers[0]._kept, counts=dict(keepers[0].counts))})
    keeper = keepers[0]
    assert [c for c in comparisons if c[1] != c[2]] == []
    assert seen["kept"] is None and keeper.counts == seen["counts"]       # nothing kept or proven after the row
    assert keeper.counts["switched_off"] == 1
    # From row AT on, each of the pass's own reads built a plain boundary (the references build theirs too).
    assert built.count(True) == 1 and built.count(False) >= 2 * (result.scanned - AT + 1)


def test_the_switch_turned_on_again_builds_afresh(corpus, monkeypatch, switch_on, keepers):
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus, hooks={AT: _turn(monkeypatch, "false"), AT + 2: _turn(monkeypatch, "true")})
    assert [c for c in comparisons if c[1] != c[2]] == []
    assert keepers[0].counts["built"] == 2 and keepers[0].counts["switched_off"] == 1


# --- the age cap and a stalled pass (WS0's review, item 5) ---------------------------------------------------------

@pytest.mark.parametrize("raw, seconds", [(None, 600), ("5", 60), ("90", 90), ("3600", 3600), ("99999", 3600),
                                          ("ten", 600), ("", 600)])
def test_the_age_cap_is_read_as_60_to_3600_seconds(monkeypatch, raw, seconds):
    name = switches.REVIEW_PASS_BOUNDARY_MAX_AGE.name
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)
    assert rpb.max_age() == seconds


@pytest.mark.parametrize("seconds, rebuilt", [(599, False), (601, True)])
def test_a_template_older_than_the_cap_is_built_again(corpus, monkeypatch, switch_on, keepers, clock, seconds,
                                                      rebuilt):
    """The fake clock moves `seconds` between two halves of one row's prepare: the template, built at that row, is
    older than the cap (600 s) at the next read only when more than 600 s passed."""
    real, moved = rpb.ReviewPassBoundary.boundary_for, []

    def boundary_for(self, resolver, conn):
        found = real(self, resolver, conn)
        if not moved and self._kept is not None:
            moved.append(1)
            clock.advance(seconds)   # inside a row, so no stall: only the template's age moves
        return found
    monkeypatch.setattr(rpb.ReviewPassBoundary, "boundary_for", boundary_for)
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus)
    counts = keepers[0].counts
    assert [c for c in comparisons if c[1] != c[2]] == []
    assert (counts["aged"], counts["built"], counts["stalled"]) == ((1, 2, 0) if rebuilt else (0, 1, 0))


def test_the_age_cap_is_read_at_every_row(corpus, monkeypatch, switch_on, keepers, clock):
    """The cap shortened to 60 s at row AT applies from that row: a template built at row AT is 61 s old at the next
    read and is built again (at the pass's opening cap of 600 s it would have been kept)."""
    import os
    name = switches.REVIEW_PASS_BOUNDARY_MAX_AGE.name
    monkeypatch.delenv(name, raising=False)
    real, moved = rpb.ReviewPassBoundary.boundary_for, []

    def boundary_for(self, resolver, conn):
        found = real(self, resolver, conn)
        if os.environ.get(name) == "60" and not moved:
            moved.append(1)
            clock.advance(61)
        return found
    monkeypatch.setattr(rpb.ReviewPassBoundary, "boundary_for", boundary_for)
    run_pass(corpus, hooks={AT: lambda: monkeypatch.setenv(name, "60")})
    counts = keepers[0].counts
    assert moved and keepers[0]._max_age == 60 and counts["aged"] == 1 and counts["built"] == 2


@pytest.mark.parametrize("seconds, dropped", [(599, False), (601, True)])
def test_a_pass_without_a_row_for_longer_than_the_cap_drops_its_template(corpus, monkeypatch, switch_on, keepers,
                                                                         clock, seconds, dropped):
    seen = {}
    comparisons = []
    _shadow(monkeypatch, comparisons)
    run_pass(corpus, hooks={AT: lambda: clock.advance(seconds),
                            AT + 1: lambda: seen.update(counts=dict(keepers[0].counts))})
    counts = keepers[0].counts
    assert [c for c in comparisons if c[1] != c[2]] == []
    assert (counts["stalled"], counts["built"]) == ((1, 2) if dropped else (0, 1))


class _Loop:
    """A loop standing in for the pass's: it keeps what `call_later` was asked, and runs it when told."""

    def __init__(self):
        self.calls = []

    def call_later(self, delay, callback):
        handle = types.SimpleNamespace(delay=delay, callback=callback, cancelled=False)
        handle.cancel = lambda: setattr(handle, "cancelled", True)
        self.calls.append(handle)
        return handle


def test_the_stall_callback_drops_the_template_when_no_row_came_within_the_cap(corpus, switch_on, clock):
    loop = _Loop()
    keeper = rpb.ReviewPassBoundary(corpus.resolver, corpus.reviews, clock=clock, loop=loop)
    try:
        with owner():
            with keeper.row(), corpus.resolver._read() as (conn, _floor):
                corpus.resolver.entity_boundary(conn)
        template = keeper._kept.template
        armed, = loop.calls
        assert armed.delay == 600 and not armed.cancelled
        clock.advance(300)
        armed.callback()                       # early: nothing is dropped, and it is armed again for what is left
        assert keeper._kept is not None and loop.calls[-1].delay == 300 and keeper._idle is loop.calls[-1]
        again = loop.calls[-1]
        with keeper.row():                     # a row cancels the pending callback and arms a new one at its end
            pass
        assert again.cancelled and len(loop.calls) == 3 and loop.calls[-1].delay == 600
        clock.advance(601)
        loop.calls[-1].callback()
        assert keeper._kept is None and keeper.counts["stalled"] == 1 and list(template._forms) == [eb._ENDED]
    finally:
        keeper.close()
    assert keeper._idle is None


def test_a_pass_on_its_loop_arms_the_stall_callback_between_rows(corpus, switch_on, keepers):
    import asyncio as aio
    armed = []

    async def classify(prepared):
        keeper = keepers[0]
        loop = aio.get_running_loop()
        armed.append(keeper._loop is loop and keeper._idle is not None
                     and abs(keeper._idle.when() - (loop.time() + keeper._max_age)) < 5)
        return rc.answer(prepared)
    worker = AutomaticReviewWorker(corpus.resolver, corpus.reviews, classifier=classify)
    with owner():
        aio.run(worker._process(corpus.request, refresh=False))
    assert armed and all(armed)
    assert keepers[0]._idle is None


# --- one construction (WS0's review, item 6) -----------------------------------------------------------------------

@pytest.mark.parametrize("on", [False, True])
def test_the_pass_and_a_plain_read_build_through_one_factory(corpus, monkeypatch, keepers, on):
    if on:
        monkeypatch.setenv(SWITCH, "true")
    else:
        monkeypatch.delenv(SWITCH, raising=False)
    made, real_init = [], eb.EntityBoundary.__init__
    calls, real_factory = [], evidence.EvidenceResolver._new_entity_boundary

    def init(self, conn, *args, **kwargs):
        made.append((args, kwargs))
        real_init(self, conn, *args, **kwargs)

    def factory(conn, *args, **kwargs):
        calls.append((args, kwargs))
        return real_factory(conn, *args, **kwargs)
    monkeypatch.setattr(eb.EntityBoundary, "__init__", init)
    monkeypatch.setattr(evidence.EvidenceResolver, "_new_entity_boundary", staticmethod(factory))
    result = run_pass(corpus)
    # Every boundary the pass made came out of the factory, the plain reads' and the pass's template alike, and
    # the factory passes nothing to the constructor but `record`: one call site cannot build differently.
    assert len(made) == len(calls) and all(args == () for args, _kwargs in made + calls)
    assert [kwargs for _args, kwargs in made] == [{"record": kwargs.get("record", False)} for _a, kwargs in calls]
    assert [kwargs.get("record", False) for _args, kwargs in calls].count(True) == int(on)
    assert len(calls) == (1 if on else result.scanned + len(result.calls))
