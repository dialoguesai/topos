"""Closed, deterministic truth-pack protocol shared by the node and control plane.

A claim always names one first-person question. No lexical/LLM inference and no
other-person subject are accepted. Grant consent is separate from answering.
"""
from typing import Literal
from pydantic import model_validator
from .contract import StrictModel
from .canonical import PolicyError

VERIFY = "permissions.v2.verify_claim"
PACK = "icebreakers-v1"
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

def authorize_truth(policy, intent):
    if (policy.versions.capability != "permissions-beta/p2c-v3"
        or intent.pack_id not in (getattr(policy.search, "truth_packs", None) or [])):
        raise PolicyError("permission_denied")
