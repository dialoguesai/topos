"""The enrolled review stores past the 1 MiB digest cap: capacity, detection, crash and compatibility.

The authority digest is defined exactly as before -- `digest` of every review row
ordered by `review_id`, retired rows included -- but it is streamed, so a store is
no longer stopped at roughly 309 owner reviews of M1's shape. These tests pin both
halves of that claim: stores now grow, and nothing the external floor detected
before has become invisible, on a small store and on one past the old cap alike.

Every detection case is asserted three ways, because the floor's comparand is the
marker the runtime cached: through the already-built service, through a fresh
`get()`, and after a restart.
"""
from contextlib import contextmanager
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import time

import pytest

from topos.permissions_v2 import evidence as evidence_module
from topos.permissions_v2.canonical import MAX_BYTES, PolicyError, Rows, canonical_bytes, digest, digest_stream
from topos.permissions_v2.evidence import (EvidenceIdentity, EvidenceRevision, EvidenceReviewStore,
    EvidenceSnapshot, OwnerEvidenceReview, ReviewedClassification)
from topos.permissions_v2.evidence_review_runtime import ReviewEnrollmentRuntime
from topos.permissions_v2.evidence_reviews import EvidenceLookup, RecordEvidenceReview, RevokeEvidenceReview
from topos.permissions_v2.projection_reviews import (ProjectionReviewStore, RecordProjectionReview,
    RevokeProjectionReview)
from topos.permissions_v2.runtime import load_runtime

from tests.permissions_v2.test_evidence import corpus, owner  # noqa: F401
from tests.permissions_v2.test_evidence_reviews import paired_runtime  # noqa: F401
from tests.permissions_v2.test_projection_reviews import projection_runtime  # noqa: F401


SELECT = "SELECT review_id,fact_id,review_json,active FROM fact_reviews ORDER BY review_id"
# 400 M1-shaped rows encode to about 1.2 MB, past the cap that stopped the old digest.
# Detection and crash cases run at both sizes.
OVER_CAP = 400
SIZES = ["small", "over_cap"]
EMPTY_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
KNOWN_ANSWER = "6945d2fe8fe261273b30165890fbb72569abbf768c2403ed8720a1cbe767caed"


# --- M1-shaped rows --------------------------------------------------------------------------------


def m1_review(binding, index, *, track="e"):
    """One locator evidence review of M1's shape: one artifact, one message leaf, two classifications.

    Identifier lengths follow the campaign: a uuid fact id, an `imessage:<rowid>` leaf, an
    `m1ds-<24 hex>` dataset id and 64-hex revisions. The encoded row size is asserted below,
    so a model change that moves it out of the measured band is visible here rather than in
    a capacity claim that quietly stops being about M1's shape.
    """
    seed_hex = hashlib.sha256(f"{track}-{index}".encode()).hexdigest()
    fact_id = f"{seed_hex[:8]}-{seed_hex[8:12]}-4{seed_hex[13:16]}-a{seed_hex[17:20]}-{seed_hex[20:32]}"
    artifact = EvidenceRevision(identity=EvidenceIdentity(binding=binding, table="signal_objects",
        record_id=fact_id, source_id=None, dataset_kind="node_resource", dataset_id=None), revision=seed_hex)
    leaf = EvidenceRevision(identity=EvidenceIdentity(binding=binding, table="conversation_messages",
        record_id=f"imessage:{100001 + index}", source_id="imessage", dataset_kind="row_dataset",
        dataset_id="m1ds-" + seed_hex[:24]), revision=hashlib.sha256(seed_hex.encode()).hexdigest())
    snapshot = EvidenceSnapshot(binding=binding, canonical_file_revision=seed_hex, fact_id=fact_id,
        candidate_revision=seed_hex, lineage_revision=seed_hex, protection_revision=seed_hex,
        artifacts=[artifact], leaves=[leaf])
    classifications = [ReviewedClassification(evidence=item, domains=["hobbies"], sensitivity="personal",
        subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
        independent_copies="none_known") for item in (artifact, leaf)]
    review = OwnerEvidenceReview(version="topos-owner-evidence-review/v1",
        review_id=f"m1r-{track}-{seed_hex[:20]}", owner_id=binding.owner_id, reviewed_at=1789600000 + index,
        snapshot=snapshot, classifications=classifications)
    return (review.review_id, fact_id, canonical_bytes(review.model_dump()).decode("ascii"), 1)


def m1_rows(binding, count, *, track="e", start=0):
    return [m1_review(binding, index, track=track) for index in range(start, start + count)]


def grow(store, rows, *, batch=60):
    """Write reviews through the enrolled store's own transaction, `batch` per transaction.

    This is exactly `record_review`'s write -- retire the fact's current row, insert the new
    one -- so every transaction verifies the entry digest and publishes the marker.
    """
    for index in range(0, len(rows), batch):
        with store._db() as db:
            for row in rows[index:index + batch]:
                db.execute("UPDATE fact_reviews SET active=0 WHERE fact_id=? AND active=1", (row[1],))
                db.execute("INSERT INTO fact_reviews VALUES(?,?,?,?)", row)


# --- fixtures and small helpers ----------------------------------------------------------------------


def code(call):
    try:
        call()
        return "ok"
    except PolicyError as exc:
        return exc.code


def flat_digest(rows):
    """The uncapped whole-history formula the lab's activate-pending recovery computes."""
    return hashlib.sha256(json.dumps(rows, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode("ascii")).hexdigest()


def legacy_digest(db):
    """`_authority_digest` exactly as it stood before the change: one canonical value, capped."""
    rows = db.execute("SELECT review_id,fact_id,review_json,active FROM fact_reviews ORDER BY review_id").fetchall()
    return digest([list(row) for row in rows])


@contextmanager
def capped_digest():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(EvidenceReviewStore, "_authority_digest", staticmethod(legacy_digest))
        yield patch


def tamper(store, sql, args=()):
    with sqlite3.connect(store) as conn:
        conn.execute(sql, args)


class Enrolled:
    def __init__(self, runtime, config, path, service, corpus):
        self.runtime, self.config, self.path, self.service, self.corpus = runtime, config, path, service, corpus
        self.current = None
        self.store = Path(config["evidence_review_store_path"])
        self.marker = self.store.with_name(self.store.name + ".enrollment.json")

    @property
    def reviews(self):
        return self.service.reviews

    @property
    def lookup(self):
        return EvidenceLookup(fact_id=self.corpus[2])

    def rows(self):
        with sqlite3.connect(self.store) as conn:
            return [list(row) for row in conn.execute(SELECT)]

    def revision(self):
        return json.loads(self.marker.read_text())["revision"]

    def restart(self):
        self.runtime.close()
        return load_runtime(self.path, active_database=self.corpus[0].path)

    def _three_ways(self, answer):
        """The three ways an enrolled store is reached: cached service, fresh get(), after restart."""
        with owner():
            cached = answer(lambda: self.service.read(self.lookup))
            fresh = answer(lambda: self.runtime.evidence_reviews(require_existing=True).read(self.lookup))
        reopened = None
        try:
            reopened = self.restart()
            with owner():
                restarted = answer(lambda: reopened.evidence_reviews(require_existing=True).read(self.lookup))
        except PolicyError as exc:  # load_runtime itself can refuse the private paths
            restarted = exc.code
        finally:
            if reopened is not None:
                reopened.close()
        return cached, fresh, restarted

    def read_codes(self):
        return self._three_ways(code)

    def read_revisions(self):
        """The same three ways, reporting which review each one actually served.

        A refusal code is not the whole story when the question is whether a store serves a
        review it must not: `read_codes` answers `ok` either way. This reports the served
        `current_review_revision` -- `None` when nothing is current -- or the refusal code.
        """
        def served(call):
            try:
                return call().current_review_revision
            except PolicyError as exc:
                return exc.code

        return self._three_ways(served)


def request_for(corpus, review_id, *, expected=None):
    with owner():
        snapshot = corpus[0].inspect_for_review(corpus[2])
    classifications = [ReviewedClassification(evidence=item, domains=["reading"], sensitivity="personal",
        subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
        independent_copies="none_known") for item in snapshot.artifacts + snapshot.leaves]
    return RecordEvidenceReview(review_id=review_id, expected_snapshot=snapshot,
        expected_current_review_revision=expected, classifications=classifications)


@pytest.fixture
def enrolled(corpus, paired_runtime):
    runtime, config, path = paired_runtime
    with owner():
        service = runtime.evidence_reviews(require_existing=False)
    return Enrolled(runtime, config, path, service, corpus)


def seed(enrolled, *, filler=0):
    """One current review on the corpus fact, one retired review, and `filler` retired M1 rows."""
    with owner():
        first = enrolled.service.record(request_for(enrolled.corpus, "seed-review-1"), now=1200)
        enrolled.service.revoke(RevokeEvidenceReview(fact_id=enrolled.corpus[2], review_id="seed-review-1",
            expected_review_revision=first.review_revision))
        current = enrolled.service.record(request_for(enrolled.corpus, "seed-review-2"), now=1201)
    enrolled.current = current
    if filler:
        grow(enrolled.reviews, m1_rows(enrolled.corpus[0].binding, filler))
    return current


def next_request(enrolled, review_id):
    """A replacement for the seeded current review, which is what a second record has to name."""
    return request_for(enrolled.corpus, review_id, expected=enrolled.current.review_revision)


def sized(enrolled, size):
    return seed(enrolled, filler=OVER_CAP if size == "over_cap" else 0)


# --- capacity ----------------------------------------------------------------------------------------


def test_the_m1_row_shape_stays_in_the_measured_band(corpus):
    sizes = sorted(len(canonical_bytes([list(row)])) for row in m1_rows(corpus[0].binding, 40))
    assert 2500 <= sizes[0] and sizes[-1] <= 3600
    assert MAX_BYTES // sizes[len(sizes) // 2] < 400, "a row must still be a real fraction of the old cap"


def test_one_evidence_store_holds_far_more_reviews_than_the_old_cap(enrolled):
    """2,000 M1-shaped locator reviews in one store, with the owner path still working on top."""
    current = seed(enrolled, filler=0)
    binding = enrolled.corpus[0].binding
    grow(enrolled.reviews, m1_rows(binding, 1800))
    # The tail is one review per transaction, so each of these 200 writes pays its own entry
    # digest, exit digest and two marker publications on a store already far past the old cap.
    for row in m1_rows(binding, 200, start=1800):
        grow(enrolled.reviews, [row], batch=1)
    rows = enrolled.rows()
    assert len(rows) == 2002  # 2,000 M1 rows plus the seed's two
    assert len(json.dumps(rows, separators=(",", ":"))) > 5 * MAX_BYTES
    with pytest.raises(PolicyError, match="json_size"):
        digest(rows)  # the old whole-value digest cannot even be computed at this size

    marker = json.loads(enrolled.marker.read_text())
    assert marker["state"] == "active" and marker["version"] == "topos-owner-evidence-enrollment/v2"
    assert marker["authority_digest"] == digest_stream(Rows(rows)) == flat_digest(rows)

    with owner():
        assert enrolled.service.read(enrolled.lookup).current_review_revision == current.review_revision
        assert enrolled.corpus[0].qualify(enrolled.corpus[2], reviews=enrolled.reviews).verdict == "qualified"
        replacement = enrolled.service.record(request_for(enrolled.corpus, "after-the-cap",
            expected=current.review_revision), now=1300)
        assert replacement.state.qualification.verdict == "qualified"
        revoked = enrolled.service.revoke(RevokeEvidenceReview(fact_id=enrolled.corpus[2],
            review_id="after-the-cap", expected_review_revision=replacement.review_revision))
        assert revoked.action == "revoked"
    assert enrolled.read_codes() == ("ok", "ok", "ok")


def test_the_projection_store_also_grows_past_its_own_cap(corpus, projection_runtime):
    """The output store shares the class, so it inherits the streamed digest and the same floor.

    Filler rows carry evidence-review bodies of the same size band: the floor digests the
    stored bytes, and a retired output row is never parsed by any reader.
    """
    runtime, config, path = projection_runtime
    with owner():
        evidence_service = runtime.evidence_reviews(require_existing=False)
        evidence_service.record(request_for(corpus, "evidence-for-output"), now=1200)
        service = runtime.projection_reviews(require_existing=False)
        preview = service.preview(EvidenceLookup(fact_id=corpus[2]), now=1200)
        recorded = service.record(RecordProjectionReview(review_id="output-review-1",
            expected_candidate=preview.candidate, expected_candidate_hash=preview.candidate_hash,
            expected_current_review_revision=None, classification={"domains": ["reading"],
                "sensitivity": "personal", "subject": "self", "assertion": "explicit_atomic_preference"}),
            now=1200)
    grow(service.outputs, m1_rows(corpus[0].binding, 500, track="p"))
    store = Path(config["projection_review_store_path"])
    with sqlite3.connect(store) as conn:
        rows = [list(row) for row in conn.execute(SELECT)]
    assert len(rows) == 501
    with pytest.raises(PolicyError, match="json_size"):
        digest(rows)
    marker = json.loads(store.with_name(store.name + ".enrollment.json").read_text())
    assert marker["version"] == "topos-owner-projection-enrollment/v2" and marker["state"] == "active"
    assert marker["authority_digest"] == digest_stream(Rows(rows)) == flat_digest(rows)
    with owner():
        assert service.read(EvidenceLookup(fact_id=corpus[2]), now=1201).qualification.verdict == "reviewed"
        assert service.with_reviewed(corpus[2], now=1201, callback=lambda *_args: "released") == "released"
        revoked = service.revoke(RevokeProjectionReview(fact_id=corpus[2], review_id=recorded.review_id,
            expected_review_revision=recorded.review_revision), now=1202)
    assert revoked.action == "revoked"
    runtime.close()
    reopened = load_runtime(path, active_database=corpus[0].path)
    try:
        with owner():
            state = reopened.projection_reviews().read(EvidenceLookup(fact_id=corpus[2]), now=1203)
        assert state.current_review is None and state.qualification.verdict == "withheld"
    finally:
        reopened.close()


# --- the digest value itself --------------------------------------------------------------------------


def test_the_authority_digest_is_the_pinned_value_over_review_id_order(tmp_path):
    """A known answer over a fixed store, inserted so that rowid order differs from review_id order.

    This is what a changed column set, changed framing, or an ORDER BY rowid would break.
    `fact_reviews` is a rowid table and VACUUM renumbers rowids, so ordering by rowid would
    turn routine maintenance into a rollback refusal.
    """
    path = tmp_path / "known.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE fact_reviews(review_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL,"
                     "review_json TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)))")
        conn.executemany("INSERT INTO fact_reviews VALUES(?,?,?,?)", [
            ("review-b", "fact-2", '{"n":2}', 1), ("review-a", "fact-1", '{"n":1}', 0),
            ("review-c", "fact-1", '{"n":3}', 1)])
    with sqlite3.connect(path) as conn:
        assert EvidenceReviewStore._authority_digest(conn) == KNOWN_ANSWER == legacy_digest(conn)
        conn.execute("VACUUM")
    with sqlite3.connect(path) as conn:
        assert EvidenceReviewStore._authority_digest(conn) == KNOWN_ANSWER


def test_an_empty_store_digests_as_the_empty_array(enrolled):
    assert json.loads(enrolled.marker.read_text())["authority_digest"] == EMPTY_DIGEST == digest([])


# --- detection: every row-level case, at both sizes ------------------------------------------------------


@pytest.mark.parametrize("size", SIZES)
def test_an_older_store_restored_in_place_is_refused_as_rollback(enrolled, size):
    current = sized(enrolled, size)
    older = enrolled.store.read_bytes()
    with owner():
        enrolled.service.revoke(RevokeEvidenceReview(fact_id=enrolled.corpus[2], review_id="seed-review-2",
            expected_review_revision=current.review_revision))
    before = enrolled.marker.read_bytes()
    enrolled.store.write_bytes(older)
    assert enrolled.read_codes() == ("review_store_rollback",) * 3
    assert enrolled.marker.read_bytes() == before


@pytest.mark.parametrize("size", SIZES)
def test_an_added_current_row_is_refused_as_rollback(enrolled, size):
    sized(enrolled, size)
    tamper(enrolled.store, "INSERT INTO fact_reviews VALUES('planted','fact-x','{}',1)")
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_an_added_retired_row_is_refused_as_rollback(enrolled, size):
    sized(enrolled, size)
    tamper(enrolled.store, "INSERT INTO fact_reviews VALUES('planted','fact-x','{}',0)")
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_a_deleted_current_row_is_refused_as_rollback(enrolled, size):
    sized(enrolled, size)
    tamper(enrolled.store, "DELETE FROM fact_reviews WHERE review_id='seed-review-2'")
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_a_deleted_retired_row_is_refused_as_rollback(enrolled, size):
    """Retired rows are inside the digest, and they are what refuses a replayed review id."""
    sized(enrolled, size)
    tamper(enrolled.store, "DELETE FROM fact_reviews WHERE review_id='seed-review-1'")
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_an_edited_review_body_is_refused_as_rollback(enrolled, size):
    sized(enrolled, size)
    tamper(enrolled.store, "UPDATE fact_reviews SET review_json=replace(review_json,'personal','none')")
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_editing_the_oldest_retired_row_is_refused_as_rollback(enrolled, size):
    """Deep history is digested too: the first row by review_id is as pinned as the newest."""
    sized(enrolled, size)
    oldest = min(row[0] for row in enrolled.rows())
    tamper(enrolled.store, "UPDATE fact_reviews SET review_json=review_json||' ' WHERE review_id=?", (oldest,))
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_a_reactivated_retired_review_is_refused_as_rollback(enrolled, size):
    sized(enrolled, size)
    tamper(enrolled.store, "UPDATE fact_reviews SET active=1 WHERE review_id='seed-review-1'")
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_retiring_the_current_review_is_refused_as_rollback(enrolled, size):
    sized(enrolled, size)
    tamper(enrolled.store, "UPDATE fact_reviews SET active=0 WHERE review_id='seed-review-2'")
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_swapping_two_active_flags_is_refused_as_rollback(enrolled, size):
    sized(enrolled, size)
    tamper(enrolled.store, "UPDATE fact_reviews SET active=1-active "
                           "WHERE review_id IN ('seed-review-1','seed-review-2')")
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_a_renamed_review_id_is_refused_as_rollback(enrolled, size):
    """Content moved to another id changes that row's key and its place in the digested order."""
    sized(enrolled, size)
    tamper(enrolled.store, "UPDATE fact_reviews SET review_id='zzz-renamed' WHERE review_id='seed-review-1'")
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_a_changed_fact_id_is_refused_as_rollback(enrolled, size):
    sized(enrolled, size)
    tamper(enrolled.store, "UPDATE fact_reviews SET fact_id='other-fact' WHERE review_id='seed-review-2'")
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_a_review_body_stored_as_a_blob_is_refused_as_a_type(enrolled, size):
    """Type strictness is unchanged: the streamed encoder refuses exactly what canonical refuses."""
    sized(enrolled, size)
    tamper(enrolled.store, "UPDATE fact_reviews SET review_json=CAST(review_json AS BLOB) "
                           "WHERE review_id='seed-review-2'")
    assert enrolled.read_codes() == ("json_type",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_a_physical_reorder_with_the_same_rows_stays_qualified(enrolled, size):
    """VACUUM renumbers rowids. Digesting in review_id order keeps that benign, as it was."""
    sized(enrolled, size)
    with sqlite3.connect(enrolled.store) as conn:
        conn.execute("VACUUM")
    before = enrolled.marker.read_bytes()
    assert enrolled.read_codes() == ("ok", "ok", "ok")
    assert enrolled.marker.read_bytes() == before


@pytest.mark.parametrize("size", SIZES)
def test_a_different_store_at_the_enrolled_path_is_refused_as_binding(enrolled, size, tmp_path):
    """Store identity swap: the marker pins `store_id`, which a fresh store cannot reproduce."""
    sized(enrolled, size)
    reopened = None
    try:
        enrolled.runtime.close()
        enrolled.store.rename(tmp_path / "moved-aside.db")
        with owner():
            EvidenceReviewStore(enrolled.store, resolver=enrolled.corpus[0])
        reopened = load_runtime(enrolled.path, active_database=enrolled.corpus[0].path)
        with owner(), pytest.raises(PolicyError, match="review_database_binding"):
            reopened.evidence_reviews(require_existing=True)
    finally:
        if reopened is not None:
            reopened.close()


@pytest.mark.parametrize("size", SIZES)
def test_a_store_id_edited_inside_the_file_is_refused_as_binding(enrolled, size):
    sized(enrolled, size)
    tamper(enrolled.store, "UPDATE review_identity SET store_id=? WHERE singleton=1", ("f" * 64,))
    assert enrolled.read_codes() == ("review_database_binding",) * 3


@pytest.mark.parametrize("size", SIZES)
def test_an_older_marker_restored_alone_is_refused(enrolled, size):
    current = sized(enrolled, size)
    older_marker = enrolled.marker.read_bytes()
    with owner():
        enrolled.service.revoke(RevokeEvidenceReview(fact_id=enrolled.corpus[2], review_id="seed-review-2",
            expected_review_revision=current.review_revision))
    enrolled.marker.write_bytes(older_marker)
    # A running process refuses the revision regression; a fresh one has no cached revision,
    # so it compares the older marker with the newer rows and calls that a rollback.
    assert enrolled.read_codes() == ("ok", "review_enrollment_unavailable", "review_store_rollback")


@pytest.mark.parametrize("damage,restarted", [("pending", "review_enrollment_unavailable"),
    ("deleted", "review_enrollment_unavailable"), ("group_readable", "review_enrollment_unavailable"),
    ("symlink", "review_database_binding")])
def test_marker_faults_keep_closing_enrollment_on_a_grown_store(enrolled, damage, restarted, tmp_path):
    """The marker side of the floor, on a store the old digest could not have encoded at all.

    A symlinked marker is refused one step earlier, by `load_runtime`'s private-path check.
    """
    seed(enrolled, filler=OVER_CAP)
    if damage == "pending":
        body = json.loads(enrolled.marker.read_text())
        body["state"] = "pending"
        enrolled.marker.write_text(json.dumps(body))
    elif damage == "deleted":
        enrolled.marker.unlink()
    elif damage == "group_readable":
        enrolled.marker.chmod(0o644)
    else:
        body = enrolled.marker.read_bytes()
        elsewhere = tmp_path / "marker-elsewhere.json"
        elsewhere.write_bytes(body)
        elsewhere.chmod(0o600)
        enrolled.marker.unlink()
        enrolled.marker.symlink_to(elsewhere)
    # The already-built service keeps serving a store it has verified, as it does today;
    # every path that re-reads the marker refuses.
    assert enrolled.read_codes() == ("ok", "review_enrollment_unavailable", restarted)


# --- detection: the schema pin -------------------------------------------------------------------------


REVIVE_SEED_1 = "UPDATE fact_reviews SET active=1 WHERE review_id='seed-review-1'"


def bump_schema(conn):
    """Make the next connection re-read `sqlite_master` after a `writable_schema` edit."""
    conn.execute("PRAGMA schema_version=%d" % (conn.execute("PRAGMA schema_version").fetchone()[0] + 1))


def plant_schema_row(store, kind, name, sql, *, table="fact_reviews"):
    """Write one `sqlite_master` row directly, so the `type` spelling is the attacker's to choose.

    `CREATE TRIGGER` and `CREATE VIEW` always store `type` lower-cased, so a pin that
    compares `type` under SQLite's default BINARY collation can only be reached this way.
    SQLite itself refuses to load a schema row whose `type` is not a case variant of the
    kind its `sql` declares ("malformed database schema"), so the case variants below are
    the whole reachable set -- and they are exactly what such a pin misses. Verified on
    SQLite 3.47.1: with `type='TRIGGER'` the trigger is installed and fires.
    """
    with sqlite3.connect(store) as conn:
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) VALUES(?,?,?,0,?)",
                     (kind, name, table, sql))
        bump_schema(conn)
        conn.execute("PRAGMA writable_schema=OFF")


@pytest.mark.parametrize("planted", ["insert_trigger", "identity_trigger", "view",
                                     "upper_case_trigger", "mixed_case_trigger", "upper_case_view"])
def test_a_trigger_or_view_planted_in_the_store_is_refused_as_binding(enrolled, planted):
    """A trigger fires inside a legitimate owner write, so the exit digest would publish its work.

    Nothing the engine writes needs a trigger or a view in this file, and the digest covers
    rows rather than schema, so a planted one is laundered into the marker on the next
    record. Refusing every object the store did not itself create is the cheap way to close
    that, and it has to be independent of how the planted row spells its own kind: a pin
    written as `type IN ('trigger','view')` is bypassed by capitalising one word, while the
    trigger still fires.
    """
    sized(enrolled, "small")
    if planted == "insert_trigger":
        tamper(enrolled.store, "CREATE TRIGGER revive AFTER INSERT ON fact_reviews BEGIN "
                               + REVIVE_SEED_1 + "; END")
    elif planted == "identity_trigger":
        tamper(enrolled.store, "CREATE TRIGGER revive AFTER UPDATE ON review_identity BEGIN "
                               + REVIVE_SEED_1 + "; END")
    elif planted == "view":
        tamper(enrolled.store, "CREATE VIEW fact_reviews_all AS SELECT * FROM fact_reviews")
    elif planted == "upper_case_trigger":
        plant_schema_row(enrolled.store, "TRIGGER", "revive",
                         "CREATE TRIGGER revive AFTER UPDATE ON review_identity BEGIN " + REVIVE_SEED_1 + "; END",
                         table="review_identity")
    elif planted == "mixed_case_trigger":
        plant_schema_row(enrolled.store, "TrIgGeR", "revive",
                         "CREATE TRIGGER revive AFTER INSERT ON fact_reviews BEGIN " + REVIVE_SEED_1 + "; END")
    else:
        plant_schema_row(enrolled.store, "VIEW", "fact_reviews_all",
                         "CREATE VIEW fact_reviews_all AS SELECT * FROM fact_reviews", table="fact_reviews_all")
    before = enrolled.marker.read_bytes()
    with owner():
        assert code(lambda: enrolled.service.record(next_request(enrolled, "would-fire"),
                                                    now=1400)) == "review_database_binding"
    assert enrolled.read_codes() == ("review_database_binding",) * 3
    assert enrolled.marker.read_bytes() == before
    with sqlite3.connect(enrolled.store) as conn:
        assert conn.execute("SELECT active FROM fact_reviews WHERE review_id='seed-review-1'").fetchone()[0] == 0


def test_the_store_holds_exactly_the_objects_the_pin_expects(enrolled):
    """The expected set is the store's own schema, so the pin cannot drift away from it."""
    sized(enrolled, "small")
    with sqlite3.connect(enrolled.store) as conn:
        found = {(row[0], row[1]) for row in conn.execute("SELECT type,name FROM sqlite_master")}
    assert found == evidence_module.EvidenceReviewStore._schema_objects
    assert ("index", "fact_reviews_current") in found, "the current-review index must exist to be trusted"
    assert enrolled.read_codes() == ("ok", "ok", "ok")


@pytest.mark.parametrize("damage,expected", [
    ("extra_table", ("review_database_binding",) * 3),
    ("extra_index", ("review_database_binding",) * 3),
    ("renamed_index", ("review_database_binding",) * 3),
    # A missing index is the one case the engine repairs rather than refuses: the reopen
    # creates it before the pin, which is what lets a store an older engine wrote open at
    # all. The already-built store still refuses, because its own transaction sees the gap.
    ("dropped_index", ("review_database_binding", "review_database_binding", "ok")),
])
def test_any_object_the_store_did_not_create_is_refused(enrolled, damage, expected):
    """Deny by default, because the digest covers rows and never schema.

    An index decides which row a predicate is answered with, so an unexpected one is as
    consequential here as a trigger; a kind this engine does not know about must fail
    closed rather than be allowed by omission.
    """
    sized(enrolled, "small")
    before = enrolled.marker.read_bytes()
    if damage == "extra_table":
        tamper(enrolled.store, "CREATE TABLE spare(a)")
    elif damage == "extra_index":
        tamper(enrolled.store, "CREATE INDEX fact_reviews_spare ON fact_reviews(active,fact_id)")
    elif damage == "renamed_index":
        tamper(enrolled.store, "DROP INDEX fact_reviews_current")
        tamper(enrolled.store, "CREATE INDEX fact_reviews_other ON fact_reviews(fact_id,active)")
    else:
        tamper(enrolled.store, "DROP INDEX fact_reviews_current")
    assert enrolled.read_codes() == expected
    assert enrolled.marker.read_bytes() == before


def test_a_stale_current_review_index_never_decides_which_review_is_current(enrolled):
    """The index is now the engine's own artefact, so a poisoned one must not be trusted.

    `WHERE fact_id=? AND active=1` is answered from `fact_reviews_current`'s keys alone, and
    the authority digest covers table rows only -- never the index, never `sqlite_master`. A
    b-tree left pointing at a revoked row therefore serves that review while the digest still
    matches the marker byte for byte and the object set is exactly the expected one. Only
    re-reading the row out of the table catches it.
    """
    seed(enrolled, filler=0)
    fact_id = enrolled.corpus[2]
    truth = enrolled.rows()
    bump = bump_schema
    # Flip the flags with the index visible, so its b-tree records the revoked review as
    # current; detach the index; put the rows back with it invisible; re-attach the stale
    # b-tree under its own name and definition. The rows are the truth, the index is a lie.
    with sqlite3.connect(enrolled.store) as conn:
        kept = conn.execute("SELECT rootpage,sql,tbl_name FROM sqlite_master "
                            "WHERE name='fact_reviews_current'").fetchone()
        conn.execute("UPDATE fact_reviews SET active=0 WHERE review_id='seed-review-2'")
        conn.execute("UPDATE fact_reviews SET active=1 WHERE review_id='seed-review-1'")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("DELETE FROM sqlite_master WHERE name='fact_reviews_current'")
        bump(conn)
        conn.execute("PRAGMA writable_schema=OFF")
    with sqlite3.connect(enrolled.store) as conn:
        conn.execute("UPDATE fact_reviews SET active=0 WHERE review_id='seed-review-1'")
        conn.execute("UPDATE fact_reviews SET active=1 WHERE review_id='seed-review-2'")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) VALUES('index',"
                     "'fact_reviews_current',?,?,?)", (kept[2], kept[0], kept[1]))
        bump(conn)
        conn.execute("PRAGMA writable_schema=OFF")
    # The rows, and so the digest and the marker, are untouched; the object set is expected.
    assert enrolled.rows() == truth
    with sqlite3.connect(enrolled.store) as conn:
        assert {(row[0], row[1]) for row in conn.execute("SELECT type,name FROM sqlite_master")} == \
            evidence_module.EvidenceReviewStore._schema_objects
        assert json.loads(enrolled.marker.read_text())["authority_digest"] == digest_stream(Rows(truth))
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN SELECT rowid FROM fact_reviews "
                                               "WHERE fact_id=? AND active=1", (fact_id,))]
        assert any("fact_reviews_current" in step for step in plan), "the poisoned index must be in the plan"
        through_index = conn.execute("SELECT review_id FROM fact_reviews WHERE fact_id=? AND active=1",
                                     (fact_id,)).fetchall()
        assert through_index == [("seed-review-1",)], "the index has to be serving the revoked review"
    # And the engine refuses rather than serving it.
    assert enrolled.read_codes() == ("review_database_binding",) * 3
    with owner():
        assert enrolled.corpus[0].qualify(fact_id, reviews=enrolled.reviews).reason_code == "review_database_binding"


def test_two_active_rows_for_one_fact_are_refused_rather_than_guessed(tmp_path):
    """`_current_row` reads a second rowid before answering, in both stores.

    Nothing the engine writes can leave two active rows on one fact -- recording retires the
    current one inside the same transaction -- so this is a tampered or half-written store,
    and the row a poisoned index happens to yield first must not become the owner's current
    review by default. The output store refuses through the same read, under its own code,
    because `output_review_ambiguous` is what its callers report.
    """
    path = tmp_path / "ambiguous.db"
    with sqlite3.connect(path) as conn:
        conn.execute(TABLE_SQL)
        conn.executemany("INSERT INTO fact_reviews VALUES(?,?,?,?)", [
            ("review-a", "fact-1", '{"body":"first"}', 1),
            ("review-b", "fact-1", '{"body":"second"}', 1),
            ("review-c", "fact-2", '{"body":"only"}', 1),
            ("review-d", "fact-2", '{"body":"retired"}', 0)])
    with sqlite3.connect(path) as conn:
        assert code(lambda: EvidenceReviewStore._current_row(conn, "fact-1")) == "review_ambiguous"
        assert code(lambda: ProjectionReviewStore._current_in(conn, "fact-1")) == "output_review_ambiguous"
        # One active row still answers, and a fact with none still answers `None`, so the
        # refusal is about the second row and nothing else.
        assert EvidenceReviewStore._current_row(conn, "fact-2") == '{"body":"only"}'
        assert EvidenceReviewStore._current_row(conn, "fact-3") is None


# --- detection: the index the digest itself enumerates with -------------------------------------------------
#
# Hardening `_current_row` moved the serving lookup onto the table b-tree but left the digest
# enumerating with `sqlite_autoindex_fact_reviews_1`, so the two came to disagree in the
# attacker's favour: a row in the table and not in that autoindex is invisible to the digest
# and visible to the rowid re-read. These tests reproduce exactly that, both ways round.


PK_INDEX = "sqlite_autoindex_fact_reviews_1"
# Byte-identical to the store's own `CREATE TABLE`; asserted against it below, so a schema
# change here cannot quietly stop the attack from reproducing and start passing by accident.
TABLE_SQL = ("CREATE TABLE fact_reviews(review_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL,"
             "review_json TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)))")
# The same table with the PRIMARY KEY dropped: same columns, types, NOT NULLs and CHECK, so
# the row written through it is a row the real table accepts -- and with no primary key to
# maintain, the engine writes it to the table b-tree alone.
PK_LESS_SQL = ("CREATE TABLE fact_reviews(review_id TEXT NOT NULL,fact_id TEXT NOT NULL,"
               "review_json TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)))")


@contextmanager
def the_pk_index_detached(store):
    """Write to the table b-tree with the primary-key autoindex detached, then re-attach it.

    The same `PRAGMA writable_schema` detach/re-attach the stale-index case above uses, aimed
    at the autoindex instead. Deleting that `sqlite_master` row on its own is not enough --
    SQLite reports "database disk image is malformed" on the next write, because the table's
    own `sql` still declares a PRIMARY KEY with no index to put it in -- so the table's `sql`
    is swapped for the PK-less spelling for exactly one connection and restored byte for
    byte afterwards. `fact_reviews_current` stays attached throughout, so a row written here
    does get a key there: that is what carries it into `_current_row`'s lookup. Afterwards the
    file's `(type, name)` object set, the table's `sql`, and every row the digest can reach
    are what they were, and `PRAGMA integrity_check` is the only thing that says otherwise.
    """
    with sqlite3.connect(store) as conn:
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='fact_reviews'").fetchone()[0] == TABLE_SQL
        rootpage = conn.execute("SELECT rootpage FROM sqlite_master WHERE name=?", (PK_INDEX,)).fetchone()[0]
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET sql=? WHERE type='table' AND name='fact_reviews'", (PK_LESS_SQL,))
        conn.execute("DELETE FROM sqlite_master WHERE name=?", (PK_INDEX,))
        bump_schema(conn)
        conn.execute("PRAGMA writable_schema=OFF")
    try:
        yield
    finally:
        with sqlite3.connect(store) as conn:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute("UPDATE sqlite_master SET sql=? WHERE type='table' AND name='fact_reviews'", (TABLE_SQL,))
            conn.execute("INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) "
                         "VALUES('index',?,'fact_reviews',?,NULL)", (PK_INDEX, rootpage))
            bump_schema(conn)
            conn.execute("PRAGMA writable_schema=OFF")


def hide_row_from_the_pk_index(store, row):
    """Insert `row` into the table b-tree alone: an extra row no index entry names."""
    with the_pk_index_detached(store):
        with sqlite3.connect(store) as conn:
            conn.execute("INSERT INTO fact_reviews VALUES(?,?,?,?)", row)


def diverge_a_row_from_its_key(store, review_id, cell):
    """Edit a row's stored `review_id` behind the autoindex, leaving its key pointing at the row.

    The index still holds one entry `review_id` -> that rowid, so every lookup that matches on
    the key still finds the row; what changed is the cell inside the row the marker is supposed
    to pin. This is the corruption the plain ordered walk could not see, because it read that
    one column out of the index entry rather than out of the row.
    """
    with the_pk_index_detached(store):
        with sqlite3.connect(store) as conn:
            conn.execute("UPDATE fact_reviews SET review_id=? WHERE review_id=?", (cell, review_id))


def delete_a_row_behind_the_pk_index(store, review_id):
    """Delete a row with the autoindex detached: its entry survives with no row to answer for."""
    with the_pk_index_detached(store):
        with sqlite3.connect(store) as conn:
            conn.execute("DELETE FROM fact_reviews WHERE review_id=?", (review_id,))


def revive_a_revoked_review_invisibly(store, review_id, fact_id):
    """Put the revoked review's own stored body back as an active row the digest cannot see.

    Nothing about the body is forged: it is the bytes the owner's own `record_review` wrote,
    which is what makes this a test of the enumeration rather than of body validation. The
    row keeps the review's id, so the autoindex's one key for that id still points at the
    retired row the marker was taken over.
    """
    with sqlite3.connect(store) as conn:
        body = conn.execute("SELECT review_json FROM fact_reviews WHERE review_id=?", (review_id,)).fetchone()[0]
    hide_row_from_the_pk_index(store, (review_id, fact_id, body, 1))


def uncrosschecked_digest(db):
    """`_authority_digest` without the row-count cross-check: the shape that shipped the hole."""
    return digest_stream(Rows(db.execute(evidence_module._REVIEW_SELECT)))


@contextmanager
def trusting_the_pk_index():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(EvidenceReviewStore, "_authority_digest", staticmethod(uncrosschecked_digest))
        yield patch


def key_sourced_digest(db):
    """`_authority_digest` over the plain ordered walk: the shape that shipped the cell hole.

    Cross-check and all, so the only difference from the shipped function is where the
    `review_id` cell of each row comes from -- the index entry's key here, the table row
    there.
    """
    counted = evidence_module._Counted(db.execute(SELECT))
    result = digest_stream(Rows(counted))
    if counted.count != db.execute(evidence_module._REVIEW_COUNT).fetchone()[0]:
        raise PolicyError("review_database_binding")
    return result


@contextmanager
def trusting_the_index_key():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(EvidenceReviewStore, "_authority_digest", staticmethod(key_sourced_digest))
        yield patch


def test_the_digest_enumerates_with_an_index_and_reads_the_table(enrolled):
    """The two statements the cross-check rests on, pinned by their plans.

    The ordered read still walks the primary-key autoindex -- that is the whole reason a
    cross-check is needed -- but every cell it hashes is read out of the table row that walk
    seeks, not out of the index entry, which is what `SEARCH t USING INTEGER PRIMARY KEY`
    says. The plain walk below is the spelling that shipped the cell hole: its single plan
    line is the index, and its `review_id` comes from the index key. `NOT INDEXED` is what
    makes the count the table b-tree's own answer; a plain `count(*)` is answered from the
    very index the enumeration uses, so it would agree with the digest about a row neither
    can see. `fact_reviews` is also the only table in either store with an autoindex to hide
    a row from: `review_identity` and `projection_contract` key on `INTEGER PRIMARY KEY`,
    which is the rowid, so their reads go to the table b-tree already.
    """
    sized(enrolled, "small")
    with sqlite3.connect(enrolled.store) as conn:
        def plan(sql):
            return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]

        assert plan(evidence_module._REVIEW_SELECT) == [
            "SCAN i USING COVERING INDEX " + PK_INDEX,
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN"]
        assert plan(SELECT) == ["SCAN fact_reviews USING INDEX " + PK_INDEX], "the spelling it replaced"
        # No sorter: the order is the index's, so no owner review body goes through a temp
        # b-tree. `... FROM fact_reviews NOT INDEXED ORDER BY review_id` reads all four cells
        # from the table too and needs no count, but puts every body through one -- about 27 MB
        # of them at 10,000 M1-shaped rows -- and measured 114 ms there against 87 ms.
        assert plan("SELECT review_id,fact_id,review_json,active FROM fact_reviews NOT INDEXED "
                    "ORDER BY review_id") == ["SCAN fact_reviews", "USE TEMP B-TREE FOR ORDER BY"]
        assert not any("TEMP B-TREE" in line for line in plan(evidence_module._REVIEW_SELECT))
        assert plan(evidence_module._REVIEW_COUNT) == ["SCAN fact_reviews"]
        assert "INDEX" in plan("SELECT count(*) FROM fact_reviews")[0], "the cross-check cannot use this one"
        assert plan(evidence_module._IDENTITY_SELECT) == ["SEARCH review_identity USING INTEGER PRIMARY KEY (rowid=?)"]
    autoindexes = {name for kind, name in ProjectionReviewStore._schema_objects
                   if kind == "index" and name.startswith("sqlite_autoindex_")}
    assert autoindexes == {PK_INDEX}, "a new keyed table needs the same cross-check"


def test_the_digest_is_the_same_value_the_plain_walk_gave(tmp_path):
    """Reading the cells out of the table changed which b-tree they come from, not the value.

    Ordering is still the index's, so the two spellings agree row for row on any store SQLite
    itself would accept -- including keys the campaign never writes, where a Python-side sort
    and SQLite's own collation could differ: NULL, empty, case variants, digits and non-ASCII.
    """
    path = tmp_path / "keys.db"
    keys = [None, "", "A", "a", "Z", "z", "zz", "0", "é", "É", "r-10", "r-2"]
    with sqlite3.connect(path) as conn:
        conn.execute(TABLE_SQL)
        conn.executemany("INSERT INTO fact_reviews VALUES(?,?,?,?)",
                         [(key, "fact-%d" % index, '{"body":%d}' % index, index % 2)
                          for index, key in enumerate(keys)])
    with sqlite3.connect(path) as conn:
        walked = [list(row) for row in conn.execute(SELECT)]
        joined = [list(row) for row in conn.execute(evidence_module._REVIEW_SELECT)]
        assert joined == walked, "same rows, same order"
        assert EvidenceReviewStore._authority_digest(conn) == digest_stream(Rows(walked)) == flat_digest(walked)
        assert len(walked) == len(keys)


def test_a_row_hidden_from_the_primary_key_index_is_never_served(enrolled):
    """A revoked review, put back as a row the digest's enumeration cannot reach, must not be served.

    `_current_row` resolves a rowid through `fact_reviews_current` and re-reads the row out
    of the TABLE, while `_authority_digest` enumerates through the primary-key autoindex. A
    row written into the table b-tree alone is therefore served as the owner's current
    review while the digest, and so the marker, is unchanged byte for byte -- and while the
    object set, the table's `sql` and every row any digested read returns are all exactly
    what they were. Comparing the streamed row count with the table's own count is what
    closes it; the arm that patches the cross-check out is the reproduction of the attack,
    so this test fails rather than quietly passing if the cross-check stops doing the work.
    """
    current = seed(enrolled, filler=0)
    fact_id = enrolled.corpus[2]
    with owner():
        enrolled.service.revoke(RevokeEvidenceReview(fact_id=fact_id, review_id="seed-review-2",
            expected_review_revision=current.review_revision))
        assert enrolled.service.read(enrolled.lookup).current_review is None, "the owner revoked it"
    truth, before = enrolled.rows(), enrolled.marker.read_bytes()

    revive_a_revoked_review_invisibly(enrolled.store, "seed-review-2", fact_id)

    # Nothing the digest, the schema pin or the marker looks at has moved.
    assert enrolled.rows() == truth
    with sqlite3.connect(enrolled.store) as conn:
        assert {(row[0], row[1]) for row in conn.execute("SELECT type,name FROM sqlite_master")} == \
            evidence_module.EvidenceReviewStore._schema_objects
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='fact_reviews'").fetchone()[0] == TABLE_SQL
        assert json.loads(enrolled.marker.read_text())["authority_digest"] == digest_stream(Rows(truth))
        # The row is really in the table, it is what the current-review lookup now finds, and
        # only a scan that refuses every index can count it.
        assert conn.execute(evidence_module._REVIEW_COUNT).fetchone()[0] == len(truth) + 1
        assert conn.execute("SELECT count(*) FROM fact_reviews").fetchone()[0] == len(truth)
        assert conn.execute("SELECT review_id FROM fact_reviews WHERE fact_id=? AND active=1",
                            (fact_id,)).fetchall() == [("seed-review-2",)]
        damage = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        assert any("missing from index " + PK_INDEX in line for line in damage), damage

    # The attack, reproduced: without the cross-check all three ways serve the revoked review.
    with trusting_the_pk_index():
        assert enrolled.read_revisions() == (current.review_revision,) * 3
    # And with it, all three refuse, as tamper rather than as a transient fault.
    assert enrolled.read_revisions() == ("review_database_binding",) * 3
    with owner():
        assert enrolled.corpus[0].qualify(fact_id, reviews=enrolled.reviews).reason_code == "review_database_binding"
    assert enrolled.marker.read_bytes() == before, "a refused store must not republish its marker"


def test_the_output_store_refuses_a_row_hidden_from_its_primary_key_index_too(corpus, projection_runtime):
    """The projection twin, which inherits `_authority_digest` -- pinned rather than assumed.

    Its own shape adds nothing to hide behind: `projection_contract` keys on `INTEGER
    PRIMARY KEY`, so it has no autoindex, and `_contract` reads the whole table and requires
    it to be exactly one known row.
    """
    runtime, config, path = projection_runtime
    lookup = EvidenceLookup(fact_id=corpus[2])
    with owner():
        runtime.evidence_reviews(require_existing=False).record(
            request_for(corpus, "evidence-for-hidden-output"), now=1200)
        service = runtime.projection_reviews(require_existing=False)
        preview = service.preview(lookup, now=1200)
        recorded = service.record(RecordProjectionReview(review_id="output-review-1",
            expected_candidate=preview.candidate, expected_candidate_hash=preview.candidate_hash,
            expected_current_review_revision=None, classification={"domains": ["reading"],
                "sensitivity": "personal", "subject": "self", "assertion": "explicit_atomic_preference"}),
            now=1200)
        service.revoke(RevokeProjectionReview(fact_id=corpus[2], review_id=recorded.review_id,
            expected_review_revision=recorded.review_revision), now=1201)
        assert service.read(lookup, now=1202).current_review is None

    store = Path(config["projection_review_store_path"])
    marker = store.with_name(store.name + ".enrollment.json")
    before = marker.read_bytes()
    revive_a_revoked_review_invisibly(store, recorded.review_id, corpus[2])

    with owner():
        with trusting_the_pk_index():
            revived = service.read(lookup, now=1203)
            assert revived.current_review_revision == recorded.review_revision, "the attack has to reproduce"
        assert code(lambda: service.read(lookup, now=1204)) == "review_database_binding"
        assert code(lambda: service.with_reviewed(corpus[2], now=1204,
                                                  callback=lambda *_args: "released")) == "review_database_binding"
    runtime.close()
    reopened = load_runtime(path, active_database=corpus[0].path)
    try:
        with owner():
            assert code(lambda: reopened.projection_reviews().read(lookup, now=1205)) == "review_database_binding"
    finally:
        reopened.close()
    assert marker.read_bytes() == before


def test_a_review_id_edited_away_from_its_index_key_is_refused(enrolled):
    """Every digested cell comes out of the table row, not out of the index entry that found it.

    The plain ordered walk read `review_id` from `sqlite_autoindex_fact_reviews_1`'s key and
    only the other three columns from the row, so this corruption -- the row's own
    `review_id`, edited behind the index, with the key left pointing at it -- digested as the
    key and left the marker matching byte for byte. Nothing serves that cell today, so it
    disclosed nothing; it was still a cell the marker was believed to pin and did not, which
    is the same class of accident as the hidden row above. The `trusting_the_index_key` arm is
    that spelling, cross-check included, so it isolates exactly where the cell is read from,
    and it has to keep serving the store for this test to be about anything. The output store
    inherits the same function, as the hidden-row twin above pins.
    """
    current = seed(enrolled, filler=0)
    truth, before = enrolled.rows(), enrolled.marker.read_bytes()

    diverge_a_row_from_its_key(enrolled.store, "seed-review-2", "seed-review-2-is-not-this")

    with sqlite3.connect(enrolled.store) as conn:
        # The pin, the count and the old enumeration all still report an untouched store.
        assert {(row[0], row[1]) for row in conn.execute("SELECT type,name FROM sqlite_master")} == \
            EvidenceReviewStore._schema_objects
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='fact_reviews'").fetchone()[0] == TABLE_SQL
        assert conn.execute(evidence_module._REVIEW_COUNT).fetchone()[0] == len(truth)
        assert [list(row) for row in conn.execute(SELECT)] == truth
        assert digest_stream(Rows(conn.execute(SELECT))) == json.loads(enrolled.marker.read_text())["authority_digest"]
        # Only a read that goes to the table sees the edited cell, and only `integrity_check`
        # names it. The key itself is intact: the row is still found by the id the owner knows.
        assert conn.execute("SELECT review_id FROM fact_reviews NOT INDEXED WHERE fact_id=?",
                            (enrolled.corpus[2],)).fetchall() == [("seed-review-1",), ("seed-review-2-is-not-this",)]
        assert conn.execute("SELECT fact_id FROM fact_reviews WHERE review_id='seed-review-2'").fetchall() == \
            [(enrolled.corpus[2],)]
        damage = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        assert any("missing from index " + PK_INDEX in line for line in damage), damage

    with trusting_the_index_key():
        assert enrolled.read_revisions() == (current.review_revision,) * 3, "the hole has to reproduce"
    assert enrolled.read_codes() == ("review_store_rollback",) * 3
    assert enrolled.marker.read_bytes() == before, "a refused store must not republish its marker"


def test_an_index_entry_the_table_cannot_answer_for_is_refused_as_tamper(enrolled):
    """The other direction of the count cross-check: more streamed rows than the table holds.

    A row deleted behind the autoindex leaves an entry naming a rowid the table no longer has.
    The plain walk ended the read with SQLite's "database disk image is malformed" and the
    store came back as the transient `review_storage_unavailable`; the `LEFT JOIN` streams a
    row of NULLs instead, which the count refuses as tamper. The direction matters most at the
    exit digest, where a changed digest is published rather than refused: a stream that grew a
    row there would be written into the marker as the owner's own work, so the comparison is
    an equality rather than "not fewer than".
    """
    seed(enrolled, filler=0)
    truth, before = enrolled.rows(), enrolled.marker.read_bytes()

    delete_a_row_behind_the_pk_index(enrolled.store, "seed-review-1")

    with sqlite3.connect(enrolled.store) as conn:
        assert {(row[0], row[1]) for row in conn.execute("SELECT type,name FROM sqlite_master")} == \
            EvidenceReviewStore._schema_objects
        assert conn.execute(evidence_module._REVIEW_COUNT).fetchone()[0] == len(truth) - 1
        streamed = [list(row) for row in conn.execute(evidence_module._REVIEW_SELECT)]
        assert streamed == [[None, None, None, None], truth[1]], "one entry, no row to answer for"
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(SELECT).fetchall()
        damage = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        assert any("wrong # of entries in index " + PK_INDEX in line for line in damage), damage

    assert enrolled.read_codes() == ("review_database_binding",) * 3
    assert enrolled.marker.read_bytes() == before


# --- the exit digest runs only after a review change ------------------------------------------------------


@pytest.fixture
def digest_calls(monkeypatch):
    calls = []
    original = EvidenceReviewStore._authority_digest

    def counted(db):
        calls.append(None)
        return original(db)

    monkeypatch.setattr(EvidenceReviewStore, "_authority_digest", staticmethod(counted))
    return calls


def test_the_exit_digest_runs_only_when_a_review_row_was_written(enrolled, digest_calls):
    """A transaction that compiled nothing able to change `fact_reviews` cannot have changed one.

    Under BEGIN IMMEDIATE no other connection can commit, and every statement -- trigger
    programs included -- is compiled through the authorizer, so skipping the exit digest
    there cannot hide a change. It is also what stops a release callback republishing the
    marker after rewriting the file underneath an open read.
    """
    seed(enrolled, filler=0)

    def digests(call):
        del digest_calls[:]
        with owner():
            call()
        return len(digest_calls)

    # A read observes the clock (one verifying open that writes no review row) and opens once more.
    assert digests(lambda: enrolled.service.read(enrolled.lookup)) == 2
    assert digests(lambda: enrolled.corpus[0].qualify(enrolled.corpus[2], reviews=enrolled.reviews)) == 2
    # A record adds its own mutating open, the only one that pays an exit digest.
    assert digests(lambda: enrolled.service.record(next_request(enrolled, "counted-record"), now=1500)) == 5


@pytest.mark.parametrize("statement", [
    "UPDATE fact_reviews SET active=0 WHERE review_id='seed-review-2'",
    "INSERT INTO fact_reviews VALUES('inside','fact-i','{}',0)",
    "REPLACE INTO fact_reviews VALUES('seed-review-2','fact-r','{}',1)",
    "DELETE FROM fact_reviews WHERE review_id='seed-review-1'",
    "UPDATE FACT_REVIEWS SET active=0 WHERE review_id='seed-review-2'",
])
def test_a_review_change_inside_the_transaction_still_publishes(enrolled, statement):
    """However the row was written, the marker must move with it -- upper-cased table name included."""
    seed(enrolled, filler=0)
    before = enrolled.revision()
    with enrolled.reviews._db() as db:
        db.execute(statement)
    assert enrolled.revision() == before + 1
    assert json.loads(enrolled.marker.read_text())["authority_digest"] == digest_stream(Rows(enrolled.rows()))
    assert enrolled.read_codes() == ("ok", "ok", "ok")


def test_a_schema_change_inside_the_transaction_is_never_silently_committed(enrolled):
    seed(enrolled, filler=0)
    before_marker, before_rows = enrolled.marker.read_bytes(), enrolled.rows()
    with pytest.raises(PolicyError, match="review_storage_unavailable"):
        with enrolled.reviews._db() as db:
            db.execute("DROP TABLE fact_reviews")
    assert enrolled.marker.read_bytes() == before_marker and enrolled.rows() == before_rows


def test_a_row_change_the_authorizer_never_saw_is_refused(enrolled, monkeypatch):
    """Belt and braces beside the authorizer, for two integers of row accounting.

    The authorizer is a prepare-time hook and `total_changes` is a runtime counter; they
    fail independently. A future path that changed a row without the hook seeing it would
    otherwise skip the exit digest, so the whole transaction is refused instead.
    """
    seed(enrolled, filler=0)

    class Blind(evidence_module._ReviewAccess):
        def __call__(self, *_args):
            return sqlite3.SQLITE_OK

    monkeypatch.setattr(evidence_module, "_ReviewAccess", Blind)
    before_marker, before_rows = enrolled.marker.read_bytes(), enrolled.rows()
    # Reported as tamper, not as the transient `review_storage_unavailable` this file also
    # uses for "could not open" and "SQLite errored": a row that moved outside the engine's
    # accounting means the store must be quarantined, not retried.
    with pytest.raises(PolicyError, match="review_database_binding"):
        with enrolled.reviews._db() as db:
            db.execute("INSERT INTO fact_reviews VALUES('unseen','fact-u','{}',0)")
    assert enrolled.marker.read_bytes() == before_marker and enrolled.rows() == before_rows


def test_the_authorizer_classifies_every_statement_kind(tmp_path):
    """The classification the skipped exit digest rests on, pinned statement by statement."""
    path = tmp_path / "access.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE fact_reviews(review_id TEXT PRIMARY KEY,fact_id TEXT,review_json TEXT,active INTEGER)")
        conn.execute("CREATE TABLE review_identity(singleton INTEGER PRIMARY KEY,highest_generation INTEGER)")
        conn.execute("INSERT INTO review_identity VALUES(1,0)")
        conn.execute("INSERT INTO fact_reviews VALUES('r1','f1','{}',1)")

    def classify(statements, *, prepare_first=None):
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if prepare_first:
                conn.execute(prepare_first)
            access = evidence_module._ReviewAccess()
            conn.set_authorizer(access)
            for statement in statements:
                conn.execute(statement)
            return access
        finally:
            conn.rollback()
            conn.close()

    def touched(statements, *, prepare_first=None):
        return classify(statements, prepare_first=prepare_first).touched

    def wrote(statements):
        return classify(statements).wrote

    for statement in [evidence_module._REVIEW_SELECT,  # the exit digest compiles this one here
                      "SELECT review_id,fact_id,review_json,active FROM fact_reviews ORDER BY review_id",
                      "SELECT count(*) FROM fact_reviews",
                      evidence_module._REVIEW_COUNT,
                      "SELECT type,name FROM sqlite_master",
                      "UPDATE review_identity SET highest_generation=7 WHERE singleton=1"]:
        assert touched([statement]) is False, statement
    # `wrote` is the other half, and it is what decides whether the `total_changes`
    # cross-check runs at all: a read must leave it False, or the cross-check is dead
    # code on every path in the file.
    for statement, expected in [(evidence_module._REVIEW_SELECT, False),
                                ("SELECT count(*) FROM fact_reviews", False),
                                (evidence_module._REVIEW_COUNT, False),
                                ("SELECT type,name FROM sqlite_master", False),
                                ("UPDATE review_identity SET highest_generation=7 WHERE singleton=1", True),
                                ("INSERT INTO fact_reviews VALUES('r4','f4','{}',1)", True)]:
        assert wrote([statement]) is expected, statement
    for statement in ["INSERT INTO fact_reviews VALUES('r2','f2','{}',1)",
                      "UPDATE fact_reviews SET active=0 WHERE review_id='absent'",  # zero rows, still a write
                      "DELETE FROM fact_reviews WHERE review_id='r1'",
                      "REPLACE INTO fact_reviews VALUES('r1','f1','{}',0)",
                      "INSERT INTO fact_reviews VALUES('r1','f1','{}',1) "
                      "ON CONFLICT(review_id) DO UPDATE SET active=0",
                      "UPDATE FACT_REVIEWS SET active=0 WHERE review_id='r1'",
                      "DROP TABLE fact_reviews",
                      "ALTER TABLE fact_reviews RENAME TO other_reviews",
                      "CREATE TRIGGER t AFTER UPDATE ON review_identity BEGIN UPDATE fact_reviews SET active=0; END",
                      "PRAGMA writable_schema=ON",
                      "ATTACH DATABASE ':memory:' AS side"]:
        assert touched([statement]) is True, statement
    # A trigger body that writes reviews is compiled through the hook when it fires.
    assert touched(["CREATE TRIGGER t AFTER UPDATE ON review_identity BEGIN UPDATE fact_reviews SET active=0; END",
                    "UPDATE review_identity SET highest_generation=9 WHERE singleton=1"]) is True
    # A statement cached before set_authorizer is expired and recompiled through it.
    assert touched(["INSERT INTO fact_reviews VALUES('r3','f3','{}',1)"],
                   prepare_first="INSERT INTO fact_reviews VALUES('r9','f9','{}',1)") is True
    # An unrecognized action code counts as touched: an unknown statement kind costs one
    # extra digest rather than silently skipping one.
    access = evidence_module._ReviewAccess()
    assert access(9999, "fact_reviews", None, "main", None) == sqlite3.SQLITE_OK and access.touched is True


# --- the exit digest must never re-read a rewritten file ---------------------------------------------------


class _SmallCache:
    """`sqlite3` with a two-page cache, so anything re-read after the entry scan comes off disk.

    The 1 MiB cap kept every store inside SQLite's default ~2 MB page cache, which hid this
    shape. An uncapped store routinely exceeds it, so it has to be tested directly.
    """
    def __init__(self, module):
        self._module = module

    def __getattr__(self, name):
        return getattr(self._module, name)

    def connect(self, *args, **kwargs):
        connection = self._module.connect(*args, **kwargs)
        connection.execute("PRAGMA cache_size=2")
        return connection


def test_an_in_place_restore_during_a_release_callback_is_never_absorbed(enrolled):
    """A release holds the review transaction open across its callback and writes no review row.

    A callback that rewrites the store file underneath that open transaction must not end up
    with its restored rows published into the marker as the owner's current reviews. Because
    the transaction compiled nothing that could change a review row, no exit digest runs at
    all: the marker cannot move, and the next verifying open refuses the restored file.
    """
    current = seed(enrolled, filler=OVER_CAP)
    older = enrolled.store.read_bytes()
    with owner():
        enrolled.service.record(request_for(enrolled.corpus, "newer-review",
            expected=current.review_revision), now=1600)
    before = enrolled.revision()

    def restore(_evidence, _rows):
        enrolled.store.write_bytes(older)
        return "released"

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(evidence_module, "sqlite3", _SmallCache(sqlite3))
        with owner():
            code(lambda: enrolled.corpus[0].with_qualified(enrolled.corpus[2], reviews=enrolled.reviews,
                                                           callback=restore))
    assert enrolled.revision() == before, "a transaction that wrote no review row must not move the marker"
    assert enrolled.read_codes() == ("review_store_rollback",) * 3


# --- crash windows ------------------------------------------------------------------------------------------


def raises(code_name):
    def fail(*_args, **_kwargs):
        raise PolicyError(code_name)
    return fail


@pytest.mark.parametrize("size", SIZES)
def test_a_failed_marker_write_leaves_the_store_usable(enrolled, size):
    sized(enrolled, size)
    before = enrolled.marker.read_bytes()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ReviewEnrollmentRuntime, "publish_pending", raises("review_enrollment_unavailable"))
        with owner(), pytest.raises(PolicyError, match="review_enrollment_unavailable"):
            enrolled.service.record(next_request(enrolled, "never-published"), now=1700)
    assert enrolled.marker.read_bytes() == before
    assert enrolled.read_codes() == ("ok", "ok", "ok")


@pytest.mark.parametrize("size", SIZES)
def test_a_crash_after_pending_and_before_commit_leaves_enrollment_closed(enrolled, size):
    sized(enrolled, size)
    rows_before = enrolled.rows()
    original = ReviewEnrollmentRuntime.publish_pending

    def pending_then_crash(self, value):
        original(self, value)
        raise PolicyError("simulated_crash")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ReviewEnrollmentRuntime, "publish_pending", pending_then_crash)
        with owner(), pytest.raises(PolicyError, match="simulated_crash"):
            enrolled.service.record(next_request(enrolled, "crashing"), now=1700)
    assert json.loads(enrolled.marker.read_text())["state"] == "pending"
    assert enrolled.rows() == rows_before, "the store rolled back to the state the marker no longer names"
    with owner():
        assert code(lambda: enrolled.runtime.evidence_reviews(require_existing=True)) == \
            "review_enrollment_unavailable"
    reopened = enrolled.restart()
    try:
        with owner():
            assert code(lambda: reopened.evidence_reviews(require_existing=True)) == \
                "review_enrollment_unavailable"
    finally:
        reopened.close()


@pytest.mark.parametrize("size", SIZES)
def test_a_crash_after_commit_and_before_active_leaves_a_recoverable_pending_marker(enrolled, size):
    """The pending digest still equals the committed rows under the lab's own uncapped formula.

    That equality is what `recover_durable_identity.py --phase activate-pending` checks, and
    it now holds on stores the capped digest could not encode at all. The lab tool computes
    the same uncapped formula already, so it needs no change.
    """
    sized(enrolled, size)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ReviewEnrollmentRuntime, "publish_active", raises("review_enrollment_unavailable"))
        with owner(), pytest.raises(PolicyError, match="review_enrollment_unavailable"):
            enrolled.service.record(next_request(enrolled, "committed-not-active"), now=1700)
    marker, rows = json.loads(enrolled.marker.read_text()), enrolled.rows()
    assert marker["state"] == "pending"
    assert marker["authority_digest"] == flat_digest(rows) == digest_stream(Rows(rows))
    with owner():
        assert code(lambda: enrolled.runtime.evidence_reviews(require_existing=True)) == \
            "review_enrollment_unavailable"
    reopened = enrolled.restart()
    try:
        with owner():
            assert code(lambda: reopened.evidence_reviews(require_existing=True)) == \
                "review_enrollment_unavailable"
    finally:
        reopened.close()


def test_a_crash_during_first_enrollment_never_creates_a_replacement_store(corpus, paired_runtime):
    runtime, config, path = paired_runtime
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ReviewEnrollmentRuntime, "_replace_marker", raises("simulated_crash"))
        with owner(), pytest.raises(PolicyError, match="simulated_crash"):
            runtime.evidence_reviews(require_existing=False)
    marker = json.loads(Path(config["evidence_review_store_path"] + ".enrollment.json").read_text())
    assert marker["state"] == "pending" and marker["store_id"] is None and marker["authority_digest"] is None
    runtime.close()
    reopened = load_runtime(path, active_database=corpus[0].path)
    try:
        with owner():
            assert code(lambda: reopened.evidence_reviews(require_existing=False)) == \
                "review_enrollment_unavailable"
    finally:
        reopened.close()


# --- compatibility with stores and markers the capped digest wrote --------------------------------------------


def test_a_store_and_marker_written_by_the_capped_digest_open_untouched(corpus, paired_runtime):
    """There is no migration because there is nothing to migrate: the value is byte-identical.

    Every state the v2 engine could commit passed the capped exit digest before its commit,
    so it is at most 1 MiB, and on that whole domain the streamed digest returns the same
    hex. The ordinary entry comparison in `_db` -- under BEGIN IMMEDIATE and the node write
    gate, before any review is served -- is the verification of the old digest, and a store
    that fails it is refused as rollback rather than adopted.
    """
    runtime, config, path = paired_runtime
    store = Path(config["evidence_review_store_path"])
    marker = store.with_name(store.name + ".enrollment.json")
    with capped_digest():
        with owner():
            service = runtime.evidence_reviews(require_existing=False)
            first = service.record(request_for(corpus, "written-by-old-code"), now=1200)
        old_marker_bytes = marker.read_bytes()
        with sqlite3.connect(store) as conn:
            rows = [list(row) for row in conn.execute(SELECT)]
        assert json.loads(old_marker_bytes)["authority_digest"] == digest(rows)

    runtime.close()
    reopened = load_runtime(path, active_database=corpus[0].path)
    try:
        with owner():
            service = reopened.evidence_reviews(require_existing=True)
            assert service.read(EvidenceLookup(fact_id=corpus[2])).current_review_revision == first.review_revision
        assert marker.read_bytes() == old_marker_bytes, "an unchanged store must not rewrite its marker"
        # And the store the old code wrote now grows, keeping its marker version and the
        # formula the lab's recovery phase recomputes.
        grow(service.reviews, m1_rows(corpus[0].binding, OVER_CAP))
        with sqlite3.connect(store) as conn:
            grown = [list(row) for row in conn.execute(SELECT)]
        body = json.loads(marker.read_text())
        assert body["version"] == "topos-owner-evidence-enrollment/v2" and body["state"] == "active"
        assert body["authority_digest"] == flat_digest(grown)
        with pytest.raises(PolicyError, match="json_size"):
            digest(grown)
        with owner():
            assert service.read(EvidenceLookup(fact_id=corpus[2])).qualification.verdict == "qualified"
    finally:
        reopened.close()


def test_a_v2_store_whose_rows_do_not_match_its_v2_digest_is_refused_and_not_adopted(corpus, paired_runtime):
    runtime, config, path = paired_runtime
    store = Path(config["evidence_review_store_path"])
    marker = store.with_name(store.name + ".enrollment.json")
    with capped_digest():
        with owner():
            service = runtime.evidence_reviews(require_existing=False)
            service.record(request_for(corpus, "written-by-old-code"), now=1200)
    tamper(store, "UPDATE fact_reviews SET active=0 WHERE review_id='written-by-old-code'")
    before = marker.read_bytes()
    runtime.close()
    reopened = load_runtime(path, active_database=corpus[0].path)
    try:
        with owner():
            assert code(lambda: reopened.evidence_reviews(require_existing=True).read(
                EvidenceLookup(fact_id=corpus[2]))) == "review_store_rollback"
    finally:
        reopened.close()
    assert marker.read_bytes() == before


def test_a_v2_store_already_past_the_legacy_cap_is_served_only_when_its_marker_matches(corpus, paired_runtime):
    """Decision: the marker stays the trust anchor, so such a store opens, or is refused as rollback.

    The engine cannot have produced one -- a write that crossed the cap rolled back before
    its commit -- so it can only come from a writer outside the engine that also republished
    the marker, which is the same trusted-file boundary as a marker forged to match tampered
    rows. Today every path on such a store fails `json_size`; now it opens if, and only if,
    the rows still hash to the marker. This is the one named change in what a tampered-file
    scenario yields, and it is inherent in removing the cap.
    """
    runtime, config, path = paired_runtime
    with owner():
        service = runtime.evidence_reviews(require_existing=False)
        service.record(request_for(corpus, "before-the-cap"), now=1200)
    store = Path(config["evidence_review_store_path"])
    marker = store.with_name(store.name + ".enrollment.json")
    runtime.close()
    with sqlite3.connect(store) as conn:  # an external writer, never the engine
        conn.executemany("INSERT INTO fact_reviews VALUES(?,?,?,?)",
                         [row[:3] + (0,) for row in m1_rows(corpus[0].binding, OVER_CAP + 50)])
        grown = [list(row) for row in conn.execute(SELECT)]
    assert len(json.dumps(grown, separators=(",", ":"))) > MAX_BYTES

    def reopen_and_read(authority_digest):
        body = json.loads(marker.read_text())
        body["authority_digest"] = authority_digest
        body["revision"] += 1
        marker.write_text(json.dumps(body))
        reopened = load_runtime(path, active_database=corpus[0].path)
        try:
            with owner():
                return code(lambda: reopened.evidence_reviews(require_existing=True).read(
                    EvidenceLookup(fact_id=corpus[2])))
        finally:
            reopened.close()

    assert reopen_and_read("0" * 64) == "review_store_rollback"
    assert reopen_and_read(flat_digest(grown)) == "ok"


def test_downgrading_to_the_capped_digest_after_growth_fails_closed_and_keeps_the_marker(enrolled):
    """An older engine, or a shadow host on an older commit, refuses a grown store rather than resetting it.

    It fails closed, but as `json_size`, which the handler maps to HTTP 400 and the control
    plane to `evidence_request_invalid` -- a client reads that as a bad request rather than a
    node fault. Deploy the engine and any shadow host from the same commit.
    """
    seed(enrolled, filler=OVER_CAP)
    before = enrolled.marker.read_bytes()
    with capped_digest():
        with owner():
            assert code(lambda: enrolled.service.read(enrolled.lookup)) == "json_size"
            assert code(lambda: enrolled.service.record(next_request(enrolled, "on-old-code"),
                                                        now=1800)) == "json_size"
            assert enrolled.corpus[0].qualify(enrolled.corpus[2],
                                              reviews=enrolled.reviews).reason_code == "json_size"
    assert enrolled.marker.read_bytes() == before
    assert enrolled.read_codes() == ("ok", "ok", "ok")


# --- the documented baseline that is deliberately unchanged --------------------------------------------------


def test_an_older_marker_and_older_store_restored_together_are_still_accepted(enrolled):
    """Open issue F02, pinned here so that any change to it is deliberate rather than incidental."""
    current = seed(enrolled, filler=0)
    store_bytes, marker_bytes = enrolled.store.read_bytes(), enrolled.marker.read_bytes()
    with owner():
        enrolled.service.revoke(RevokeEvidenceReview(fact_id=enrolled.corpus[2], review_id="seed-review-2",
            expected_review_revision=current.review_revision))
    enrolled.store.write_bytes(store_bytes)
    enrolled.marker.write_bytes(marker_bytes)
    reopened = enrolled.restart()
    try:
        with owner():
            state = reopened.evidence_reviews(require_existing=True).read(EvidenceLookup(fact_id=enrolled.corpus[2]))
        assert state.current_review_revision == current.review_revision
    finally:
        reopened.close()


# --- the slow-digest tripwire, which is the only compensating control for the residual cost ------


def test_the_slow_digest_warning_is_one_line_per_digest_and_carries_no_review(caplog, tmp_path):
    """The tripwire that says a bounded or incremental scheme is due, pinned line by line.

    It is also a privacy promise about a log line: a duration and a row count, never a
    reviewed fact id and never a review body. The exact format is asserted, so a field
    cannot be added to it without this test failing.
    """
    path = tmp_path / "warned.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE fact_reviews(review_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL,"
                     "review_json TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)))")
        conn.executemany("INSERT INTO fact_reviews VALUES(?,?,?,?)", [
            ("review-a", "fact-secret-1", '{"body":"a reviewed message"}', 1),
            ("review-b", "fact-secret-2", '{"body":"another one"}', 0),
            ("review-c", "fact-secret-3", '{"body":"a third"}', 1)])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(evidence_module, "_DIGEST_WARN_SECONDS", -1.0)
        with caplog.at_level(logging.WARNING, logger="topos.permissions_v2.evidence"):
            with sqlite3.connect(path) as conn:
                EvidenceReviewStore._authority_digest(conn)
                EvidenceReviewStore._authority_digest(conn)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 2, "one line per digest, not one per row"
    for record in warnings:
        message = record.getMessage()
        assert re.fullmatch(r"permissions_v2 review authority digest took \d+ ms over 3 rows", message), message
        assert record.args[1] == 3


def test_a_fast_digest_logs_nothing_at_the_shipped_threshold(caplog, enrolled):
    """And the shipped threshold is pinned, so moving it is a deliberate act.

    0.1 s was about 10,600 rows on the machine this was measured on -- roughly the row
    count at which the per-write cost is already documented as too high, so the warning
    arrives while the numbers still matter rather than at about 26,000 rows. The
    duration is measured around the table-count cross-check as well as the encoding, so
    it stays a statement about what one digest costs rather than about one part of it.
    """
    assert evidence_module._DIGEST_WARN_SECONDS == 0.1
    with caplog.at_level(logging.WARNING, logger="topos.permissions_v2.evidence"):
        current = seed(enrolled, filler=0)
        with owner():
            enrolled.service.read(enrolled.lookup)
            enrolled.service.record(next_request(enrolled, "quiet-record"), now=2000)
    assert [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING] == []
    assert current is not None


class _Clock:
    """`time.monotonic` a test drives, delegating every other `time` call to the real module."""
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class _ScriptedDb:
    """The two statements `_authority_digest` runs, answered from a script and charged a duration.

    A file cannot answer them independently: a store whose primary-key index names more rows
    than the table takes the `PRAGMA writable_schema` tamper the detection tests go through,
    and it cannot be made to spend a chosen number of milliseconds inside one of the two.
    Charging the clock to the count alone is what separates "the warning is measured around
    the cross-check" from "beside it".
    """
    class _Answer:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    def __init__(self, rows, count, *, clock, count_cost):
        self.rows, self.count, self.clock, self.count_cost = rows, count, clock, count_cost

    def execute(self, sql):
        if sql == evidence_module._REVIEW_SELECT:
            return iter(self.rows)
        assert sql == evidence_module._REVIEW_COUNT, sql
        self.clock.now += self.count_cost
        return self._Answer(self.count)


def test_the_slow_digest_warning_is_measured_around_the_cross_check(caplog):
    """The duration is what one whole digest cost, the table count included, compared in seconds.

    "100 ms is about 10,600 rows" is a claim about what a writer pays for a digest, and the
    cross-check is about 7% of it, so a threshold that excluded it would be a tripwire for
    part of the cost. The encoding here is instant and only the count advances the clock, so
    a warning can come from nowhere else -- and the two calls sit either side of the shipped
    0.1 s, which is a duration in seconds and not in milliseconds.
    """
    clock, rows = _Clock(), [("r1", "f1", '{"body":1}', 1), ("r2", "f2", '{"body":2}', 0)]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(evidence_module, "time", clock)
        with caplog.at_level(logging.WARNING, logger="topos.permissions_v2.evidence"):
            slow = EvidenceReviewStore._authority_digest(
                _ScriptedDb(rows, len(rows), clock=clock, count_cost=0.2))
            clock.now = 0.0
            fast = EvidenceReviewStore._authority_digest(
                _ScriptedDb(rows, len(rows), clock=clock, count_cost=0.05))
    assert slow == fast == flat_digest([list(row) for row in rows])
    assert [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING] == \
        ["permissions_v2 review authority digest took 200 ms over 2 rows"]


def test_the_digest_refuses_a_stream_longer_than_the_table_as_well_as_shorter(caplog):
    """The cross-check is an equality, not a floor.

    The shorter direction is the hidden row, reproduced against a real file above. The longer
    one is an index entry the table cannot answer for -- also reproduced against a file, but
    only because `LEFT JOIN` streams it as NULLs; the reason it must refuse rather than fall
    through to the digest comparison is the exit digest, where a changed value is published
    into the marker as the owner's own work rather than refused. Both are one comparison here,
    with no file in the way of it.
    """
    clock, rows = _Clock(), [("r1", "f1", '{"body":1}', 1), ("r2", "f2", '{"body":2}', 0)]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(evidence_module, "time", clock)
        with caplog.at_level(logging.WARNING, logger="topos.permissions_v2.evidence"):
            for stored in (len(rows) - 1, len(rows) + 1):
                # Charged well past the warning threshold, so the refusal is also what stops
                # a store the engine is quarantining from logging a line about itself.
                assert code(lambda: EvidenceReviewStore._authority_digest(
                    _ScriptedDb(rows, stored, clock=clock, count_cost=0.5))) == "review_database_binding"
                clock.now = 0.0
            assert EvidenceReviewStore._authority_digest(
                _ScriptedDb(rows, len(rows), clock=clock, count_cost=0.0)) == flat_digest([list(row) for row in rows])
    assert [record for record in caplog.records if record.levelno == logging.WARNING] == [], \
        "a refused digest logs nothing about the store"


# --- the narrowed retire, and the read that no longer writes -------------------------------------


class _Recording:
    """`sqlite3` with every `execute` recorded as `(sql, rowcount)`.

    The retire and the clock high-water are both plain SQL inside the store, and both
    were changed for cost rather than for a value, so neither moves any digest, any
    marker or any row a reader can see. Recording the statements is the only way a test
    can tell the shipped rule from the one it replaced.
    """
    def __init__(self, module, log):
        self._module, self._log = module, log

    def __getattr__(self, name):
        return getattr(self._module, name)

    def connect(self, *args, **kwargs):
        connection = self._module.connect(*args, **kwargs)
        # Only the store's own read-write connection; the canonical database is opened
        # read-only through the same module and must be handed back untouched.
        if kwargs.get("isolation_level", "") is None:
            return _RecordingConnection(connection, self._log)
        return connection


class _RecordingConnection:
    def __init__(self, connection, log):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_log", log)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)

    def execute(self, sql, *args):
        cursor = self._connection.execute(sql, *args)
        self._log.append((sql, cursor.rowcount))
        return cursor


@contextmanager
def recorded():
    log = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(evidence_module, "sqlite3", _Recording(sqlite3, log))
        yield log


def matching(log, prefix):
    return [entry for entry in log if entry[0].startswith(prefix)]


def test_retiring_a_current_review_rewrites_exactly_one_row(enrolled):
    """Now that nothing caps the retired rows, the retire must not rewrite all of them.

    Setting an already-retired row's `active` to 0 changes no digested byte, so neither the
    authority digest nor the marker moves if this narrowing is reverted -- it is invisible
    to every other test in this file.
    """
    current = seed(enrolled, filler=0)
    with owner():
        for index in range(3):
            current = enrolled.service.record(request_for(enrolled.corpus, f"retire-{index}",
                expected=current.review_revision), now=1900 + index)
    fact_id = enrolled.corpus[2]
    with sqlite3.connect(enrolled.store) as conn:
        already_retired = conn.execute("SELECT count(*) FROM fact_reviews WHERE fact_id=? AND active=0",
                                       (fact_id,)).fetchone()[0]
    assert already_retired >= 3, "the un-narrowed statement would have rewritten these too"
    with recorded() as log:
        with owner():
            enrolled.service.record(request_for(enrolled.corpus, "final-record",
                expected=current.review_revision), now=1950)
    retires = matching(log, "UPDATE fact_reviews SET active=0")
    assert len(retires) == 1 and retires[0][1] == 1, retires
    assert "active=1" in retires[0][0]
    assert enrolled.read_codes() == ("ok", "ok", "ok")


def test_the_output_store_retire_is_narrowed_too(corpus, projection_runtime):
    """The projection twin: its own statement, in its own module, with its own test."""
    runtime, config, path = projection_runtime
    with owner():
        evidence_service = runtime.evidence_reviews(require_existing=False)
        evidence_service.record(request_for(corpus, "evidence-for-narrowing"), now=1200)
        service = runtime.projection_reviews(require_existing=False)

        def record(review_id, *, expected):
            preview = service.preview(EvidenceLookup(fact_id=corpus[2]), now=1200)
            return service.record(RecordProjectionReview(review_id=review_id,
                expected_candidate=preview.candidate, expected_candidate_hash=preview.candidate_hash,
                expected_current_review_revision=expected, classification={"domains": ["reading"],
                    "sensitivity": "personal", "subject": "self", "assertion": "explicit_atomic_preference"}),
                now=1200)

        first = record("output-review-1", expected=None)
        service.revoke(RevokeProjectionReview(fact_id=corpus[2], review_id=first.review_id,
            expected_review_revision=first.review_revision), now=1201)
        store = Path(config["projection_review_store_path"])
        with sqlite3.connect(store) as conn:
            assert conn.execute("SELECT count(*) FROM fact_reviews WHERE fact_id=? AND active=0",
                                (corpus[2],)).fetchone()[0] == 1
        with recorded() as log:
            record("output-review-2", expected=None)
    retires = matching(log, "UPDATE fact_reviews SET active=0")
    # Nothing is active for the fact, so the narrowed statement matches no row at all;
    # the un-narrowed one would rewrite the retired row.
    assert len(retires) == 1 and retires[0][1] == 0, retires
    assert "active=1" in retires[0][0]


def test_an_owner_read_does_not_write_the_clock_high_water_it_already_has(enrolled):
    """A read that writes nothing is what makes the `total_changes` cross-check live on reads.

    `total_changes` is connection-wide, so any row write in the transaction -- including an
    unconditional high-water UPDATE -- switches the cross-check off. Writing the high-water
    only when it moves is also one less write per owner read on a store nothing caps.
    """
    seed(enrolled, filler=0)
    with recorded() as log:
        with owner():
            enrolled.service.read(enrolled.lookup)
    assert matching(log, "UPDATE review_identity") == []
    assert matching(log, "SELECT highest_generation FROM review_identity"), "it still has to be checked"
