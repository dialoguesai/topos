"""NR166: the projection review store still reads through the base store's gated transaction.

R1 (d65c7da3) gave `EvidenceReviewStore._db` a keyword, `locked`, and made the store's own digest and clock reads
ask for it. `ProjectionReviewStore` overrides `_db` and did not take the word, so with R1's switch OFF every such
read of a projection store raised `TypeError`: 119 tests in 11 files, none of them in the files R1 was tested
with. No R1 test went through the projection store.

Pinned here: the projection store's digest read works with the switch off and on; inside a release's read-only
window its ordinary reads use the reader and still pass the store's own contract check, and its digest read
still takes the gated transaction; and, so the same mistake cannot recur quietly, every override of a method of
the two stores accepts every keyword its base accepts.
"""
from __future__ import annotations

import inspect
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import corpus, owner  # noqa: F401 -- fixtures
from tests.permissions_v2.test_projection_reviews import service  # noqa: F401 -- fixture
from topos.permissions_v2 import evidence, projection_reviews
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.projection_reviews import ProjectionReviewStore
from topos.storage.db import write_gate

pytestmark = pytest.mark.public
SWITCH = "TOPOS_PERMISSIONS_V2_DECIDE_OUTSIDE_GATE"


@pytest.mark.parametrize("switch", ["unset", "true"])
def test_the_projection_stores_digest_read_works_with_the_switch_off_and_on(service, monkeypatch, switch):
    monkeypatch.delenv(SWITCH, raising=False)
    if switch == "true":
        monkeypatch.setenv(SWITCH, "true")
    store = service.outputs
    assert isinstance(store, ProjectionReviewStore)
    with owner():
        digest = store.current_authority_digest()          # the base's read, through this store's own `_db`
        with store._db() as db:                            # and an ordinary transaction, as before
            assert db.execute("SELECT singleton FROM projection_contract").fetchall() == [(1,)]
            assert write_gate._WRITE_LOCK._is_owned()
    assert isinstance(digest, str) and len(digest) == 64


def test_inside_a_releases_window_it_reads_through_the_reader_and_keeps_its_contract_check(service, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    store = service.outputs
    with owner():
        outside = store.current_authority_digest()
        with store.release_reads():
            with store._db() as db:                        # the read-only reader: no gate, and it cannot write
                assert not write_gate._WRITE_LOCK._is_owned()
                assert db.execute("SELECT singleton FROM projection_contract").fetchall() == [(1,)]
                with pytest.raises(sqlite3.OperationalError):
                    db.execute("DELETE FROM projection_contract")
            assert store.current_authority_digest() == outside     # `locked`: the gated transaction, as always
            # The store's own contract check runs on the reader too: a file that is not a projection store refuses.
            monkeypatch.setattr(projection_reviews, "STORE_VERSION", "another-contract")
            with pytest.raises(Exception):
                with store._db():
                    pass


@pytest.mark.parametrize("base, sub", [(EvidenceReviewStore, ProjectionReviewStore)])
def test_an_override_takes_every_keyword_its_base_takes(base, sub):
    """A keyword added to a base method and asked for by the base's own code must be accepted by every override,
    or the override raises for callers that never heard of it."""
    missing = []
    for name, override in vars(sub).items():
        inherited = getattr(base, name, None)
        if not callable(inherited) or name.startswith("__"):
            continue
        override = inspect.unwrap(getattr(override, "__func__", override))
        inherited = inspect.unwrap(getattr(inherited, "__func__", inherited))
        try:
            theirs, ours = inspect.signature(inherited).parameters, inspect.signature(override).parameters
        except (TypeError, ValueError):
            continue
        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in ours.values()):
            continue
        keywords = {key for key, parameter in theirs.items()
                    if parameter.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    and key != "self"}
        missing += [f"{sub.__name__}.{name} lacks {key}" for key in sorted(keywords - set(ours))]
    assert missing == []


def test_the_two_stores_have_no_other_subclass_in_the_package():
    """The guard above names its pairs. A new subclass of either store must be added to it."""
    import pkgutil

    import topos.permissions_v2 as package

    found = set()
    for module in pkgutil.iter_modules(package.__path__):
        source = (package.__path__[0] + "/" + module.name + ".py")
        try:
            text = open(source, encoding="utf-8").read()
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("class ") and ("(EvidenceReviewStore)" in line or "(EvidenceResolver)" in line):
                found.add(line.split("(")[0].removeprefix("class ").strip())
    assert found == {"ProjectionReviewStore"}
    assert evidence.EvidenceReviewStore is EvidenceReviewStore
