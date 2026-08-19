"""The narrowing ledger's vocabulary is closed by the mechanism, not by care.

``narrowing.py``'s docstring credited "enums by construction, not by care". An
independent pass tested that claim by hand on 2026-08-19 and it was false::

    >>> _slug("Sarah Chen divorce lawyer meeting")
    'sarah_chen_divorce_lawyer_meeting'
    >>> ledger.record("scope_routing", "dropped", "Sarah Chen divorce lawyer meeting")
    >>> ledger.as_public()
    {'ledger': [{'stage': 'scope_routing', 'action': 'dropped',
                 'reason': 'sarah_chen_divorce_lawyer_meeting'}]}

``as_public`` is, per its own docstring, what leaves the node. All three slug
implementations — this one, the control plane's and the front end's — were character
filters with a 64-character cap: they sanitize, they do not constrain membership, so a
name and a legal matter travelled through fully legible. There was no leak on the day,
because every live call site passed a literal or a module constant. The guarantee rested
on every *future* call site, which is exactly what the docstring said it did not.

Three properties here, and the first is the probe above run verbatim:

1. **Free text into ``record`` does not reach ``as_public``.** No repo had this test.
2. **The declared sets cover the producers.** The reason set is long and hand-written;
   a rename in ``exclusion.py`` or ``negotiation.py`` would otherwise start emitting
   ``unrecognized`` in production with every test still green.
3. **The published contract matches this module.** The control plane and the front end
   test against ``topos/protocol/narrowing_vocabulary.json`` because they cannot import
   from here; a set that drifts from the file it publishes is worse than no file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from topos.query import narrowing as N
from topos.query.narrowing import NarrowingEntry, NarrowingLedger

VOCABULARY_PATH = (
    Path(__file__).resolve().parents[2] / "topos" / "protocol" / "narrowing_vocabulary.json"
)

#: The verification's own probe, kept verbatim so this file answers the finding it
#: exists for rather than a paraphrase of it.
OWNER_TEXT = "Sarah Chen divorce lawyer meeting"


class TestFreeTextCannotReachThePublicLedger:
    def test_the_measured_probe(self) -> None:
        led = NarrowingLedger()
        led.record("scope_routing", "dropped", OWNER_TEXT)
        published = led.as_public()
        assert published == {
            "ledger": [
                {"stage": "scope_routing", "action": "dropped", "reason": N.UNRECOGNIZED}
            ]
        }
        blob = json.dumps(published).lower()
        assert "sarah" not in blob
        assert "divorce" not in blob
        assert "lawyer" not in blob

    def test_the_slug_alone_would_not_have_stopped_it(self) -> None:
        """Naming the thing the previous mechanism actually did, so a revert is loud."""
        assert N._slug(OWNER_TEXT) == "sarah_chen_divorce_lawyer_meeting"
        assert N._member(OWNER_TEXT, N.REASONS) == N.UNRECOGNIZED

    @pytest.mark.parametrize("field", ["stage", "action", "reason"])
    def test_every_public_field_is_closed_not_just_reason(self, field: str) -> None:
        led = NarrowingLedger()
        led.record(
            OWNER_TEXT if field == "stage" else "scope_routing",
            OWNER_TEXT if field == "action" else "dropped",
            OWNER_TEXT if field == "reason" else "max_scope_routes",
        )
        assert led.as_public()["ledger"][0][field] == N.UNRECOGNIZED

    def test_direct_construction_is_closed_too(self) -> None:
        """``as_public`` serializes the dataclass, and the dataclass is public."""
        entry = NarrowingEntry(stage=OWNER_TEXT, action=OWNER_TEXT, reason=OWNER_TEXT)
        assert entry.as_public() == {
            "stage": N.UNRECOGNIZED,
            "action": N.UNRECOGNIZED,
            "reason": N.UNRECOGNIZED,
        }

    def test_a_merged_entry_from_another_codebase_is_re_checked(self) -> None:
        """An upstream that is careless cannot widen this one's set."""
        led = NarrowingLedger()
        led.extend_public(
            [{"stage": "compose", "action": "truncated", "reason": OWNER_TEXT, "dropped": 3}]
        )
        assert led.as_public()["ledger"] == [
            {
                "stage": "compose",
                "action": "truncated",
                "reason": N.UNRECOGNIZED,
                "dropped": 3,
            }
        ]

    def test_an_unknown_cause_is_still_refused(self) -> None:
        led = NarrowingLedger()
        led.empty(OWNER_TEXT)
        assert led.empty_cause is None
        assert "empty_cause" not in led.as_public()

    def test_closing_the_set_did_not_make_record_raise(self) -> None:
        """Telemetry may cost a debug line, never a turn — including on a hostile value."""

        class Hostile:
            def __str__(self) -> str:
                raise RuntimeError("boom")

        led = NarrowingLedger()
        led.record(Hostile(), Hostile(), Hostile())
        assert led.as_public()["ledger"] == [
            {"stage": N.UNRECOGNIZED, "action": N.UNRECOGNIZED, "reason": N.UNRECOGNIZED}
        ]

    def test_telemetry_carries_no_text_either(self) -> None:
        led = NarrowingLedger()
        led.record(OWNER_TEXT, OWNER_TEXT, OWNER_TEXT)
        assert "sarah" not in json.dumps(led.as_telemetry()).lower()


class TestTheDeclaredSetsCoverTheProducers:
    """Every constant a producer module emits has to be a member, or it silently becomes
    ``unrecognized`` in production while nothing goes red."""

    def test_the_empty_causes_double_as_reasons(self) -> None:
        # `empty(cause, stage=...)` with no explicit reason records the cause itself.
        assert N.EMPTY_CAUSES <= N.REASONS

    def test_the_sentinel_is_a_member_of_every_set(self) -> None:
        # Coercion has to be idempotent: an entry merged in from another codebase that
        # already collapsed must stay collapsed, not be re-flagged on every hop.
        for allowed in (N.STAGES, N.ACTIONS, N.REASONS):
            assert N.UNRECOGNIZED in allowed
            assert N._member(N.UNRECOGNIZED, allowed) == N.UNRECOGNIZED

    def test_exclusion_constants_are_members(self) -> None:
        from topos.query import exclusion

        for name in dir(exclusion):
            if name.startswith("REASON_"):
                assert getattr(exclusion, name) in N.REASONS, name
            if name.startswith("ACTION_"):
                assert getattr(exclusion, name) in N.ACTIONS, name

    def test_negotiation_reason_codes_are_members(self) -> None:
        from topos.query import negotiation

        for name in dir(negotiation):
            if name.startswith("REASON_"):
                assert getattr(negotiation, name) in N.REASONS, name

    def test_manifest_validation_codes_are_members(self) -> None:
        """`handlers/query.py` records `exc.code` as the reason for a manifest denial."""
        import ast
        import inspect

        from topos.query import manifest_validation

        source = inspect.getsource(manifest_validation)
        codes = {
            node.args[0].value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ManifestValidationError"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        assert codes, "no denial codes found — this check has gone blind"
        assert codes <= N.REASONS, sorted(codes - N.REASONS)

    def test_supply_states_are_members(self) -> None:
        from topos.query import retrieval

        for name in ("SUPPLY_NO_SOURCE", "SUPPLY_NEVER_DELIVERED", "SUPPLY_DELIVERED_THEN_EMPTIED"):
            assert getattr(retrieval, name) in N.REASONS, name

    def test_turn_outcomes_that_end_a_turn_are_members(self) -> None:
        from topos.query.pipeline import _NOT_QUERIED_OUTCOMES

        assert set(_NOT_QUERIED_OUTCOMES) <= N.REASONS

    def test_the_stage_constants_are_members(self) -> None:
        declared = {
            getattr(N, name) for name in dir(N) if name.startswith("STAGE_")
        }
        assert declared == N.STAGES - {N.UNRECOGNIZED}


class TestThePublishedContractMatches:
    """`topos/protocol/narrowing_vocabulary.json` is what the other two repos test
    against. A file that drifts from this module is a comment saying 'keep in sync'."""

    def _doc(self) -> dict:
        assert VOCABULARY_PATH.is_file(), f"missing contract at {VOCABULARY_PATH}"
        return json.loads(VOCABULARY_PATH.read_text())

    def test_every_set_matches_exactly(self) -> None:
        doc = self._doc()
        assert set(doc["stages"]) == N.STAGES
        assert set(doc["actions"]) == N.ACTIONS
        assert set(doc["reasons"]) == N.REASONS
        assert set(doc["empty_causes"]) == N.EMPTY_CAUSES
        assert doc["sentinel"] == N.UNRECOGNIZED

    def test_the_precedence_order_is_published_not_just_the_membership(self) -> None:
        # The front end reproduces this order to decide which cause outranks which.
        assert tuple(self._doc()["empty_cause_precedence"]) == N._CAUSE_PRECEDENCE
