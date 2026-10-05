"""Closed, mirrored wire grammar for answers written by the owner's node.

This module grants no authority. The signed request still has to pass the
ledger, and a reply still has to be checked against the exact node key.
"""
from __future__ import annotations

import base64
import re
import unicodedata
from typing import Annotated, Literal, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, StringConstraints, model_validator

from .canonical import PolicyError, canonical_bytes, digest, parse_json
from .contract import StrictModel
from .forwarding import SignedNodeResult, node_result_signing_bytes
from .knowledge_contract import AnswerMode, KnowledgeItem
from .search_contract import MAX_SEARCH_BYTES
from .signing import AuthorityBinding, SignedKnowledgeAnswerEnvelope, parse_authority, parse_envelope

ASK = "permissions.v2.answer"
FETCH = "permissions.v2.answer.fetch"
VERSION = "topos-answer/v1"
K_ANSWER = 8
MAX_ANSWER_BYTES = 40_000
ANSWER_ID = r"^ans_[0-9a-f]{32}$"
_CITATION = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")

AnswerId = Annotated[str, StringConstraints(strict=True, pattern=ANSWER_ID)]
Question = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4_000)]
AnswerText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8_000)]


class AskIntent(StrictModel):
    question: Question


class FetchIntent(StrictModel):
    answer_id: AnswerId


class AnswerPending(StrictModel):
    version: Literal["topos-answer/v1"]
    state: Literal["pending"]
    answer_id: AnswerId


class AnswerOnly(StrictModel):
    version: Literal["topos-answer/v1"]
    outcome: Literal["answered"]
    answer: AnswerText

    @model_validator(mode="after")
    def no_sources(self):
        if _CITATION.search(self.answer) or _control_character(self.answer):
            raise ValueError("answer contains a citation or control character")
        return self


class AnswerWithSources(StrictModel):
    version: Literal["topos-answer/v1"]
    outcome: Literal["answered"]
    answer: AnswerText
    records: list[KnowledgeItem] = Field(min_length=1, max_length=K_ANSWER)

    @model_validator(mode="after")
    def exact_citations(self):
        if _control_character(self.answer):
            raise ValueError("answer contains a control character")
        used = set()
        for match in _CITATION.finditer(self.answer):
            numbers = [int(part) for part in re.split(r"\s*[,;]\s*", match.group(1))]
            if not numbers or any(number < 1 or number > len(self.records) for number in numbers):
                raise ValueError("citation outside the supplied records")
            used.update(numbers)
        if used != set(range(1, len(self.records) + 1)):
            raise ValueError("each supplied record must be cited")
        return self


class NoAnswer(StrictModel):
    version: Literal["topos-answer/v1"]
    outcome: Literal["no_answer"]


def _control_character(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" and char != "\n" for char in value)


def effective_mode(policy, *, frontend_client_id: str) -> AnswerMode:
    declared = getattr(getattr(policy, "search", None), "answers", None)
    if declared is not None:
        return declared
    return "records" if policy.binding.client_id == frontend_client_id else "only"


def same_answer_authority(current, admitted) -> bool:
    """Another share can move node_epoch; no authority over this share may move."""
    a, b = parse_authority(current).model_dump(), parse_authority(admitted).model_dump()
    a.pop("node_epoch", None)
    b.pop("node_epoch", None)
    return a == b


def parse_answer_output(raw, *, request_type: str, mode: AnswerMode):
    """Parse only the body this request and effective mode may release."""
    try:
        value = raw.model_dump() if hasattr(raw, "model_dump") else parse_json(raw) if isinstance(raw, (str, bytes)) else raw
        if type(value) is not dict or request_type not in (ASK, FETCH) or mode not in ("only", "with_sources"):
            raise PolicyError("answer_output_invalid")
        if request_type == ASK:
            result = AnswerPending.parse(value)
        elif value.get("state") == "pending":
            result = AnswerPending.parse(value)
        elif value.get("outcome") == "no_answer":
            result = NoAnswer.parse(value)
        else:
            result = (AnswerOnly if mode == "only" else AnswerWithSources).parse(value)
        cap = MAX_SEARCH_BYTES if isinstance(result, AnswerWithSources) else MAX_ANSWER_BYTES
        if len(canonical_bytes(result.model_dump())) > cap:
            raise PolicyError("answer_output_invalid")
        return result
    except (PolicyError, ValueError, TypeError):
        raise PolicyError("answer_output_invalid") from None


def verify_node_answer(raw, *, trusted_keys: Mapping[str, bytes],
                       envelope: SignedKnowledgeAnswerEnvelope, output, mode: AnswerMode,
                       now: int) -> SignedNodeResult:
    """The unchanged node-result proof with the answer schema in place of search."""
    if type(now) is not int or now < 0:
        raise PolicyError("clock_invalid")
    envelope = parse_envelope(envelope)
    if not isinstance(envelope, SignedKnowledgeAnswerEnvelope):
        raise PolicyError("answer_output_invalid")
    body = parse_answer_output(output, request_type=envelope.request_type, mode=mode)
    result = SignedNodeResult.parse(raw.model_dump() if isinstance(raw, SignedNodeResult) else raw)
    authority = parse_authority({field: getattr(envelope, field) for field in AuthorityBinding.model_fields})
    if (result.envelope_hash != digest(envelope.model_dump()) or result.request_id != envelope.request_id
        or result.request_hash != envelope.request_hash or result.authority != authority
        or result.output_hash != digest(body.model_dump())):
        raise PolicyError("result_binding")
    if (result.checked_at > now or result.checked_at < envelope.issued_at or result.expires_at <= now
        or result.expires_at > envelope.expires_at or envelope.expires_at <= now):
        raise PolicyError("result_time")
    key = trusted_keys.get(result.kid)
    if not isinstance(key, bytes) or len(key) != 32:
        raise PolicyError("signing_key_unknown")
    try:
        signature = base64.urlsafe_b64decode(result.signature + "==")
        if base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=") != result.signature:
            raise ValueError("signature encoding")
        Ed25519PublicKey.from_public_bytes(key).verify(signature, node_result_signing_bytes(result))
    except (ValueError, InvalidSignature):
        raise PolicyError("signature_invalid") from None
    return result
