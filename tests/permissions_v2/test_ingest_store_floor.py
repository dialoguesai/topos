"""The ingest ledger past the 1 MiB digest cap, and against a row its own enumeration cannot see.

The authority digest is defined exactly as before -- `digest` of every row of the
four durable tables, each ordered by its first column -- but it is streamed, so the
lane is no longer stopped at roughly 5,500 linked messages. These tests pin both
halves of that claim: the ledger now grows, and the value did not move, so no
enrolled store's marker is re-pinned.

`IngestProvenanceService._authority_digest` enumerates each durable table with
`SELECT * FROM <table> ORDER BY 1`. SQLite plans all four of those as
`SCAN <table> USING INDEX sqlite_autoindex_<table>_1`: the digest walks each
table's primary-key autoindex and fetches rows through it, never the table
b-tree. A row written into the b-tree but not into that autoindex is therefore
digested by nobody, and the external marker still matches byte for byte.

`ingest_provenance_enrollments.dataset_id` and `ingest_provenance_jobs.enrollment_id`
are declared UNIQUE, so each has a second autoindex, and `enroll` and `enqueue`
look rows up through exactly those. That is what makes the hidden row reachable
rather than merely present: a serving call finds it, and answers about another
dataset's -- or another lane's -- enrollment.

These tests plant the row the way an attacker with write access to the file
would, assert that the marker is still satisfied by the digest's own
enumeration, and require the store to refuse. `ingest_provenance_records` and
`ingest_provenance_commands` have no non-primary-key lookup today and so no
reachable row; they are here because the cross-check belongs at the digest,
where it does not depend on which index some later query happens to pick.
"""
import json
import sqlite3

import pytest

from topos.permissions_v2.canonical import MAX_BYTES, PolicyError, Rows, canonical_bytes, digest, digest_stream
from topos.permissions_v2.ingest_protocol import (CHATGPT_OWNER_ATTESTATION, CHATGPT_READER_CONTRACT,
    CHATGPT_SOURCE_ID)
from topos.permissions_v2.ingest_provenance import _SCHEMA, OWNER_ATTESTATION, IngestProvenanceService

from tests.permissions_v2.test_ingest_provenance import (claimed, enroll,  # noqa: F401
    ingest_fixture, owner, result)


DIGESTED = [table for table in _SCHEMA if table != "ingest_provenance_state"]
# The exact declared spelling with the PRIMARY KEY removed, taken from the schema the
# store installs, so a schema change breaks these loudly instead of silently planting
# nothing. Only the key is dropped: every UNIQUE constraint stays, which is what keeps
# the planted row reachable through the second autoindex.
PKLESS = {table: _SCHEMA[table].replace(" PRIMARY KEY", "", 1) for table in DIGESTED}


# --- detection: the index the digest itself enumerates with ------------------------------------------


def _rewrite_schema(path, table, sql, indexes):
    """Replace `table`'s declaration and its index rows in sqlite_master, in one commit."""
    conn = sqlite3.connect(path)
    try:
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET sql=? WHERE name=?", (sql, table))
        conn.execute("DELETE FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,))
        for name, rootpage in indexes:
            conn.execute("INSERT INTO sqlite_master VALUES('index',?,?,?,NULL)", (name, table, rootpage))
        # sqlite_master edits do not move the cookie on their own, and every other
        # connection would keep serving its cached parse of the old declaration.
        conn.execute(f"PRAGMA schema_version={version + 1}")
        conn.commit()
    finally:
        conn.close()


def plant_hidden_row(path, table, row):
    """Write one row into `table`'s b-tree, never into its primary-key autoindex.

    The declaration is swapped for its PRIMARY KEY-less spelling for the length of one
    insert and then restored byte for byte, so the store's schema pin -- which reads
    `sqlite_master` for `name GLOB 'ingest_provenance_*'`, a pattern no
    `sqlite_autoindex_*` name matches -- sees nothing move in either direction.

    SQLite names a table's constraint indexes by position as it parses the
    declaration, so while the key is absent the surviving UNIQUE constraint is the
    one that expects `sqlite_autoindex_<table>_1`. Its root page is republished under
    that name for the window, which is why the planted row still reaches the UNIQUE
    index -- exactly the state a real b-tree/index divergence leaves behind -- and is
    then restored to `_2`. `PRAGMA integrity_check` reports the key autoindex afterwards;
    no request path runs it.
    """
    conn = sqlite3.connect(path)
    try:
        declared = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        indexes = [(name, rootpage) for name, rootpage in conn.execute(
            "SELECT name,rootpage FROM sqlite_master WHERE type='index' AND tbl_name=? ORDER BY name", (table,))]
    finally:
        conn.close()
    assert indexes[0][0] == f"sqlite_autoindex_{table}_1"
    _rewrite_schema(path, table, PKLESS[table],
                    [(f"sqlite_autoindex_{table}_1", rootpage) for _, rootpage in indexes[1:]])
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"INSERT INTO {table} VALUES({','.join('?' * len(row))})", row)
        conn.commit()
    finally:
        conn.close()
    _rewrite_schema(path, table, declared, indexes)
    # On its own connection, and thrown away with it: `PRAGMA integrity_check` attaches
    # the temp database, and `_connection` refuses a connection carrying one.
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0] == declared
        # The report's wording and order are SQLite's: 3.45 (the CI runner's) lists the
        # missing row before the wrong entry count, 3.46 and later the other way round.
        # What the plant must have done is the same on every version: the key autoindex
        # no longer agrees with the table.
        report = [line[0] for line in conn.execute("PRAGMA integrity_check")]
        assert report != ["ok"] and any(f"sqlite_autoindex_{table}_1" in line for line in report), report
    finally:
        conn.close()


def assert_marker_still_matches(service, conn, hidden):
    """The digest's own enumeration still produces the value the marker pins."""
    enumerated = {table: [list(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                  for table in DIGESTED}
    assert digest(enumerated) == json.loads(service.marker.read_text())["authority_digest"]
    assert len(enumerated[hidden]) + 1 == conn.execute(f"SELECT count(*) FROM {hidden} NOT INDEXED").fetchone()[0]


def chatgpt_lane(service, conn):
    """A second, finished enrollment in the other lane, whose job the iMessage lane must not see."""
    export = service.root / "export.json"
    export.write_bytes(b"[]")
    export.chmod(0o400)
    with owner():
        described = service.describe_snapshot(conn, snapshot_id="export", reader_contract=CHATGPT_READER_CONTRACT)
        enrollment = service.enroll(conn, snapshot_id="export", dataset_id="chatgpt-dataset",
                                    snapshot_sha256=described["snapshot_sha256"],
                                    owner_attestation=CHATGPT_OWNER_ATTESTATION,
                                    reader_contract=CHATGPT_READER_CONTRACT)
        job = service.enqueue(conn, enrollment_id=enrollment["enrollment_id"], source_id=CHATGPT_SOURCE_ID)
    context = service.claim(conn, job["job_id"], CHATGPT_SOURCE_ID)
    with context.batch(conn):
        service.finish(conn, context, result())
    return enrollment, job


def test_enqueue_refuses_a_jobs_row_hidden_from_the_key_autoindex(ingest_fixture):
    """`enqueue` reaches the planted row through `enrollment_id`, and answers with another lane's job."""
    service, conn, _ = ingest_fixture
    path = service.resolver.path
    imessage = enroll(service, conn)
    other, job = chatgpt_lane(service, conn)
    assert (other["source_id"], other["enrollment_id"] == imessage["enrollment_id"]) == (CHATGPT_SOURCE_ID, False)
    conn.close()
    # enrollment_id is UNIQUE, so this needs an enrollment with no job of its own; job_id
    # collides with the other lane's job, which only the absent key index would have caught.
    plant_hidden_row(path, "ingest_provenance_jobs",
                     (job["job_id"], imessage["enrollment_id"], 1, "queued", None, None, None))
    conn = sqlite3.connect(path)
    try:
        assert_marker_still_matches(service, conn, "ingest_provenance_jobs")
        assert conn.execute("SELECT job_id FROM ingest_provenance_jobs WHERE enrollment_id=?",
                            (imessage["enrollment_id"],)).fetchone()[0] == job["job_id"]
        with owner(), pytest.raises(PolicyError, match="^ingest_ledger_binding$"):
            # Unrefused this returns the ChatGPT lane's job id, enrollment id, status and
            # result to an iMessage-lane caller, and suppresses this enrollment's own job.
            service.enqueue(conn, enrollment_id=imessage["enrollment_id"])
        with owner(), pytest.raises(PolicyError, match="^ingest_ledger_binding$"):
            service.status(conn, job_id=job["job_id"], source_id=CHATGPT_SOURCE_ID)
        # Nothing was written under either refusal: still the real job and the planted one,
        # and no job of this enrollment's own. The store declined to vouch, not to write.
        assert conn.execute("SELECT count(*) FROM ingest_provenance_jobs NOT INDEXED").fetchone()[0] == 2
    finally:
        conn.close()


def test_enroll_refuses_an_enrollments_row_hidden_from_the_key_autoindex(ingest_fixture):
    """`enroll` reaches the planted row through `dataset_id`, and answers about another dataset."""
    service, conn, _ = ingest_fixture
    path = service.resolver.path
    imessage = enroll(service, conn)
    conn.close()
    plant_hidden_row(path, "ingest_provenance_enrollments",
                     (imessage["enrollment_id"], "{}", "other-dataset", 1, "active", 0, OWNER_ATTESTATION, 0, "uds"))
    conn = sqlite3.connect(path)
    try:
        assert_marker_still_matches(service, conn, "ingest_provenance_enrollments")
        assert conn.execute("SELECT enrollment_id FROM ingest_provenance_enrollments WHERE dataset_id=?",
                            ("other-dataset",)).fetchone()[0] == imessage["enrollment_id"]
        with owner(), pytest.raises(PolicyError, match="^ingest_ledger_binding$"):
            # Unrefused this reports `other-dataset` as already enrolled and hands back the
            # metadata of the enrollment bound to `native-dataset`, having created nothing.
            described = service.describe_snapshot(conn, snapshot_id="canary")
            service.enroll(conn, snapshot_id="canary", dataset_id="other-dataset",
                           snapshot_sha256=described["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION)
    finally:
        conn.close()


@pytest.mark.parametrize("table,row", [
    ("ingest_provenance_records", ("hidden-message", "hidden-enrollment", 1, "hidden-job", "0" * 64)),
    ("ingest_provenance_commands", ("hidden-command", "a" * 64)),
])
def test_a_hidden_row_is_refused_in_a_table_no_serving_read_can_reach(ingest_fixture, table, row):
    """No lookup reaches these rows today. The digest refuses them anyway, because it is the digest.

    Both tables have only the primary-key autoindex, so the planted row is invisible to
    every read in the module as well as to the digest. The cross-check is what makes "every
    row is digested" a checked claim rather than a property of the query plans that exist
    on the day it is written.
    """
    service, conn, _ = ingest_fixture
    path = service.resolver.path
    enrollment = enroll(service, conn)
    conn.close()
    plant_hidden_row(path, table, row)
    conn = sqlite3.connect(path)
    try:
        assert_marker_still_matches(service, conn, table)
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        with owner(), pytest.raises(PolicyError, match="^ingest_ledger_binding$"):
            service.revoke(conn, enrollment_id=enrollment["enrollment_id"])
    finally:
        conn.close()


# --- capacity ----------------------------------------------------------------------------------------
#
# Built as one canonical value, this ledger's shape reached MAX_BYTES at 5,568 record links
# and every door on the lane raised `json_size` from there on. OVER_CAP is comfortably past
# that, and `test_the_record_link_row_shape_stays_in_the_measured_band` is what keeps the
# number meaning something if the link row ever changes shape.

OVER_CAP = 6000
BATCH = 500
EMPTY_DIGEST = "e0c6930a263008a063fe5ae4e713cddf19db134a79c6ab24ccddd06efeac4379"
KNOWN_ANSWER = "e419125e39e584d7746b27ae4607758a29aa64d8e7e91557fcd31bab106b66ab"


def link_rows(conn, count, *, start=0):
    """`count` iMessage rows a link can name, inserted as the native sync would write them."""
    rows = [(f"imessage:{100000 + index}", f"conv-{index // 40}", "native-dataset", "imessage",
             str(200000 + index), "owner-1", "sender-1", "contact", 0, "2026-09-17T00:00:00Z",
             f"message body {index}", "{}", "user") for index in range(start, start + count)]
    conn.executemany("INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return [row[0] for row in rows]


def grow(service, conn, context, message_ids, *, batch=BATCH):
    """Link `message_ids` through the store's own batches, `batch` per transaction.

    This is the real ingest write: every batch is one `_transaction`, so each pays an entry
    digest, an exit digest and two marker publications over a ledger that is already as large
    as it will get. Nothing here reaches into the tables behind the store.
    """
    for index in range(0, len(message_ids), batch):
        with context.batch(conn):
            for message_id in message_ids[index:index + batch]:
                context.record_insert(conn, message_id)


def built_digest(conn):
    """The digest exactly as it was built before it was streamed, cap and all."""
    return digest({table: [list(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                   for table in DIGESTED})


def streamed_digest(conn):
    """The same value recomputed outside the store, lazily, and normalized as the store does."""
    return digest_stream({table: Rows(list(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1"))
                          for table in DIGESTED})


def test_the_record_link_row_shape_stays_in_the_measured_band(ingest_fixture):
    """A link row must stay a real fraction of the old cap, or the capacity claim stops being one."""
    service, conn, _ = ingest_fixture
    context = claimed(ingest_fixture)
    grow(service, conn, context, link_rows(conn, 40))
    sizes = sorted(len(canonical_bytes([list(row)]))
                   for row in conn.execute("SELECT * FROM ingest_provenance_records"))
    assert 150 <= sizes[0] and sizes[-1] <= 260
    assert 4000 < MAX_BYTES // sizes[len(sizes) // 2] < 8000, "the old cap must still land near 5,500 links"


def test_one_ingest_ledger_holds_far_more_links_than_the_old_cap(ingest_fixture):
    """6,000 linked messages in one ledger, with every owner door still working on top of it."""
    service, conn, _ = ingest_fixture
    context = claimed(ingest_fixture)
    grow(service, conn, context, link_rows(conn, OVER_CAP))
    with context.batch(conn):
        service.finish(conn, context, result())

    assert conn.execute("SELECT count(*) FROM ingest_provenance_records").fetchone()[0] == OVER_CAP
    with pytest.raises(PolicyError, match="^json_size$"):
        built_digest(conn)  # the old whole-value digest cannot even be computed at this size
    marker = json.loads(service.marker.read_text())
    assert marker["state"] == "active" and marker["authority_digest"] == streamed_digest(conn)

    with owner():
        # Every door `_check` guards, on a ledger past the point where they all used to raise
        # `json_size`: enqueue, status and revoke here, and enroll, enqueue and claim inside
        # `chatgpt_lane`, which runs a second lane's job end to end against the same ledger.
        assert service.status(conn, job_id=context.job_id)["status"] == "done"
        assert service.enqueue(conn, enrollment_id=context.enrollment_id)["job_id"] == context.job_id
    service.validate_record_origin(conn, message_id="imessage:100000",
        origin={"version": "owner-attested-snapshot/v1", "enrollment_id": context.enrollment_id,
                "job_id": context.job_id})
    other, job = chatgpt_lane(service, conn)
    with owner():
        assert service.status(conn, job_id=job["job_id"], source_id=CHATGPT_SOURCE_ID)["status"] == "done"
        assert service.revoke(conn, enrollment_id=other["enrollment_id"],
                              source_id=CHATGPT_SOURCE_ID)["state"] == "revoked"
        assert service.revoke(conn, enrollment_id=context.enrollment_id)["state"] == "revoked"
    # Revocation withholds the already-linked row -- `_enrollment(active=True)` refuses a state
    # that is no longer active -- which is the floor still deciding, not failing to be computed.
    with pytest.raises(PolicyError, match="^ingest_enrollment_stale$"):
        service.validate_record_origin(conn, message_id="imessage:100000",
            origin={"version": "owner-attested-snapshot/v1", "enrollment_id": context.enrollment_id,
                    "job_id": context.job_id})


def test_the_streamed_digest_is_the_value_the_built_digest_produced(ingest_fixture):
    """Byte-identity at every size the built digest could still answer at, and past where it could not.

    This is the whole compatibility claim: an enrolled store's `ingest-snapshots.enrollment.json`
    pins a digest the built encoder wrote, and a value that moved would close every such store
    on its next open instead of opening it. Below the cap the two encoders are compared directly
    -- including at 0 links, where the enrollment and its job are the only rows -- and above it
    only the streamed one can answer, which is the point.
    """
    service, conn, _ = ingest_fixture
    context = claimed(ingest_fixture)
    message_ids = link_rows(conn, OVER_CAP)
    linked = 0
    for count in (0, 1, 40, 386, 2000):
        grow(service, conn, context, message_ids[linked:count])
        linked = count
        assert conn.execute("SELECT count(*) FROM ingest_provenance_records").fetchone()[0] == count
        assert built_digest(conn) == streamed_digest(conn) == service._authority_digest(conn)
    grow(service, conn, context, message_ids[linked:])
    with pytest.raises(PolicyError, match="^json_size$"):
        built_digest(conn)
    assert streamed_digest(conn) == service._authority_digest(conn)


def test_the_authority_digest_is_the_pinned_value_over_first_column_order(tmp_path):
    """A known answer over a fixed ledger, inserted so that rowid order differs from key order.

    This is what a changed column set, a changed framing or an `ORDER BY rowid` would break.
    All four tables are rowid tables and VACUUM renumbers rowids, so ordering by rowid would
    turn routine maintenance into a rollback refusal that closes the lane.
    """
    path = tmp_path / "known.db"
    with sqlite3.connect(path) as conn:
        for table in DIGESTED:
            conn.execute(_SCHEMA[table])
        assert IngestProvenanceService._authority_digest(conn) == EMPTY_DIGEST == digest(
            {table: [] for table in DIGESTED})
        conn.executemany("INSERT INTO ingest_provenance_commands VALUES(?,?)",
                         [("command-b", "b" * 64), ("command-a", "a" * 64)])
        conn.execute("INSERT INTO ingest_provenance_enrollments VALUES(?,?,?,?,?,?,?,?,?)",
                     ("enrollment-1", "{}", "dataset-1", 1, "active", 3, OWNER_ATTESTATION, 1758000000, "uds"))
        conn.execute("INSERT INTO ingest_provenance_jobs VALUES(?,?,?,?,?,?,?)",
                     ("job-1", "enrollment-1", 1, "done", None, None, None))
        conn.executemany("INSERT INTO ingest_provenance_records VALUES(?,?,?,?,?)",
                         [("imessage:2", "enrollment-1", 1, "job-1", "2" * 64),
                          ("imessage:1", "enrollment-1", 1, "job-1", "1" * 64)])
    with sqlite3.connect(path) as conn:
        assert IngestProvenanceService._authority_digest(conn) == KNOWN_ANSWER == built_digest(conn)
        conn.execute("VACUUM")
    with sqlite3.connect(path) as conn:
        assert IngestProvenanceService._authority_digest(conn) == KNOWN_ANSWER


def test_the_digest_is_the_same_value_whatever_row_factory_the_node_set(ingest_fixture):
    """The ledger digests the node's own connection, and the node sets `row_factory`.

    `topos/core/state.py` puts `sqlite3.Row` on the canonical connection and re-asserts it,
    so the rows reaching the digest are not tuples. The built digest normalized them with
    `list(row)` -- a `sqlite3.Row` yields its values -- and the streamed one has to produce
    that same byte string, because an enrolled store's marker was pinned through whichever
    factory happened to be set when it was written. Streaming a `sqlite3.Row` straight into
    `Rows` instead refuses it as `json_type`, which closes the lane rather than moving it.
    """
    service, conn, _ = ingest_fixture
    context = claimed(ingest_fixture)
    grow(service, conn, context, link_rows(conn, 40))
    plain = service._authority_digest(conn)
    assert plain == built_digest(conn)
    for factory in (sqlite3.Row, lambda cursor, row: row, None):
        conn.row_factory = factory
        assert service._authority_digest(conn) == plain
        # and the store still opens on top of it, which is the digest the marker pins
        with owner():
            assert service.status(conn, job_id=context.job_id)["status"] == "running"
    conn.row_factory = None
