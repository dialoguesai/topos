"""SUITE-NEGATION-TWINS — "but nothing from X" has to change the answer, and so does "not".

protects: SYS-query I1, from the other side. The transform family (`test_transform_invariance_
family.py`) pins that a wrapper around a question must NOT change what it retrieves. This pins the
opposite obligation: a word that changes what the owner asked for MUST change what comes back. An
instrument that only checks invariance can be satisfied by a pipeline that ignores half the
sentence.

`test_enforced_exclusion.py` already covers the mechanism thoroughly — compilation, ambiguity,
filtering, and what may leave the node. What is missing, and what backlog option H-03 asks for, is
the RELATION over a case and its twin: same question, one exclusion added, and the difference
between the two traces is the measurement. A unit test says the filter works on a packet someone
built for it; a twin says the filter works on the packet the pipeline actually produces.

Two shapes:

  * **exclusion twins** — `X` versus `X, but nothing from <source>`. The twin must lose exactly the
    excluded rows and keep the others. Losing everything is not enforcement, it is breakage; losing
    nothing is the failure the module was written to prevent.
  * **NevIR-shaped did / did-not** — a question and its negation must not produce identical
    retrieval. This one is recorded rather than demanded: see the class docstring.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from topos.query.exclusion import apply_exclusions, parse_exclusions

pytestmark = [pytest.mark.check("C-quality-negation-twins")]


def _packet() -> Dict[str, Any]:
    """A packet with rows from three distinguishable places, so a twin's loss is attributable."""
    return {
        "rows": [
            {"_table": "journal_entries", "record_id": "j1", "content": "morning pages"},
            {"_table": "journal_entries", "record_id": "j2", "content": "evening pages"},
            {"_table": "conversation_messages", "record_id": "m1", "content": "standup notes"},
            {"_table": "calendar_events", "record_id": "e1", "content": "roadmap review"},
        ],
        "summaries": [
            {"summary_text": "journal roundup", "retrieval_source": "grow_journal"},
            {"summary_text": "work roundup", "dimension": "Work"},
        ],
    }


def _ids(packet: Dict[str, Any]) -> List[str]:
    return [r["record_id"] for r in packet.get("rows", [])]


#: (control, twin, what the twin must lose). The control is the same question in every pair, so the
#: only difference between the two traces is the clause under test.
_CONTROL = "what happened in my week"
_TWINS = [
    ("journal", f"{_CONTROL}, but nothing from the journal", ["j1", "j2"]),
]


class TestAnExclusionTwinLosesExactlyWhatItNamed:
    @pytest.mark.parametrize("label,twin,expected_lost", _TWINS, ids=[t[0] for t in _TWINS])
    def test_the_twin_differs_from_its_control_in_the_named_way(
        self, label: str, twin: str, expected_lost: List[str]
    ) -> None:
        control_packet, twin_packet = _packet(), _packet()
        apply_exclusions(control_packet, parse_exclusions(_CONTROL))
        apply_exclusions(twin_packet, parse_exclusions(twin))
        control_ids, twin_ids = _ids(control_packet), _ids(twin_packet)
        lost = [i for i in control_ids if i not in twin_ids]
        assert lost == expected_lost, f"{label}: expected to lose {expected_lost}, lost {lost}"

    def test_the_control_keeps_everything(self) -> None:
        """The two-sided floor. A control that already lost rows would make any twin look
        enforcing."""
        packet = _packet()
        before = _ids(copy.deepcopy(packet))
        apply_exclusions(packet, parse_exclusions(_CONTROL))
        assert _ids(packet) == before

    def test_an_exclusion_that_takes_everything_is_not_enforcement(self) -> None:
        """Named because it is the failure mode a too-eager filter produces, and it passes any
        test that only checks the excluded thing is gone."""
        packet = _packet()
        apply_exclusions(packet, parse_exclusions(_TWINS[0][1]))
        assert _ids(packet), "the twin emptied the packet; that is breakage, not exclusion"
        assert "m1" in _ids(packet) and "e1" in _ids(packet)

    def test_an_exclusion_the_pipeline_cannot_compile_changes_nothing_and_says_so(self) -> None:
        """The fail-loud half. Silently dropping an uncompilable fragment would let an answer read
        as though the owner's exclusion had been honoured — worse than not enforcing at all."""
        packet = _packet()
        spec = parse_exclusions(f"{_CONTROL}, but nothing about the thing from last spring")
        outcome = apply_exclusions(packet, spec)
        assert spec.requested and not spec.fully_enforced
        assert spec.as_public()["not_applied"] >= 1
        assert _ids(packet) == _ids(_packet()), "an uncompilable exclusion silently filtered rows"
        assert outcome.dropped == 0


class TestDidVersusDidNot:
    """NevIR-shaped recognition, RECORDED rather than demanded.

    "what did I ship" and "what did I not ship" are different questions, and a retriever that keys
    on content words treats them as the same one — the negation is a stopword away from invisible.
    The honest position today is that the engine has no negation-of-the-verb handling at all, so
    demanding sensitivity here would be demanding a feature, not guarding one.

    What this does is fix the current behaviour in place, so that the day someone builds it, the
    change is visible rather than incidental. The exclusion path above is the negation the product
    actually ships.
    """

    @pytest.mark.parametrize(
        "affirmative,negative",
        [
            ("what did I ship this week", "what did I not ship this week"),
            ("who did I talk to", "who did I not talk to"),
        ],
    )
    def test_a_verb_negation_is_not_an_exclusion(self, affirmative: str, negative: str) -> None:
        """Neither form compiles to an exclusion — "not" before a verb is not "but nothing from".
        Recorded so that a future negation feature does not arrive by accidentally widening the
        exclusion parser, which would make "what did I not ship" delete the shipping rows."""
        assert not parse_exclusions(affirmative).requested
        assert not parse_exclusions(negative).requested

    def test_the_two_forms_are_indistinguishable_to_the_exclusion_plane(self) -> None:
        a, b = _packet(), _packet()
        apply_exclusions(a, parse_exclusions("what did I ship this week"))
        apply_exclusions(b, parse_exclusions("what did I not ship this week"))
        assert _ids(a) == _ids(b)
