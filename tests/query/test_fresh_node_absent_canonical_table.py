"""A declared canonical table that this node has never created is an EMPTY STORE.

A standard init runs the migrations and nothing else: 77 tables, and
``conversation_messages`` is not among them — the conversations table manager
creates it the first time a message actually lands. So on a first-run install the
table named by ``relationship_context:read``'s manifest does not exist yet.

Retrieval read it anyway. ``_list_canonical_rows`` walked ``manifest.canonical_tables``
straight into ``canonical.list()``, which built ``SELECT COUNT(*) FROM (…)`` over a
table SQLite has never heard of, and the turn died at
``storage/adapters/sqlite/stores.py:449`` with ``sqlite3.OperationalError: no such
table: conversation_messages``. The owner's first question on a new node returned a
500 — on the OWNER path, where nothing is filtered and no grant is involved.

The absence is not an error. It is the same fact the empty-cause taxonomy already has
words for, so the tests here assert the words: ``store_empty`` as the cause and
``connected_never_delivered`` as the sub-cause, on a ledger the caller receives, rather
than merely asserting that nothing blew up. An empty answer with no cause is the
false-absence bug this taxonomy exists to end, and it would satisfy a
"does not raise" test perfectly.

The last two classes are the counterweights. A probe that returns "absent" for every
table would satisfy every assertion above while silently disabling retrieval, and a
``try/except`` around the read would satisfy them while swallowing a real fault — so
one class asserts rows still arrive when the table is there, and one asserts a genuine
storage failure still reaches the caller.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

import pytest

from topos.query import narrowing as N
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

SCOPE = "relationship_context:read"
TABLE = "conversation_messages"
#: A source this scope actually authorizes — otherwise the control below proves nothing
#: about the probe, only that the scope filtered an unauthorized source.
SOURCE_ID = "demo_messenger_file"
INSTALLED = [SOURCE_ID]
QUERY = "what has been going on with bram lately"

#: The two tiers the sweep probed. The owner path is the one that 500'd, and it is the
#: one that cannot be explained away as a grant edge case: it applies no filter at all.
TIERS = ("owner_raw", "default_disclosure")


def _table_names(conn: sqlite3.Connection) -> List[str]:
    return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))


@pytest.fixture()
def fresh_conn(tmp_path):
    """A standard init and nothing else — no ingest, so no canonical message table."""
    c = sqlite3.connect(str(tmp_path / "fresh.db"))
    apply_all_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def ingested_conn(tmp_path):
    """The same node after one message landed — the table now exists and holds a row."""
    c = sqlite3.connect(str(tmp_path / "ingested.db"))
    apply_all_migrations(c)
    ConversationsTablesManager(c).upsert_message_batch(
        [
            {
                "message_id": "msg-1",
                "thread_id": "thread-1",
                "content": "bram is picking up the migration on friday",
                "ts": "2026-08-01T12:00:00Z",
                "sender_type": "contact",
            }
        ],
        dataset_id="user:default:device",
        source_id=SOURCE_ID,
        sync_batch_id="b1",
    )
    c.commit()
    yield c
    c.close()


def _retrieve(conn: sqlite3.Connection, *, disclosure_tier: str, ledger=None, **kwargs):
    adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn))
    return adapter.retrieve(
        RetrievalRequest(
            manifest=resolve_scope_manifest(SCOPE),
            access_mode="summary",
            query_text=QUERY,
            installed_source_ids=INSTALLED,
            disclosure_tier=disclosure_tier,
            ledger=ledger,
            **kwargs,
        )
    )


def _entries(ledger: NarrowingLedger) -> List[Dict[str, Any]]:
    return list(ledger.as_public().get("ledger") or [])


# --------------------------------------------------------------------------- premise


class TestThePremise:
    """If either half of this stops being true the tests below prove nothing."""

    def test_a_standard_init_does_not_create_the_table(self, fresh_conn) -> None:
        names = _table_names(fresh_conn)
        assert names, "migrations created no tables at all"
        assert TABLE not in names, (
            "premise gone: a standard init now creates the canonical message table, "
            "so this file no longer exercises a first-run install"
        )

    def test_the_scope_declares_the_table_anyway(self) -> None:
        assert TABLE in resolve_scope_manifest(SCOPE).canonical_tables, (
            "premise gone: this scope no longer declares the table whose absence is the bug"
        )


# --------------------------------------------------------------------------- the fix


class TestAFirstRunInstallGetsAnHonestEmptyAnswer:
    """Counterfactual, measured before the fix: BOTH tiers raised
    ``sqlite3.OperationalError: no such table: conversation_messages`` out of
    ``stores.py:449``, through ``retrieve`` → ``_build_summary_items`` →
    ``_load_canonical_summary_items`` → ``_list_canonical_rows`` → ``canonical.list``.
    """

    @pytest.mark.parametrize("tier", TIERS)
    def test_it_does_not_raise(self, fresh_conn, tier) -> None:
        bundle = _retrieve(fresh_conn, disclosure_tier=tier)
        assert bundle is not None
        assert not (bundle.context_packet.get("summaries") or []), (
            "a node that has never ingested returned summary items"
        )

    @pytest.mark.parametrize("tier", TIERS)
    def test_the_empty_answer_says_the_store_is_empty(self, fresh_conn, tier) -> None:
        led = NarrowingLedger()
        bundle = _retrieve(fresh_conn, disclosure_tier=tier, ledger=led)
        assert led.empty_cause == N.CAUSE_STORE_EMPTY, (
            f"an empty answer with cause {led.empty_cause!r} — 'nothing has ever been "
            "stored here' is not 'nothing matched your question'"
        )
        assert bundle.retrieval_metadata.get("empty_cause") == N.CAUSE_STORE_EMPTY

    @pytest.mark.parametrize("tier", TIERS)
    def test_the_ledger_names_the_sub_cause_from_the_closed_set(self, fresh_conn, tier) -> None:
        led = NarrowingLedger()
        _retrieve(fresh_conn, disclosure_tier=tier, ledger=led)
        emptied = [
            e
            for e in _entries(led)
            if e.get("stage") == N.STAGE_RETRIEVAL and e.get("action") == "emptied"
        ]
        assert emptied, "the store's absence was not recorded on the ledger at all"
        reasons = {e.get("reason") for e in emptied}
        assert "connected_never_delivered" in reasons, (
            f"expected the never-delivered sub-cause; ledger carried {sorted(reasons)}"
        )
        assert N.UNRECOGNIZED not in reasons, "a sub-cause outside the closed set was emitted"

    def test_the_owner_is_told_which_store_it_was_without_it_leaving_the_node(
        self, fresh_conn
    ) -> None:
        """``detail`` is the on-node serializer; ``as_public`` is the one that travels.
        The table name is a diagnostic for the owner's own logs, so it belongs in the
        first and must not be smuggled into the second as a reason slug."""
        led = NarrowingLedger()
        _retrieve(fresh_conn, disclosure_tier="owner_raw", ledger=led)
        local = [
            e
            for e in (led.as_local().get("ledger") or [])
            if (e.get("detail") or {}).get("table") == TABLE
        ]
        assert local, "the on-node ledger never named the absent store"
        assert "detail" not in str(led.as_public()), "detail reached the public serializer"


# --------------------------------------------------------------------------- controls


class TestTheProbeBoundsTheLaneRatherThanDisablingIt:
    """A probe that answered "absent" for every table would pass every assertion above."""

    def test_a_table_that_exists_is_still_read(self, ingested_conn) -> None:
        led = NarrowingLedger()
        bundle = _retrieve(ingested_conn, disclosure_tier="owner_raw", ledger=led)
        assert bundle.context_packet.get("summaries"), (
            "the existence probe emptied a store that holds a row"
        )
        assert led.empty_cause is None

    @pytest.mark.parametrize("tier", TIERS)
    def test_a_present_table_is_never_called_never_delivered(self, ingested_conn, tier) -> None:
        """Asserted on both tiers, and separately from the row assertion above, because
        the grantee tier legitimately withholds the row's content — so "no summaries at
        `default_disclosure`" says nothing about the probe. What the probe must never do
        at either tier is claim a store that exists has never received a delivery."""
        led = NarrowingLedger()
        _retrieve(ingested_conn, disclosure_tier=tier, ledger=led)
        reasons = {e.get("reason") for e in _entries(led)}
        assert "connected_never_delivered" not in reasons, (
            "the probe reported an existing store as never delivered"
        )


class TestAGenuineStorageFailureStillFails:
    """The absence is routed deliberately, not swallowed: the read is NOT wrapped in a
    bare ``except``, so a fault that is not "this table was never created" still reaches
    the caller instead of being reported to the owner as an empty store."""

    def test_a_broken_read_is_not_reported_as_an_empty_store(self, ingested_conn) -> None:
        adapters = AdapterFactory.create("local_database", conn=ingested_conn)

        def _boom(*args: Any, **kwargs: Any):
            raise sqlite3.OperationalError("disk I/O error")

        adapters.canonical.list = _boom  # type: ignore[method-assign]
        led = NarrowingLedger()
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            DefaultSignalRetrievalAdapter(adapters).retrieve(
                RetrievalRequest(
                    manifest=resolve_scope_manifest(SCOPE),
                    access_mode="summary",
                    query_text=QUERY,
                    installed_source_ids=INSTALLED,
                    disclosure_tier="owner_raw",
                    ledger=led,
                )
            )
