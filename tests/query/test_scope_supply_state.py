"""Why a scope holds nothing — three cases behind one identical answer.

`store_empty` already separates "nothing has ever been stored" from "you had a quiet
week". It does not say which of three things went wrong, and the remedies are different:
connect a source, wait for a first sync, or find out what emptied the tables. Only one of
those is the owner's to act on, and today's answer sends them to the wrong one.

Doc gap G4 / query Q6 ("Why is my calendar empty — is that real?"). Measured on the live
node: `calendar_events` and `financial_transactions` both hold 0 rows, and their empty
result is worded identically to a genuinely quiet week.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.manifest import ScopeResolutionManifest
from topos.query.retrieval import (
    SUPPLY_DELIVERED_THEN_EMPTIED,
    SUPPLY_NEVER_DELIVERED,
    SUPPLY_NO_SOURCE,
    _scope_supply_state,
)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE scope_source_generation ("
        " scope_id TEXT, source_id TEXT, generation INTEGER, updated_at TEXT)"
    )
    yield c
    c.close()


def manifest(scope_id="schedule:read", sources=()):
    return ScopeResolutionManifest(
        scope_id=scope_id,
        primary_dimensions=["time"],
        default_source_ids=list(sources),
    )


@pytest.fixture(autouse=True)
def _feeds(monkeypatch, request):
    """Which sources feed a scope comes from the registry, not the manifest.

    Pinned per-test so these assertions describe the probe rather than whatever this
    developer machine happens to have installed. Using `default_source_ids` here was a
    real bug: it reported "no source connected" for a scope whose calendar connector was
    installed, which sends the owner to add a connector they already have.
    """
    feeds = getattr(request, "param", None)
    import topos.sources.registry as reg

    monkeypatch.setattr(
        reg, "get_sources_by_scope",
        lambda scope_id: list(feeds) if feeds is not None else ["gcal_events"],
    )


class TestTheThreeCases:
    def test_data_landed_once_means_something_emptied_it(self, conn) -> None:
        """Never tell an owner to reconnect a feed that is working."""
        conn.execute(
            "INSERT INTO scope_source_generation VALUES ('schedule:read','gcal_events',12,'now')"
        )
        state = _scope_supply_state(conn, manifest(sources=["gcal_events"]), ["gcal_events"])
        assert state == SUPPLY_DELIVERED_THEN_EMPTIED

    def test_a_feed_is_installed_but_has_never_landed(self, conn) -> None:
        state = _scope_supply_state(conn, manifest(sources=["gcal_events"]), ["gcal_events"])
        assert state == SUPPLY_NEVER_DELIVERED

    def test_nothing_installed_feeds_this_scope(self, conn) -> None:
        """The live calendar case: connect Google Calendar, don't doubt the week."""
        state = _scope_supply_state(conn, manifest(sources=["gcal_events"]), ["grow_journal"])
        assert state == SUPPLY_NO_SOURCE

    def test_a_scope_that_declares_no_feeds_at_all(self, conn) -> None:
        assert _scope_supply_state(conn, manifest(sources=[]), ["grow_journal"]) == SUPPLY_NO_SOURCE

    def test_generation_zero_is_not_a_delivery(self, conn) -> None:
        """A registered pairing that never produced a generation has not delivered."""
        conn.execute(
            "INSERT INTO scope_source_generation VALUES ('schedule:read','gcal_events',0,'now')"
        )
        state = _scope_supply_state(conn, manifest(sources=["gcal_events"]), ["gcal_events"])
        assert state == SUPPLY_NEVER_DELIVERED


class TestUnknownStaysUnknown:
    """An unknown must not be dressed up as a diagnosis — a wrong remedy is worse than
    none, because the owner acts on it."""

    def test_no_connection(self) -> None:
        assert _scope_supply_state(None, manifest(sources=["gcal_events"]), ["gcal_events"]) is None

    def test_no_scope_id(self, conn) -> None:
        assert _scope_supply_state(conn, manifest(scope_id="", sources=["x"]), ["x"]) is None

    def test_missing_table_on_an_older_database(self) -> None:
        bare = sqlite3.connect(":memory:")
        try:
            assert _scope_supply_state(bare, manifest(sources=["gcal_events"]), ["gcal"]) is None
        finally:
            bare.close()

    def test_declared_feeds_but_installed_set_unknown(self, conn) -> None:
        """Claiming "nothing connected" when we simply were not told what is installed
        would be a guess with a confident face."""
        assert _scope_supply_state(conn, manifest(sources=["gcal_events"]), None) is None
        assert _scope_supply_state(conn, manifest(sources=["gcal_events"]), []) is None


class TestItNeverRaises:
    def test_a_hostile_connection_returns_none(self) -> None:
        class Boom:
            def execute(self, *a, **k):
                raise RuntimeError("db is on fire")

        assert _scope_supply_state(Boom(), manifest(sources=["x"]), ["x"]) is None


class TestTheCallSiteItself:
    """Unit-testing the probe was not enough.

    `_scope_supply_state` had ten green tests while the line that CALLS it referenced an
    undefined name, because nothing exercised the empty-with-a-ledger path end to end.
    The live node answered `denied · name 'installed_source_ids' is not defined` on the
    very first query. A helper can be perfect and still never run.
    """

    def test_the_empty_branch_has_no_undefined_names(self) -> None:
        """Every name used where the cause is attributed must exist in that scope."""
        import ast
        import inspect
        import textwrap

        from topos.query import retrieval

        src = textwrap.dedent(inspect.getsource(retrieval.DefaultSignalRetrievalAdapter.retrieve))
        tree = ast.parse(src)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_scope_supply_state"
        ]
        assert calls, "the supply-state probe is no longer called from retrieve()"

        # Everything the function binds: parameters, assignments, comprehension and
        # with/for targets. That is what "in scope at this line" actually means.
        fn = tree.body[0]
        bound = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update((a.asname or a.name).split(".")[0] for a in node.names)
        for name in (n.id for c in calls for n in ast.walk(c) if isinstance(n, ast.Name)):
            if name in {"_scope_supply_state", "getattr", "None"}:
                continue
            assert name in bound or name in dir(retrieval), (
                f"{name!r} is used at the supply-state call site but is neither a "
                "parameter of retrieve() nor a module-level name — this is exactly the "
                "NameError that reached the live node"
            )
