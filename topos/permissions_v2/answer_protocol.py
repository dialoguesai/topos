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
from pydantic import Field, StringConstraints, field_validator, model_serializer, model_validator

from .canonical import MAX_BYTES, PolicyError, canonical_bytes, digest, parse_json
from .contract import StrictModel
from .forwarding import SignedNodeResult, node_result_signing_bytes
from .knowledge_contract import AnswerMode, KnowledgeItem
from .search_contract import MAX_SEARCH_BYTES
from .signing import AuthorityBinding, SignedKnowledgeAnswerEnvelope, parse_authority, parse_envelope

ASK = "permissions.v2.answer"
FETCH = "permissions.v2.answer.fetch"
VERSION = "topos-answer/v1"
# A2A-4 amendment 13 (lane LA164, 13 Oct 2026): the answer protocol's own version, the number a node advertises as
# `permissions_v2_answer_version` and the number the asker's control plane says it accepts on the ask frame
# (`ACCEPTS_FIELD`, beside the principal stamp, outside the signed payload). 1 wrote answers (1.5.0); 2 carried the
# writer (amendment 9); 3 carries up to `K_ANSWER` records in a with-sources body, where 1 and 2 carried
# `K_ANSWER_LEGACY`. The body's `version` string does not move: a body within the older cap has the bytes it had.
# Answer protocol 4 (15 Oct 2026, the owner's decision; A2A-4 amendment 14): an asker whose control plane accepts 4 is
# sent up to `K_ANSWER_V4` cited records and an answer of up to `MAX_ANSWER_CHARS_V4` characters, inside byte caps of
# their own. An asker that accepts 3 keeps 16 records and 8,000 characters, one that says nothing 8 and 8,000, and
# both keep the byte caps they had. The grammar below admits the widest body; `parse_answer_output` holds an answered
# body to the caps of the version its asker accepts (`answer_within_caps`).
ANSWER_PROTOCOL_VERSION = 4
K_ANSWER_V4 = 30
K_ANSWER = 16
K_ANSWER_LEGACY = 8
ACCEPTS_FIELD = "answer_version"
#: What an ask frame without the field says: a control plane from before it, which parses the older cap.
LEGACY_ACCEPTED_VERSION = 2
#: The first version of each wider body: 3 carries `K_ANSWER` records; 4 carries `K_ANSWER_V4` records and
#: `MAX_ANSWER_CHARS_V4` characters. A caller that names no version is read as 3, the reading of before 4.
ANSWER_VERSION_V3 = 3
ANSWER_VERSION_V4 = 4
MAX_ANSWER_CHARS = 8_000
MAX_ANSWER_CHARS_V4 = 32_000
MAX_ANSWER_BYTES = 40_000
# Answer protocol 4's canonical byte caps. canonical.py writes a non-ASCII character as a six-byte escape (twelve for
# one outside the Basic Multilingual Plane, an emoji), so 32,000 characters of text can take 192,000 bytes. The
# with-sources cap is a number of its own: v3 and older keep MAX_SEARCH_BYTES (256,000), and no other cap (a
# search's, a batch's) is reused or raised for it. Both stay under the canonical ceiling (canonical.MAX_BYTES, 1 MiB)
# that every reader of a body enforces.
MAX_ANSWER_BYTES_V4 = 200_000
MAX_ANSWER_WITH_SOURCES_BYTES_V4 = 512_000
if not (MAX_ANSWER_BYTES < MAX_ANSWER_BYTES_V4 < MAX_BYTES
        and MAX_SEARCH_BYTES < MAX_ANSWER_WITH_SOURCES_BYTES_V4 < MAX_BYTES):
    raise RuntimeError("answer protocol 4's byte caps must sit above v3's and under the canonical ceiling")
ANSWER_ID = r"^ans_[0-9a-f]{32}$"
_CITATION = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")

AnswerId = Annotated[str, StringConstraints(strict=True, pattern=ANSWER_ID)]
Question = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4_000)]
AnswerText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=MAX_ANSWER_CHARS_V4)]
# A2A-4 amendment 9: what the sharer's policy discloses about the model that wrote the answer. The keys each level
# releases; a body carries exactly its policy's level's keys, or no writer at all (a node from before the field).
WriterProvider = Literal["ollama", "redpill", "openai", "anthropic", "grok", "platform"]
WriterText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
WRITER_FIELDS = ("origin", "provider", "model", "build", "template")
WRITER_KEYS: dict[str, frozenset] = {
    "origin": frozenset({"origin"}),
    "provider": frozenset({"origin", "provider"}),
    "model": frozenset({"origin", "provider", "model"}),
    "build": frozenset({"origin", "provider", "model", "build", "template"}),
}


class AskIntent(StrictModel):
    question: Question


class FetchIntent(StrictModel):
    answer_id: AnswerId


class AnswerPending(StrictModel):
    version: Literal["topos-answer/v1"]
    state: Literal["pending"]
    answer_id: AnswerId


class AnswerWriter(StrictModel):
    """Where the answer was written and by what, cut to the policy's level (`writer_level`)."""
    origin: Literal["local", "hosted"]
    provider: WriterProvider | None = None
    model: WriterText | None = None
    build: WriterText | None = None
    template: WriterText | None = None

    @model_validator(mode="before")
    @classmethod
    def declared_or_absent(cls, value):
        if isinstance(value, dict) and any(key in value and value[key] is None for key in WRITER_FIELDS[1:]):
            raise ValueError("writer field present but undeclared")
        return value

    @field_validator("model", "build", "template")
    @classmethod
    def printable(cls, value):
        if value is not None and any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("writer field contains a control character")
        return value

    @model_validator(mode="after")
    def exactly_one_level(self):
        if writer_level(self) is None:
            raise ValueError("writer keys match no disclosure level")
        return self

    @model_serializer(mode="wrap")
    def omit_undeclared(self, handler):
        encoded = handler(self)
        for key in WRITER_FIELDS[1:]:
            if encoded.get(key) is None:
                encoded.pop(key, None)
        return encoded


def writer_level(writer) -> str | None:
    """The disclosure level whose keys this writer carries exactly, else None."""
    present = frozenset(key for key in WRITER_FIELDS if getattr(writer, key, None) is not None)
    return next((level for level, keys in WRITER_KEYS.items() if keys == present), None)


def _writer_declared_or_absent(value):
    if isinstance(value, dict) and "writer" in value and value["writer"] is None:
        raise ValueError("writer present but undeclared")
    return value


def _omit_undeclared_writer(encoded):
    if encoded.get("writer") is None:
        encoded.pop("writer", None)
    return encoded


class AnswerOnly(StrictModel):
    version: Literal["topos-answer/v1"]
    outcome: Literal["answered"]
    answer: AnswerText
    writer: AnswerWriter | None = None

    @model_validator(mode="before")
    @classmethod
    def writer_declared_or_absent(cls, value):
        return _writer_declared_or_absent(value)

    @model_validator(mode="after")
    def no_sources(self):
        if _CITATION.search(self.answer) or _control_character(self.answer):
            raise ValueError("answer contains a citation or control character")
        return self

    @model_serializer(mode="wrap")
    def omit_undeclared(self, handler):
        return _omit_undeclared_writer(handler(self))


class AnswerWithSources(StrictModel):
    version: Literal["topos-answer/v1"]
    outcome: Literal["answered"]
    answer: AnswerText
    records: list[KnowledgeItem] = Field(min_length=1, max_length=K_ANSWER_V4)
    writer: AnswerWriter | None = None

    @model_validator(mode="before")
    @classmethod
    def writer_declared_or_absent(cls, value):
        return _writer_declared_or_absent(value)

    @model_serializer(mode="wrap")
    def omit_undeclared(self, handler):
        return _omit_undeclared_writer(handler(self))

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


def records_cap(accepted_version: int) -> int:
    """The most cited records a with-sources body may carry for an asker that accepts `accepted_version`: 30 from 4,
    16 at 3, 8 before (an ask frame without the field reads as 2)."""
    if accepted_version >= ANSWER_VERSION_V4:
        return K_ANSWER_V4
    return K_ANSWER if accepted_version >= ANSWER_VERSION_V3 else K_ANSWER_LEGACY


def answer_chars_cap(accepted_version: int) -> int:
    """The longest answer text, in characters, a body may carry for an asker that accepts `accepted_version`: 32,000
    from 4, 8,000 before."""
    return MAX_ANSWER_CHARS_V4 if accepted_version >= ANSWER_VERSION_V4 else MAX_ANSWER_CHARS


def answer_bytes_cap(accepted_version: int, *, with_sources: bool) -> int:
    """The most canonical bytes an answered body may take for an asker that accepts `accepted_version`: from 4,
    `MAX_ANSWER_WITH_SOURCES_BYTES_V4` with sources and `MAX_ANSWER_BYTES_V4` without; before 4, `MAX_SEARCH_BYTES`
    and `MAX_ANSWER_BYTES`, as they were."""
    if accepted_version >= ANSWER_VERSION_V4:
        return MAX_ANSWER_WITH_SOURCES_BYTES_V4 if with_sources else MAX_ANSWER_BYTES_V4
    return MAX_SEARCH_BYTES if with_sources else MAX_ANSWER_BYTES


def answer_within_caps(body, accepted_version: int) -> bool:
    """Whether an answered body is within the caps of an asker that accepts `accepted_version`: its cited records
    (`records_cap`), its answer's characters (`answer_chars_cap`) and its canonical bytes (`answer_bytes_cap`), writer
    and all. `body` is a parsed `AnswerOnly` or `AnswerWithSources`, or the dict it parses from; a body with
    `records` is read as with sources. Anything else is not within them."""
    value = body.model_dump() if isinstance(body, (AnswerOnly, AnswerWithSources)) else body
    if type(accepted_version) is not int or type(value) is not dict or type(value.get("answer")) is not str:
        return False
    with_sources = "records" in value
    if with_sources and (type(value["records"]) is not list or len(value["records"]) > records_cap(accepted_version)):
        return False
    if len(value["answer"]) > answer_chars_cap(accepted_version):
        return False
    try:
        size = len(canonical_bytes(value))
    except PolicyError:
        return False
    return size <= answer_bytes_cap(accepted_version, with_sources=with_sources)


def parse_accepted_version(raw) -> int:
    """The version an ask frame's `ACCEPTS_FIELD` names: exactly an integer from 1 to 99. Anything else is refused;
    the caller reads an absent field as `LEGACY_ACCEPTED_VERSION`, never this function."""
    if type(raw) is not int or not 1 <= raw <= 99:
        raise PolicyError("answer_version_invalid")
    return raw


def effective_mode(policy, *, frontend_client_id: str) -> AnswerMode:
    declared = getattr(getattr(policy, "search", None), "answers", None)
    if declared is not None:
        return declared
    return "records" if policy.binding.client_id == frontend_client_id else "only"


def effective_disclosure(policy) -> str:
    """The level the policy discloses about the writer (A2A-4 amendment 9): its signed `writer_disclosure`, else none."""
    declared = getattr(getattr(policy, "search", None), "writer_disclosure", None)
    return declared if declared in WRITER_KEYS else "none"


def same_answer_authority(current, admitted) -> bool:
    """Another share can move node_epoch; no authority over this share may move."""
    a, b = parse_authority(current).model_dump(), parse_authority(admitted).model_dump()
    a.pop("node_epoch", None)
    b.pop("node_epoch", None)
    return a == b


def parse_answer_output(raw, *, request_type: str, mode: AnswerMode, disclosure: str = "none",
                        accepted_version: int = ANSWER_VERSION_V3):
    """Parse only the body this request, effective mode and disclosure level may release.

    A final body may carry no writer (a node from before the field), or exactly the keys of `disclosure`'s level;
    any writer under a policy that discloses nothing is invalid. Callers that do not pass the level get the
    closed reading.

    `accepted_version` (answer protocol 4): the version the asker's control plane accepted on the ask. An answered
    body must be within that version's caps (`answer_within_caps`), whatever the grammar admits: an asker that
    accepts 3 is refused a body over 16 records or 8,000 characters. Callers that do not pass it get 3's reading,
    the reading of before 4."""
    try:
        value = raw.model_dump() if hasattr(raw, "model_dump") else parse_json(raw) if isinstance(raw, (str, bytes)) else raw
        if (type(value) is not dict or request_type not in (ASK, FETCH) or mode not in ("only", "with_sources")
                or type(accepted_version) is not int):
            raise PolicyError("answer_output_invalid")
        if request_type == ASK:
            result = AnswerPending.parse(value)
        elif value.get("state") == "pending":
            result = AnswerPending.parse(value)
        elif value.get("outcome") == "no_answer":
            result = NoAnswer.parse(value)
        else:
            result = (AnswerOnly if mode == "only" else AnswerWithSources).parse(value)
            if result.writer is not None and (disclosure not in WRITER_KEYS or writer_level(result.writer) != disclosure):
                raise PolicyError("answer_output_invalid")
            if not answer_within_caps(result, accepted_version):
                raise PolicyError("answer_output_invalid")
            return result
        if len(canonical_bytes(result.model_dump())) > MAX_ANSWER_BYTES:
            raise PolicyError("answer_output_invalid")
        return result
    except (PolicyError, ValueError, TypeError):
        raise PolicyError("answer_output_invalid") from None


def verify_node_answer(raw, *, trusted_keys: Mapping[str, bytes],
                       envelope: SignedKnowledgeAnswerEnvelope, output, mode: AnswerMode,
                       now: int, disclosure: str = "none",
                       accepted_version: int = ANSWER_VERSION_V3) -> SignedNodeResult:
    """The unchanged node-result proof with the answer schema in place of search, the body read at the caps of
    `accepted_version` (`parse_answer_output`)."""
    if type(now) is not int or now < 0:
        raise PolicyError("clock_invalid")
    envelope = parse_envelope(envelope)
    if not isinstance(envelope, SignedKnowledgeAnswerEnvelope):
        raise PolicyError("answer_output_invalid")
    body = parse_answer_output(output, request_type=envelope.request_type, mode=mode, disclosure=disclosure,
                               accepted_version=accepted_version)
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
