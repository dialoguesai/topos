"""Closed, deterministic truth-pack protocol shared by the node and control plane.

A claim always names one first-person question. No lexical inference and no
other-person subject are accepted. Grant consent is separate from answering.

`effort: high` (truth_interpret, truth_statement) adds two request types whose
payload is a free first-person statement instead of a claim: the node reads it
as one claim with its pinned local model, then answers exactly as for that
claim (`VERIFY_STATEMENT`), or only says what it read, for the player to
confirm before a verdict (`INTERPRET`). The verdict is never the model's.
"""
from typing import Annotated, Literal
from pydantic import Field, StringConstraints, model_validator
from .contract import StrictModel
from .canonical import PolicyError

VERIFY = "permissions.v2.verify_claim"
#: A free statement read by the node's interpreter, then checked as the claim it was read as.
VERIFY_STATEMENT = "permissions.v2.verify_claim_statement"
#: A free statement read by the node's interpreter and returned for confirmation; no answer is read.
INTERPRET = "permissions.v2.interpret_claim"
TRUTH_REQUEST_TYPES = (VERIFY, VERIFY_STATEMENT, INTERPRET)
PACK = "icebreakers-v1"
#: How long an interpretation can be linked to the structured check that confirms it.
INTERPRETATION_TTL_SECONDS = 120
QUESTIONS = (
    {"id": "drink", "prompt": "What is your go-to drink?", "choices": ["coffee", "tea", "neither"]},
    {"id": "time", "prompt": "When do you feel most like yourself?", "choices": ["morning", "evening", "it-varies"]},
    {"id": "weekend", "prompt": "Pick your ideal weekend.", "choices": ["outdoors", "at-home", "a-bit-of-both"]},
    {"id": "planning", "prompt": "How do you like to make plans?", "choices": ["plan-ahead", "be-spontaneous", "a-bit-of-both"]},
    {"id": "company", "prompt": "How do you recharge?", "choices": ["alone", "with-people", "a-bit-of-both"]},
)
BY_ID = {q["id"]: q for q in QUESTIONS}

class TruthIntent(StrictModel):
    pack_id: Literal["icebreakers-v1"]
    question_id: Literal["drink", "time", "weekend", "planning", "company"]
    value: str

    @model_validator(mode="after")
    def valid_choice(self):
        if self.value not in BY_ID[self.question_id]["choices"]:
            raise ValueError("unsupported choice")
        return self

Statement = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=280)]
InterpretationId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{22}$")]
QuestionId = Literal["drink", "time", "weekend", "planning", "company"]


class ConfirmedTruthIntent(TruthIntent):
    """A claim that names the interpretation it confirms or corrects. The id is provenance, never authority: the
    check is the claim's, and an unknown, expired or another grant's id only makes the receipt say "unlinked"."""
    interpretation_id: InterpretationId


def parse_intent(raw) -> TruthIntent:
    """A `permissions.v2.verify_claim` payload: the three-key claim, or the claim with its interpretation id."""
    if isinstance(raw, dict) and "interpretation_id" in raw:
        return ConfirmedTruthIntent.parse(raw)
    return TruthIntent.parse(raw)


class TruthStatement(StrictModel):
    pack_id: Literal["icebreakers-v1"]
    statement: Statement


class Interpreted(StrictModel):
    """What the node read a statement as and checked, signed with the verdict."""
    question_id: QuestionId
    value: str
    path: Literal["high"] = "high"
    interpreter: Annotated[str, StringConstraints(strict=True, pattern=r"^icebreakers-high/v[0-9]+$")]

    @model_validator(mode="after")
    def valid_choice(self):
        if self.value not in BY_ID[self.question_id]["choices"]:
            raise ValueError("unsupported choice")
        return self


class Proposition(StrictModel):
    pack_id: Literal["icebreakers-v1"]
    question_id: QuestionId
    value: str
    proposition: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]

    @model_validator(mode="after")
    def valid_choice(self):
        if self.value not in BY_ID[self.question_id]["choices"]:
            raise ValueError("unsupported choice")
        return self


class Alternative(StrictModel):
    value: str
    proposition: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]


class TruthResult(StrictModel):
    operation: Literal["verify_claim"] = "verify_claim"
    view_id: Literal["calibration.truth.v1"] = "calibration.truth.v1"
    verdict: Literal["supports", "contradicts", "unknown"]
    basis: Literal["owner_answer", "insufficient_evidence"]

    @model_validator(mode="after")
    def coherent(self):
        if (self.verdict == "unknown") != (self.basis == "insufficient_evidence"):
            raise ValueError("inconsistent verdict")
        return self

class TruthStatementResult(TruthResult):
    """`VERIFY_STATEMENT`'s signed output: the verdict on the claim the statement was read as, and that claim."""
    interpreted: Interpreted


class InterpretationResult(StrictModel):
    """`INTERPRET`'s signed output: what the statement was read as, the question's other answers, and the id the
    structured check may carry to say it was shown. No verdict and no owner answer is read for it."""
    operation: Literal["interpret_claim"] = "interpret_claim"
    view_id: Literal["calibration.interpret.v1"] = "calibration.interpret.v1"
    status: Literal["needs_confirmation"] = "needs_confirmation"
    interpretation: Proposition
    alternatives: Annotated[list[Alternative], Field(min_length=2, max_length=2)]
    interpretation_id: InterpretationId
    expires_at: Annotated[int, Field(strict=True, ge=0)]
    interpreter: Annotated[str, StringConstraints(strict=True, pattern=r"^icebreakers-high/v[0-9]+$")]

    @model_validator(mode="after")
    def same_question(self):
        values = [self.interpretation.value, *(a.value for a in self.alternatives)]
        if sorted(values) != sorted(BY_ID[self.interpretation.question_id]["choices"]):
            raise ValueError("alternatives are not the question's other answers")
        return self


def authorize_truth(policy, intent):
    if (policy.versions.capability != "permissions-beta/p2c-v3"
        or intent.pack_id not in (getattr(policy.search, "truth_packs", None) or [])):
        raise PolicyError("permission_denied")
