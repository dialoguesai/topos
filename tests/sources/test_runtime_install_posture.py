"""An install payload that omits `posture` must not downgrade the source.

`install_source_definition` replaces the bundled definition wholesale, and
`DataSourceDefinition.posture` defaults to `mixed`. Measured on the live node 2026-08-27:
active installs for `browser_visits`, `imessage`, `grow_journal` and `github_activity` all
carried `posture: null`, so every one resolved `mixed` in the engine while the bundled
registry said `ambient`. Nothing raised — posture simply stopped distinguishing anything,
and every consumer of it lost its input silently.
"""

from __future__ import annotations

import pytest
from dataclasses import replace as dataclasses_replace

from topos.sources.registry import BUNDLED_REGISTRY
from topos.sources.runtime_install import _inherit_bundled_posture


def _bundled_with_posture(wanted: str):
    for sid, defn in BUNDLED_REGISTRY.items():
        if getattr(defn, "posture", None) == wanted:
            return sid
    pytest.skip(f"no bundled source with posture={wanted}")


def test_an_omitted_posture_keeps_the_bundled_one():
    sid = _bundled_with_posture("ambient")
    out = _inherit_bundled_posture({"source_id": sid})
    assert out["posture"] == "ambient", "an omitted field means unchanged, not 'mixed'"


def test_a_declared_posture_still_wins():
    """An install must remain able to deliberately re-posture a source."""
    sid = _bundled_with_posture("ambient")
    assert _inherit_bundled_posture({"source_id": sid, "posture": "personal"})["posture"] \
        == "personal"


def test_an_unknown_source_is_left_alone():
    assert "posture" not in _inherit_bundled_posture({"source_id": "a_brand_new_connector"})


def test_a_payload_with_no_source_id_is_left_alone():
    assert _inherit_bundled_posture({}) == {}


def test_the_live_regression_sources_keep_their_posture():
    """The four that actually broke."""
    for sid in ("browser_visits", "imessage", "grow_journal", "github_activity"):
        if sid not in BUNDLED_REGISTRY:
            continue
        expected = BUNDLED_REGISTRY[sid].posture
        assert _inherit_bundled_posture({"source_id": sid})["posture"] == expected


class TestTheReadPathToo:
    """`effective_posture` must survive an already-installed definition that carries the
    bare `mixed` default — the live node's four broken sources were installed long before
    the builder fix, so the fix has to work on data already on disk."""

    def test_a_replaced_definition_carrying_only_the_default_falls_back_to_bundled(self,
                                                                                   monkeypatch):
        from topos.sources import registry as R

        sid = _bundled_with_posture("ambient")
        replaced = dataclasses_replace(R.BUNDLED_REGISTRY[sid], posture="mixed")
        monkeypatch.setitem(R.REGISTRY, sid, replaced)
        assert R.effective_posture(sid) == "ambient"

    def test_a_deliberate_re_posture_is_still_honoured(self, monkeypatch):
        """Only the bare default falls back. An install that really says `personal` wins."""
        from topos.sources import registry as R

        sid = _bundled_with_posture("ambient")
        replaced = dataclasses_replace(R.BUNDLED_REGISTRY[sid], posture="personal")
        monkeypatch.setitem(R.REGISTRY, sid, replaced)
        assert R.effective_posture(sid) == "personal"

    def test_a_genuinely_mixed_bundled_source_stays_mixed(self, monkeypatch):
        from topos.sources import registry as R

        sid = _bundled_with_posture("mixed")
        assert R.effective_posture(sid) == "mixed"
