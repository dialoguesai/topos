"""Five holes in the retrieval boundary, each pinned by the property it broke.

The theme is not "a leak happened". It is that four planes which the rest of the
system trusts to be TOTAL — the manifest's table list, a scope's declared
``must_not_retrieve``, the disclosure tier, the enforced-exclusion plane — each
had a lane that ran outside them, and one guard that was already correct had a
half nothing tested. A partial boundary reads exactly like a whole one from the
call site, which is why every test here asserts on the packet or the ledger a
caller actually receives rather than on the helper that was supposed to filter it.

Each class states the counterfactual it was written from. Where the counterfactual
was produced by hand-severing a wire rather than by seeding data, the docstring
says so, because a test whose red has never been seen is not evidence.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

SOURCE_ID = "imessage"
INSTALLED = [SOURCE_ID]
THREAD_ID = "msg-thread-1"
ENTITY_ID = "ent-anthropic"
ENTITY_NAME = "Anthropic"
QUERY = "what happened with the Anthropic thread"


def _seed(conn: sqlite3.Connection) -> None:
    ConversationsTablesManager(conn).upsert_message_batch(
        [
            {
                "message_id": THREAD_ID,
                "thread_id": "thread-1",
                "content": "shipping the compiler rewrite on friday",
                "ts": "2026-03-13T12:00:00Z",
                "sender_type": "contact",
            }
        ],
        dataset_id="user:default:device",
        source_id=SOURCE_ID,
        sync_batch_id="b1",
    )
    conn.execute(
        """
        INSERT INTO entities
            (entity_id, entity_type, canonical_name, normalized_name, is_self, mention_count)
        VALUES (?, 'org', ?, ?, 0, 4)
        """,
        (ENTITY_ID, ENTITY_NAME, ENTITY_NAME.lower()),
    )
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, event_at)
        VALUES ('m-1', ?, ?, ?, 'conversation_messages', ?, '2026-03-13T12:00:00Z')
        """,
        (ENTITY_ID, THREAD_ID, SOURCE_ID, ENTITY_NAME),
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "plane_gaps.db"))
    apply_all_migrations(c)
    _seed(c)
    yield c
    c.close()


def _retrieve(
    conn: sqlite3.Connection,
    query_text: str = QUERY,
    *,
    scope: str = "messages:read",
    access_mode: str = "summary",
    disclosure_tier: str = "owner_raw",
    ledger=None,
    **request_kwargs,
):
    adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn))
    return adapter.retrieve(
        RetrievalRequest(
            manifest=resolve_scope_manifest(scope),
            access_mode=access_mode,
            query_text=query_text,
            installed_source_ids=INSTALLED,
            disclosure_tier=disclosure_tier,
            ledger=ledger,
            **request_kwargs,
        )
    )


def _summaries(bundle) -> List[Dict[str, Any]]:
    return list(bundle.context_packet.get("summaries") or [])


def _pointers(bundle) -> List[Dict[str, Any]]:
    return [s for s in _summaries(bundle) if s.get("retrieval_source") == "entity_mention"]


# --------------------------------------------------------------------------- A


class TestTheMentionPointerObeysTheManifest:
    """A mention row is a POINTER: it names a canonical table and a record id
    without ever reading the record. That is still a disclosure — it says the
    record exists, which table holds it, and when it happened — and the lane that
    emits it took no manifest at all, so it offered whatever table the entity
    graph happened to know about.

    Counterfactual, measured before the fix: a request on ``availability:read``,
    whose manifest declares ``canonical_tables == []``, came back holding
    ``{'topic': 'Anthropic in conversation_messages', 'record_id': 'msg-thread-1',
    'retrieval_source': 'entity_mention'}``.
    """

    def test_a_grant_with_no_tables_gets_no_pointers(self, conn) -> None:
        bundle = _retrieve(conn, scope="availability:read")
        assert resolve_scope_manifest("availability:read").canonical_tables == [], (
            "premise gone: this scope must authorize no canonical table"
        )
        assert _pointers(bundle) == [], "a pointer named a table the grant does not authorize"
        assert THREAD_ID not in str(bundle.context_packet), (
            "a record id from an unauthorized table reached the packet"
        )

    def test_a_grant_that_does_authorize_the_table_still_gets_them(self, conn) -> None:
        """The control. Held at the SAME tier as the test above, so the only thing
        that differs between the two is the manifest — otherwise the first test
        would pass for a scope that had simply retrieved nothing at all."""
        bundle = _retrieve(conn, scope="messages:read")
        assert "conversation_messages" in resolve_scope_manifest("messages:read").canonical_tables
        assert _pointers(bundle), "the bound disabled the lane instead of bounding it"

    def test_the_pointer_declares_the_keys_the_other_planes_match_on(self, conn) -> None:
        """The pointer used to declare neither ``object_type`` nor ``disclosure``,
        so ``_fact_disclosure_allowed`` had nothing to test and it crossed the tier
        filter untouched; and it carried no ``canonical_table``, so a category
        exclusion could report ``enforced=true, dropped=0`` while the pointer
        stayed in the packet."""
        item = _pointers(_retrieve(conn, scope="messages:read"))[0]
        assert item["canonical_table"] == "conversation_messages"
        assert item["object_type"] == "entity_mention"
        assert item["disclosure"] == "owner_only"

    def test_a_grantee_does_not_receive_the_pointer(self, conn) -> None:
        """What the disclosure keys above buy: the pointer is the entity plane's
        own artefact, so it rides ``entity_dossiers`` — a grant no scope in the
        registry currently holds — instead of falling through to the default."""
        bundle = _retrieve(conn, scope="messages:read", disclosure_tier="default_disclosure")
        assert _pointers(bundle) == []

    def test_an_untabled_mention_is_dropped_rather_than_offered_to_everyone(self, conn) -> None:
        """A mention whose ``canonical_table`` is NULL cannot show it came from an
        authorized table, and a pointer has no second opinion to fall back on the
        way the thread lane does (a record-id intersection against already-disclosed
        rows). So it is dropped."""
        conn.execute(
            """
            INSERT INTO entity_mentions
                (mention_id, entity_id, record_id, source_id, canonical_table,
                 surface_text, event_at)
            VALUES ('m-null', ?, 'rec-untabled', ?, NULL, ?, '2026-03-13T12:00:00Z')
            """,
            (ENTITY_ID, SOURCE_ID, ENTITY_NAME),
        )
        conn.commit()
        assert "rec-untabled" not in str(_retrieve(conn, scope="messages:read").context_packet)


# --------------------------------------------------------------------------- B


class TestSummaryModeAppliesMustNotRetrieve:
    """A scope's declared ``must_not_retrieve`` bound one mode out of three.

    ``raw`` applied it to its rows and ``inference`` to the whole packet; summary
    — the mode most scopes actually answer in — never applied it. ``availability:read``
    is the live case: it declares three restrictions, its ceiling is ``inference``,
    and ``MODE_RANK`` puts ``summary`` BELOW that ceiling, so the reachable mode
    was the unenforced one.

    The forbidden key is injected rather than seeded. No summary lane in this tree
    happens to emit a key named ``content`` today, so seeding would test which
    lanes exist rather than whether the restriction is applied — and the whole
    point of applying it to the packet is to cover the lanes that grow later.
    The injection is at ``_build_summary_items``, a real lane's real return value.
    """

    FORBIDDEN_ITEM = {
        "topic": "leaked",
        "summary_text": "ok",
        "content": "the raw sentence a restricted scope may not retrieve",
    }

    @pytest.fixture()
    def leaky_summary_lane(self, monkeypatch):
        import topos.query.retrieval as R

        monkeypatch.setattr(
            R, "_build_summary_items", lambda **kw: [dict(self.FORBIDDEN_ITEM)]
        )
        return None

    def test_a_declared_restriction_is_applied_in_summary_mode(
        self, conn, leaky_summary_lane
    ) -> None:
        manifest = resolve_scope_manifest("availability:read")
        assert "content" in manifest.must_not_retrieve, "premise gone"
        bundle = _retrieve(conn, scope="availability:read", access_mode="summary")
        assert "content" not in str(bundle.context_packet), (
            "summary mode returned a key the scope declared it must not retrieve"
        )

    def test_the_restriction_leaves_unrestricted_keys_alone(
        self, conn, leaky_summary_lane
    ) -> None:
        """The control: the strip is keyed on the declaration, not a blanket wipe."""
        items = _summaries(_retrieve(conn, scope="availability:read", access_mode="summary"))
        assert any(i.get("topic") == "leaked" for i in items), (
            "the restriction removed the whole item instead of the declared key"
        )

    def test_inference_mode_already_did_this(self, conn, leaky_summary_lane) -> None:
        """The two modes that were already correct, asserted so the test above is
        read as 'summary caught up' rather than 'a new rule was invented'."""
        bundle = _retrieve(conn, scope="availability:read", access_mode="inference")
        assert "content" not in str(bundle.context_packet)


# --------------------------------------------------------------------------- C


class TestEveryCanonicalReadCarriesTheRequestedTier:
    """``_list_canonical_rows`` defaults ``disclosure_tier`` to ``owner_raw``. Nine
    call sites pass the request's tier; one — the legacy employer heuristic over
    ``profile_records`` — took the default, so a grantee's work-context ask read
    that table at the OWNER's tier: past the NSFW row exclusion and past the
    disclosure-column swap the other eight get for free.

    Asserted on the argument rather than on the output because on SQLite
    ``profile_records`` has no disclosure spec and ``_list_native`` never applies
    the Python content policy, so today the wrong tier is inert THERE and live in
    the in-memory store. A test that watched only the output would pass on one
    adapter and fail on the other for reasons that have nothing to do with this
    call site.
    """

    def _tiers_for(self, table: str) -> List[Any]:
        adapters = AdapterFactory.create("memory")
        adapters.canonical.upsert(
            "profile_records",
            {
                "record_id": "pr-1",
                "record_type": "experience",
                "title": "Staff engineer",
                "organization": "Nightshade Studios",
                "description": "worked on the pipeline",
                "content": "worked on the pipeline",
                "source_id": SOURCE_ID,
            },
        )
        seen: List[Any] = []
        original = adapters.canonical.list

        def _spy(t, **kwargs):
            if t == table:
                seen.append(kwargs.get("disclosure_tier"))
            return original(t, **kwargs)

        adapters.canonical.list = _spy  # type: ignore[method-assign]
        DefaultSignalRetrievalAdapter(adapters).retrieve(
            RetrievalRequest(
                manifest=resolve_scope_manifest("work_context:read"),
                access_mode="summary",
                query_text="who was my previous employer",
                installed_source_ids=INSTALLED,
                disclosure_tier="default_disclosure",
            )
        )
        return seen

    def test_the_profile_records_lane_reads_at_the_grantees_tier(self) -> None:
        tiers = self._tiers_for("profile_records")
        assert tiers, "premise gone: the employer lane no longer reads profile_records"
        assert set(tiers) == {"default_disclosure"}, (
            f"a canonical read used the owner's tier for a grantee: {tiers}"
        )


# --------------------------------------------------------------------------- D


class TestTheCohortRollupDoesNotSkipTheExclusionPlane:
    """``cohort_aggregate`` returns its bundle from ``retrieve`` BEFORE the
    exclusion plane at the foot of the method, so "…but nothing from my journal"
    left no trace at all: not enforced, and not reported as un-enforced either.
    A caller could not tell an honoured exclusion from a skipped one.

    It is deliberately NOT routed through the item filter. The packet holds one
    derived count over cohort membership, computed before any row exists; the
    filter would match nothing, report ``enforced=True, dropped=0``, and leave the
    count still counting the excluded members — a claim of enforcement over a
    number that was never filtered, which is the shape ``exclusion.py`` exists to
    prevent. So the plane is told the packet is aggregate-only and says so.
    """

    def _bundle(self, conn, ledger=None):
        return _retrieve(
            conn,
            "how many people do I know, but nothing from my journal",
            ledger=ledger,
            cohort_aggregate=True,
        )

    def test_the_rollup_reports_the_exclusion_as_not_applied(self, conn) -> None:
        block = self._bundle(conn).context_packet.get("exclusion")
        assert block is not None, "the rollup skipped the exclusion plane silently"
        assert block["requested"] is True
        assert block["enforced"] is False, "an unfiltered count claimed to be filtered"
        assert block["not_applied"] >= 1
        assert block["dropped"] == 0

    def test_the_ledger_carries_the_closed_set_reason(self, conn) -> None:
        from topos.query.exclusion import ACTION_NOT_APPLIED, REASON_AGGREGATE

        ledger = NarrowingLedger()
        self._bundle(conn, ledger=ledger)
        entries = [
            e
            for e in ledger.as_public().get("ledger") or []
            if e.get("action") == ACTION_NOT_APPLIED and e.get("reason") == REASON_AGGREGATE
        ]
        assert entries, f"no honest not_applied record: {ledger.as_public()}"

    def test_the_public_ledger_carries_no_fragment_text(self, conn) -> None:
        """The record is new public surface, so it gets the usual check: the
        owner's own words stay in ``detail``, which does not leave the node."""
        ledger = NarrowingLedger()
        self._bundle(conn, ledger=ledger)
        public = ledger.as_public()
        assert "but nothing from" not in str(public)
        assert all("detail" not in e for e in public.get("ledger") or [])
        assert any("journal" in str(e) for e in ledger.as_local().get("ledger") or []), (
            "the local record dropped what went un-applied, so nothing can be audited"
        )

    def test_a_request_with_no_exclusion_is_left_alone(self, conn) -> None:
        """The control: the plane claims nothing when nothing was asked."""
        bundle = _retrieve(conn, "how many people do I know", cohort_aggregate=True)
        assert bundle.context_packet.get("exclusion") is None


# --------------------------------------------------------------------------- E


class TestTheBlackHoleSourceWireHasItsOwnProperty:
    """The black-hole fix has two wires: the source filter inside
    ``_load_entity_thread_items`` (A) and the exit filter at the packet boundary
    (B). Every existing assertion is a LEAK assertion, and wire B satisfies all of
    them on its own — so wire A could be deleted with the whole suite green.

    Wire A is not a redundant copy. It is what keeps the lane from recording, in
    the PUBLIC ledger, that it found rows for the entity before wire B removes
    them. That receipt converts hiding-by-absence into hiding-by-denial, which is
    the one thing D5 forbids, and no leak assertion can see it.

    Measured, by severing wire A alone: the packet stayed clean (``msg-thread-1``
    absent, every leak test green) and the public ledger gained
    ``{stage: retrieval, action: contributed, reason: entity_thread_lane,
    dropped: 0}`` for a black-holed entity.
    """

    @pytest.fixture()
    def blackholed(self, conn):
        from topos.features.lifecycle.blackhole import BlackholeStore

        store = BlackholeStore(conn)
        store.blackhole_entity(entity_ref=ENTITY_ID)
        store.mark_rebuild_complete(ENTITY_ID)
        conn.commit()
        return conn

    @staticmethod
    def _public(conn, ledger) -> str:
        _retrieve(conn, disclosure_tier="default_disclosure", ledger=ledger)
        return str(ledger.as_public())

    def test_the_lane_files_no_public_receipt_for_a_black_holed_entity(
        self, blackholed
    ) -> None:
        """THE PROPERTY. Owned by wire A: the protected entity's records are gone
        before the lane has anything to count, so it contributes nothing and says
        nothing."""
        ledger = NarrowingLedger()
        assert "entity_thread_lane" not in self._public(blackholed, ledger), (
            "the public ledger told a grantee the entity lane found something"
        )

    def test_severing_wire_a_alone_makes_that_property_fail(
        self, blackholed, monkeypatch
    ) -> None:
        """THE SABOTAGE. Wire A is replaced by a pass-through — wire B untouched —
        and the assertion above is expected to go red. Without this, the test above
        is indistinguishable from one that passes because the lane never ran."""
        import topos.query.retrieval as R

        monkeypatch.setattr(
            R,
            "_blackhole_filter_thread_mentions",
            lambda by_table, untabled, **kw: (by_table, untabled),
        )
        ledger = NarrowingLedger()
        public = self._public(blackholed, ledger)
        assert "entity_thread_lane" in public, (
            "wire A is no longer the thing that suppresses the receipt — either the "
            "seam moved or the property above is being satisfied by something else, "
            "and either way the test above has stopped being evidence"
        )

    def test_severing_wire_a_leaves_every_leak_assertion_green(
        self, blackholed, monkeypatch
    ) -> None:
        """Why the receipt needed its own test: with wire A gone the packet is
        STILL clean, because wire B catches the same rows. The leak property is
        blind to this wire; only the ledger property can see it."""
        import topos.query.retrieval as R

        monkeypatch.setattr(
            R,
            "_blackhole_filter_thread_mentions",
            lambda by_table, untabled, **kw: (by_table, untabled),
        )
        blob = str(
            _retrieve(blackholed, disclosure_tier="default_disclosure").context_packet
        )
        assert THREAD_ID not in blob
        assert ENTITY_ID not in blob

    def test_the_owner_receives_the_receipt(self, blackholed) -> None:
        """The control for the property test: it must pass because wire A dropped
        the records, not because the lane is dead or the ledger line was deleted.
        The owner keeps their rows, so the same query on the same store DOES file
        the receipt."""
        ledger = NarrowingLedger()
        assert "entity_thread_lane" in self._public_owner(blackholed, ledger)

    @staticmethod
    def _public_owner(conn, ledger) -> str:
        _retrieve(conn, disclosure_tier="owner_raw", ledger=ledger)
        return str(ledger.as_public())
