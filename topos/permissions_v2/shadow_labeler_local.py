"""The shadow audit's local second labeler: the host's 9B, the pinned rubric, the policy's own predicates.

`shadow_labelers` is the seam and this is the one implementation. Registering it is what turns the shadow audit
from a thing that records holes into a thing that records verdicts.

**What it re-derives, and what it does not.** The model sees one record's text and the rubric, and answers with
that record's domains and sensitivity. It is never asked whether the record should have been released. That
question belongs to the policy, and the policy answers it here, through the engine's own `evaluate_predicate` over
the same attribute shape `release._attributes` builds -- so a re-score disagrees with the node only when the
*labels* differ, never because a model was asked to interpret a rule.

Two of the four attributes are deliberately NOT re-derived. `actor_role` and `subject` were proved by qualification
(native owner authorship, owner-only subject), not by any labeler, and a model looking at text cannot prove them
again; the re-score keeps them as the release path set them and re-derives only what is visible in the text. A
second labeler that guessed at authorship would produce disagreements about something the node never used a
labeler for.

**The vocabulary is closed and nothing is coerced.** A domain outside the rubric's eight, a sensitivity outside its
three, a missing field, a model that answers something that is not the schema: each makes the whole release
`unresolved`. The alternative -- dropping the unknown value and scoring the rest -- silently turns a model that
misunderstood the task into a data point, and the report card's whole output is a count of items that were checked.

**The model binding is pinned, and verified per call.** `qwen3.5:9b-mlx` at its reviewed digest, on the loopback
Ollama the campaign's own labeler uses (`scripts/permissions_beta/run_nl_experiment.py`), with the same
`format: json`, no-think, no-stream shape. An installed tag whose digest is not the reviewed one refuses rather
than answering: a re-score by a model nobody reviewed is not a second opinion, and it must not be able to become
one by someone pulling a newer tag.

**The rubric is pinned by bytes.** `shadow_rubric.pinned.md` is byte-identical to the gold set's
`rubric.pinned.md` and to the M1 machine-label track's `RUBRIC_SHA256`, so the three cannot drift into scoring
different things while claiming the same rubric. `test_L1` checks the sha, not the prose.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

VERSION = "topos-shadow-local-labeler/v1"
LABELER_ID = "local-qwen3.5-9b-mlx"
FAMILY = "qwen"
# The campaign's own local binding (`run_nl_experiment.py`): the same host, tag and reviewed digest, so a verdict
# here is attributable to the same model the program has already measured.
ORIGIN = "http://127.0.0.1:11434"
MODEL = "qwen3.5:9b-mlx"
MODEL_REVISION = "203e30078279db51132b9e026ceb7bb21330e5b1af67ef190671b375c9770404"
TIMEOUT_SECONDS = 25
MAX_TEXT_CHARS = 8_000

RUBRIC_PATH = Path(__file__).with_name("shadow_rubric.pinned.md")
RUBRIC_SHA256 = "d03b3358357cc44f85976a5eb3e80840158b701fc4e30d0e4ec5a819c959701d"
RUBRIC_BYTES = 1702
# The rubric's own closed vocabulary. Restated here so a labeler answer is checked against a set this module owns
# rather than against whatever the prose happens to contain; `test_L1` ties the two together.
DOMAINS = ("work", "plans", "hobbies", "health", "family", "finance", "relationships", "home")
SENSITIVITIES = ("none", "personal", "special")

PROMPT_VERSION = "topos-shadow-label-prompt/v1"
TEMPLATE = (
    "You label one message against a fixed rubric. Answer with JSON only, exactly "
    '{"domains": [...], "sensitivity": "..."} and nothing else.\n'
    "`domains` is every domain the message touches, from the rubric's list, possibly empty. "
    "`sensitivity` is exactly one value, the highest that applies.\n"
    "Use only the values the rubric defines. Do not explain. Do not add fields.\n\n")


def rubric() -> str:
    raw = RUBRIC_PATH.read_bytes()
    if len(raw) != RUBRIC_BYTES or hashlib.sha256(raw).hexdigest() != RUBRIC_SHA256:
        raise ValueError("the pinned rubric is not the reviewed one")
    return raw.decode("utf-8")


def system_prompt() -> str:
    return TEMPLATE + rubric()


def parse_labels(raw) -> dict | None:
    """The model's answer as rubric labels, or None. Nothing is coerced and nothing is dropped."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(raw, dict) or set(raw) != {"domains", "sensitivity"}:
        return None
    domains, sensitivity = raw["domains"], raw["sensitivity"]
    if not isinstance(domains, list) or any(not isinstance(item, str) for item in domains):
        return None
    if len(set(domains)) != len(domains) or any(item not in DOMAINS for item in domains):
        return None
    if not isinstance(sensitivity, str) or sensitivity not in SENSITIVITIES:
        return None
    return {"domains": list(domains), "sensitivity": sensitivity}


def attributes_of(labels: dict) -> dict:
    """The attribute shape the policy's predicates read, exactly as `release._attributes` builds it.

    `actor_role` and `subject` are what qualification proved, not what a labeler guessed: a model reading text
    cannot re-prove native owner authorship or an owner-only subject, so the re-score keeps them and re-derives
    only the two the rubric is about.
    """
    return {"domain": list(labels["domains"]), "actor_role": ["authored"],
            "subject": ["owner"], "sensitivity": [labels["sensitivity"]]}


def policy_verdict(policy, per_record: list[dict]) -> str:
    """Would this policy still release these records, given the labels the second labeler derived?

    `release.source_message_decision`'s combination, over re-derived labels instead of reviewed ones: a permit
    rule counts only if every record satisfies both its predicates, a deny rule denies if any does, and anything
    Unknown is Unknown rather than a guess in either direction. Returns permit | deny | indeterminate.
    """
    from .contract import Only, evaluate_predicate

    allows, denies, unknown_allow, unknown_deny = [], [], False, False
    labels = [attributes_of(item) for item in per_record]
    for rule in policy.rules:
        selection = rule.evidence_use.sources
        sources = set(selection.values if isinstance(selection, Only) else policy.source_universe.source_ids)
        tables = {table for form in rule.release.forms for table in form.tables}
        if "owner-engine-local" not in rule.evidence_use.processors.values:
            continue
        if rule.effect == "permit":
            if rule.release.ceiling != "raw" or not sources or not tables:
                continue
            values = [evaluate_predicate(rule.evidence_use.predicate, item) for item in labels]
            values += [evaluate_predicate(rule.release.predicate, item) for item in labels]
            if all(value is True for value in values):
                allows.append(rule.rule_id)
            elif False not in values and None in values:
                unknown_allow = True
        else:
            values = [evaluate_predicate(rule.evidence_use.predicate, item) for item in labels]
            values += [evaluate_predicate(rule.release.predicate, item) for item in labels]
            if True in values:
                denies.append(rule.rule_id)
            elif None in values:
                unknown_deny = True
    if denies:
        return "deny"
    if unknown_deny:
        return "indeterminate"
    if allows:
        return "permit"
    return "indeterminate" if unknown_allow else "deny"


def verdict_of(policy_answer: str) -> str:
    """permit -> agree, deny -> candidate_miss, anything else -> unresolved.

    `deny` is `candidate_miss`, never `miss`: this labeler flags, and only the owner concludes.
    """
    return {"permit": "agree", "deny": "candidate_miss"}.get(policy_answer, "unresolved")


class _PinnedTransport:
    """One loopback Ollama call per record, against the reviewed tag and digest. Never a pull, never a fallback."""

    def __init__(self, client):
        self.client = client
        self.calls = 0

    async def verify(self) -> None:
        response = await self.client.get(ORIGIN + "/api/tags", timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        installed = [row for row in (response.json().get("models") or []) if row.get("name") == MODEL]
        if len(installed) != 1 or installed[0].get("digest") != MODEL_REVISION:
            raise ValueError("the installed local model is not the reviewed revision")

    async def label(self, text: str):
        await self.verify()
        self.calls += 1
        response = await self.client.post(ORIGIN + "/api/chat", timeout=TIMEOUT_SECONDS, json={
            "model": MODEL, "stream": False, "think": False, "format": "json",
            "messages": [{"role": "system", "content": system_prompt()},
                         {"role": "user", "content": text[:MAX_TEXT_CHARS]}]})
        response.raise_for_status()
        body = response.json()
        if body.get("model") != MODEL or body.get("done") is not True:
            raise ValueError("the local model did not complete")
        return (body.get("message") or {}).get("content")


def open_transport():
    """The pinned loopback client. `trust_env=False`, so no proxy in the environment can move these bytes."""
    import httpx
    return _PinnedTransport(httpx.AsyncClient(trust_env=False, follow_redirects=False))


class LocalRubricLabeler:
    """The seam's `id`, `family` and `score`. One model call per released record, then the policy's own verdict."""

    id = LABELER_ID
    family = FAMILY

    def __init__(self, transport=None, *, open_transport=open_transport):
        self._transport = transport
        self._open = open_transport

    def score(self, records, policy) -> str:
        if policy is None:
            return "unresolved"
        try:
            # The labeler's own check, not the transport's. The prompt is built inside the transport, so a
            # transport that builds it differently -- or a stub -- would otherwise score against a rubric nobody
            # reviewed and the pin would only be checked on the path that happened to be used.
            rubric()
        except Exception:  # noqa: BLE001
            logger.warning("permissions v2 shadow labeler: the pinned rubric is not the reviewed one")
            return "unresolved"
        try:
            labels = asyncio.run(self._label_all(records))
        except Exception:  # noqa: BLE001 -- an unlabelled release is unresolved, never an agreement
            logger.warning("permissions v2 shadow labeler: the local model did not answer")
            return "unresolved"
        if labels is None:
            return "unresolved"
        return verdict_of(policy_verdict(policy, labels))

    async def _label_all(self, records) -> list[dict] | None:
        transport, owned = self._transport, False
        if transport is None:
            transport, owned = self._open(), True
        try:
            out = []
            for record in records:
                text = record.get("content") if isinstance(record, dict) else getattr(record, "content", None)
                if not isinstance(text, str) or not text.strip():
                    return None
                labels = parse_labels(await transport.label(text))
                if labels is None:
                    # One record the model could not label in the rubric's vocabulary makes the whole release
                    # unresolved: a release is released or withheld whole, so it is scored whole.
                    return None
                out.append(labels)
            return out or None
        finally:
            if owned:
                client = getattr(transport, "client", None)
                if client is not None:
                    await client.aclose()


def register(transport=None) -> LocalRubricLabeler:
    """Bind this labeler as the node's local second labeler. Nothing calls this automatically."""
    from . import shadow_labelers
    labeler = LocalRubricLabeler(transport)
    shadow_labelers.register("local", labeler)
    return labeler
