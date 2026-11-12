"""Content-free classification of completed verification responses for usage charts.

This adapter records the verdict already returned; it does not change verification.
Unknown response schemas remain unrecorded rather than becoming unanswerable. So does
``unable`` (the deadline passed or the model failed): it says nothing about the claim,
so it is shown as unavailable, not as a claim the data could not answer (owner,
29 Oct 2026).
"""
from collections.abc import Mapping

OUTCOMES = ("true", "false", "unanswerable")


def verification_outcome(result):
    if not isinstance(result, Mapping) or "error" in result:
        return None
    if "verdict" in result:
        return {"supported": "true", "contradicted": "false",
                "no_evidence": "unanswerable"}.get(result["verdict"]) if isinstance(result["verdict"], str) else None
    lanes = result.get("lanes")
    if not isinstance(lanes, Mapping) or not lanes:
        return None
    stances = []
    for lane in lanes.values():
        stance = lane.get("stance") if isinstance(lane, Mapping) else None
        if stance not in ("supports", "contradicts", "no_evidence"):
            return None
        stances.append(stance)
    if result.get("abstained") is True:
        return "unanswerable"
    decisive = set(stances) - {"no_evidence"}
    if decisive == {"supports"}:
        return "true"
    if decisive == {"contradicts"}:
        return "false"
    # Empty evidence or conflicting lanes cannot support a single yes/no result.
    return "unanswerable"
