"""Index membership and release of kind ``interest`` (IF-5 §2, §3, §5; OD-52 P7).

A browsing interest joins a grant's knowledge index as one member per qualifying
(cluster, month), and is decided again, from the canonical database, at every release.
It is admitted only when every one of these holds:

- the node flag ``TOPOS_PERMISSIONS_V2_INTEREST_SOURCES`` is on (default off: with it off the
  family is invisible, and a grant naming it releases nothing from it);
- the grant is a knowledge grant whose ``search.result_types`` names ``interest``, whose
  ``search.tables`` names ``activity_events``, and whose permit rule lists ``browser_visits``
  among its sources and ``activity_events`` among its form tables (the existing decision);
- the object passed every deterministic check of ``interest_family`` (threshold, provenance,
  private windows, NSFW, exclusions, opt-outs, host, title, person, Off-limits), under the
  cluster's own label or, when that is a bad name, under a stored second label that passes the
  same label checks (``interest_relabel``);
- the month lies wholly inside the grant's rolling window (``interest_family.period_inside``), and the current,
  still-open month counts only when the grant releases time at ``day`` precision or finer (WS0's IF-5 I1 ruling:
  the day a month crosses the threshold or a band edge then reveals nothing the grant does not already allow; a
  grant that releases no time sees whole months only);
- the label has a current assessment that is neither special nor unknown and has no protected
  content (``interest_review.qualifies``);
- the grant's rules permit the label's domains and sensitivity. A visit is the owner's
  activity, never their words, so the attributes the rules see are ``actor_role: ambient`` and
  ``subject: owner``; a rule that requires ``authored`` does not permit an interest.

What a member carries (sealed by the index like every member): its table, source and record id
(``interest:<cluster>:<month>``), the object's content revision, the assessment it was
admitted under, and the instant it was built at. At release, :func:`release` rebuilds that one
cluster's month from the current rows and refuses unless the object, the assessment and the decision are
all still what they were and the month is still inside the window at the request's own time. The
members one read decides share a :func:`snapshot` of what every build reads alike.
What a recipient receives is the IF-5 §3 record: the label, the month, a strength band and the
source ``browser_visits``; the citation is the record itself. ``event_at`` is the month's first
day, and only when the grant releases time at ``day`` precision.

The search door (IF-5 Q&A I7) builds members with :func:`members`, keeps its index current with
:func:`indexed_current` (the object and its assessment as the build admitted them, decided at the
build's own instant, so a visit that arrives later stales one member's release, never the whole
index) and releases with :func:`release`. :class:`InterestRecord` restates §3's
``InterestResult`` field for field and the tests pin the two together.
"""
from __future__ import annotations

import os
from typing import Annotated, Literal, Optional

from pydantic import StringConstraints

from . import interest_family as fam
from . import interest_review as ir
from .canonical import PolicyError, digest
from .contract import Only, evaluate_predicate
from .knowledge_contract import KnowledgeRecord, Text

FLAG = "TOPOS_PERMISSIONS_V2_INTEREST_SOURCES"
KIND = "interest"
TABLE = fam.TABLE
SOURCE_ID = fam.SOURCE_ID
CAPABILITY = "permissions-beta/p2c-v3"
PROCESSOR = "owner-engine-local"
_TRUE = frozenset({"1", "true", "yes", "on"})


class InterestRecord(KnowledgeRecord):
    """IF-5 §3: a monthly browsing interest. An assessed topic label, never a URL, title or host."""
    kind: Literal["interest"]
    label: Text
    month: Annotated[str, StringConstraints(strict=True, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
    strength: Literal["low", "medium", "high"]


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(FLAG, "")).strip().lower() in _TRUE


def _rule_sources(rule, policy) -> set:
    selection = rule.evidence_use.sources
    return set(selection.values if isinstance(selection, Only) else policy.source_universe.source_ids)


def _rule_tables(rule) -> set:
    return {table for form in rule.release.forms for table in form.tables}


def admits(policy) -> bool:
    """Whether this signed grant asks for interests at all (§2). Structure only; no data is read."""
    search = getattr(policy, "search", None)
    versions = getattr(policy, "versions", None)
    if search is None or versions is None or getattr(versions, "capability", None) != CAPABILITY:
        return False
    if KIND not in (getattr(search, "result_types", None) or ()) or TABLE not in (search.tables or ()):
        return False
    return any(rule.effect == "permit" and SOURCE_ID in _rule_sources(rule, policy) and TABLE in _rule_tables(rule)
               for rule in policy.rules)


def attributes(classification) -> dict:
    return {"domain": list(classification.domains), "actor_role": ["ambient"], "subject": ["owner"],
            "sensitivity": [classification.sensitivity]}


def decide(policy, classification) -> tuple:
    """(verdict, permitting rule id or None) for one interest, as ``release.source_message_decision`` decides
    one direct-search leaf: a permit rule must hold for every domain on its own; any deny that applies wins;
    an undecidable rule is indeterminate, never a permit."""
    base = attributes(classification)
    allows, denies, unknown_allow, unknown_deny = [], [], False, False
    for rule in policy.rules:
        if PROCESSOR not in rule.evidence_use.processors.values:
            continue
        if SOURCE_ID not in _rule_sources(rule, policy) or TABLE not in _rule_tables(rule):
            continue
        if rule.effect == "permit":
            if rule.release.ceiling != "raw":
                continue
            per_domain = [{**base, "domain": [domain]} for domain in base["domain"]]
            values = [evaluate_predicate(predicate, attrs) for attrs in per_domain
                      for predicate in (rule.evidence_use.predicate, rule.release.predicate)]
            if values and all(value is True for value in values):
                allows.append(rule.rule_id)
            elif False not in values and None in values:
                unknown_allow = True
        else:
            values = [evaluate_predicate(predicate, base)
                      for predicate in (rule.evidence_use.predicate, rule.release.predicate)]
            if True in values:
                denies.append(rule.rule_id)
            elif None in values:
                unknown_deny = True
    verdict = ("deny" if denies else "indeterminate" if unknown_deny else "permit" if allows
               else "indeterminate" if unknown_allow else "deny")
    return verdict, (allows[0] if verdict == "permit" else None)


def _now_us(now: int) -> int:
    if type(now) is not int or now < 0:
        raise PolicyError("interest_time_invalid")
    return now * 1_000_000


#: The grant precisions under which the elapsed part of the current month may release (IF-5 Q&A I1).
OPEN_MONTH_PRECISIONS = frozenset({"day", "second"})


def open_month_allowed(policy) -> bool:
    return getattr(policy.search, "release_event_time", "none") in OPEN_MONTH_PRECISIONS


def _admitted(conn, obj, *, owner_id, policy, now, context_revision):
    """(assessment, rule id) when this object may be a member or release now, else None."""
    if not obj.complete and not open_month_allowed(policy):
        return None
    if not fam.period_inside(period_start_us=obj.period_start_us, period_end_us=obj.period_end_us,
                             now_us=_now_us(now), max_age_seconds=policy.search.window.max_age_seconds):
        return None
    assessment = ir.current(conn, owner_id=owner_id, obj=obj, context_revision=context_revision)
    if not ir.qualifies(assessment):
        return None
    verdict, rule_id = decide(policy, assessment.classification)
    return (assessment, rule_id) if verdict == "permit" else None


def members(conn, *, owner_id: str, policy, now: int, boundary, opt_outs: frozenset = frozenset()) -> list:
    """The interest entries a build of this grant's index admits, on ``conn``'s snapshot.

    Each entry has the fields the index seals for any member (table, source, dataset, record id)
    plus ``rank_text`` (the label), ``rank_event_us`` (the month's first instant) and ``interest``,
    the binding :func:`release` re-checks; ``built_at`` in it is ``now``, the instant
    :func:`indexed_current` decides the member at again. Empty unless the flag is on and the grant
    admits interests.
    """
    if not enabled() or not admits(policy):
        return []
    result = fam.build(conn, owner_id=owner_id, now_us=_now_us(now), boundary=boundary, opt_outs=opt_outs)
    context_revision, _terms = ir.context(boundary)
    out = []
    for obj in result.objects:
        admitted = _admitted(conn, obj, owner_id=owner_id, policy=policy, now=now, context_revision=context_revision)
        if admitted is None:
            continue
        assessment, rule_id = admitted
        out.append({"table": TABLE, "source_id": SOURCE_ID, "dataset_id": None, "record_id": obj.interest_id,
                    "rank_text": obj.label, "rank_event_us": obj.period_start_us,
                    "interest": {"cluster_id": obj.cluster_id, "month": obj.month,
                                 "content_revision": obj.content_revision,
                                 "assessment_revision": digest(assessment.model_dump()),
                                 "allow_clause_id": rule_id, "built_at": now}})
    return out


def _binding(sealed) -> Optional[dict]:
    """The binding of a well-formed sealed interest member: this family's table and source, no dataset, and a
    record id that is exactly its cluster and month. Anything else is not an interest member."""
    binding = sealed.get("interest") if isinstance(sealed, dict) else None
    if (not isinstance(binding, dict) or sealed.get("table") != TABLE or sealed.get("source_id") != SOURCE_ID
            or sealed.get("dataset_id") is not None or not isinstance(binding.get("cluster_id"), str)
            or sealed.get("record_id") != fam.interest_id(binding["cluster_id"], binding.get("month"))):
        return None
    return binding


def snapshot(conn):
    """What every interest decided on ``conn`` reads alike, computed once for the read that decides them
    (``interest_family.Snapshot``). It checks itself at every use: another connection, or a database changed
    since, and it is computed afresh."""
    return fam.Snapshot(conn)


def _current_object(conn, sealed: dict, *, owner_id, now, boundary, opt_outs, result=None, snapshot=None):
    """The object behind a sealed member, unchanged since its build, from ``result`` (a build of its cluster at
    ``now`` on ``conn``'s snapshot; a build of its cluster and month is made when absent), else None."""
    binding = _binding(sealed)
    if binding is None:
        return None
    if result is None:
        result = fam.build(conn, owner_id=owner_id, now_us=_now_us(now), boundary=boundary, opt_outs=opt_outs,
                           clusters=[binding["cluster_id"]], months=[binding.get("month")], snapshot=snapshot)
    found = [obj for obj in result.objects if obj.interest_id == sealed["record_id"]]
    if len(found) != 1 or found[0].content_revision != binding.get("content_revision"):
        return None
    return found[0]


def indexed_current(conn, sealed_members, *, owner_id: str, boundary, opt_outs: frozenset = frozenset(),
                    policy=None) -> frozenset:
    """The record ids of the sealed members that are still the members their build admitted, on ``conn``'s snapshot.

    Each member is decided at its own build's instant (``built_at``), not at the caller's clock: its object,
    rebuilt from the current rows with every deterministic check (visits, private windows, NSFW, exclusions,
    provenance, the label's form, host, title, person and Off-limits checks, Off-limits over the month's visits),
    must have the content revision the build sealed, and its label's assessment must still be current,
    releasable and the one it was admitted under. A visit that arrives after the build therefore changes
    nothing here; it changes the object at the request's own time, so :func:`release` withholds that one
    member until the next build. A row the build read that changed since, a relabel, a reassessment or a new
    person, exclusion or Off-limits hit drops it here.

    With ``policy`` the grant's decision is made again too, as :func:`members` made it. Without one the caller
    holds the policy fixed (the search index's basis pins its hash), and :func:`release` decides it again.
    One build per instant covers every member's cluster. Empty with the flag off.
    """
    if not enabled() or (policy is not None and not admits(policy)):
        return frozenset()
    wanted: dict = {}
    for sealed in sealed_members:
        binding = _binding(sealed)
        if binding is None or type(binding.get("built_at")) is not int:
            continue
        wanted.setdefault(binding["built_at"], []).append(sealed)
    context_revision, _terms = ir.context(boundary)
    current = set()
    shared = snapshot(conn)
    for instant, group in wanted.items():
        result = fam.build(conn, owner_id=owner_id, now_us=_now_us(instant), boundary=boundary, opt_outs=opt_outs,
                           clusters=sorted({sealed["interest"]["cluster_id"] for sealed in group}), snapshot=shared)
        for sealed in group:
            obj = _current_object(conn, sealed, owner_id=owner_id, now=instant, boundary=boundary,
                                  opt_outs=opt_outs, result=result)
            if obj is None:
                continue
            binding = sealed["interest"]
            if policy is not None:
                admitted = _admitted(conn, obj, owner_id=owner_id, policy=policy, now=instant,
                                     context_revision=context_revision)
                if admitted is None or admitted[1] != binding.get("allow_clause_id"):
                    continue
                assessment = admitted[0]
            else:
                assessment = ir.current(conn, owner_id=owner_id, obj=obj, context_revision=context_revision)
                if not ir.qualifies(assessment):
                    continue
            if digest(assessment.model_dump()) == binding.get("assessment_revision"):
                current.add(sealed["record_id"])
    return frozenset(current)


def member_current(conn, sealed: dict, *, owner_id: str, policy, now: int, boundary,
                   opt_outs: frozenset = frozenset()) -> bool:
    """Whether a sealed interest member still describes the grant's permitted set on ``conn``'s snapshot."""
    return release_object(conn, sealed, owner_id=owner_id, policy=policy, now=now, boundary=boundary,
                          opt_outs=opt_outs) is not None


def release_object(conn, sealed: dict, *, owner_id: str, policy, now: int, boundary, opt_outs: frozenset = frozenset(),
                   snapshot=None):
    """The object behind a sealed member when every admission check still holds at ``now``, else None.

    ``snapshot`` (:func:`snapshot` of ``conn``) is shared by the members one read decides: the build of each member's
    cluster and month then computes what every build reads alike once per read, not once per member."""
    if not enabled() or not admits(policy):
        return None
    obj = _current_object(conn, sealed, owner_id=owner_id, now=now, boundary=boundary, opt_outs=opt_outs,
                          snapshot=snapshot)
    if obj is None:
        return None
    context_revision, _terms = ir.context(boundary)
    admitted = _admitted(conn, obj, owner_id=owner_id, policy=policy, now=now, context_revision=context_revision)
    if admitted is None:
        return None
    assessment, rule_id = admitted
    binding = sealed["interest"]
    if (digest(assessment.model_dump()) != binding.get("assessment_revision")
            or rule_id != binding.get("allow_clause_id")):
        return None
    return obj


def record(obj, *, key: bytes, grant_id: str, policy) -> dict:
    """The recipient's record (IF-5 §3). The label is the only text; the citation is the record itself."""
    from .opaque_ids import opaque_record_id
    opaque = opaque_record_id(key, grant_id=grant_id, table=TABLE, source_id=SOURCE_ID, dataset_id=None,
                              record_id=obj.interest_id)
    precision = getattr(policy.search, "release_event_time", "none")
    event_at = obj.period_start_us // 1_000_000 if precision == "day" else None
    return InterestRecord.model_validate({
        "kind": KIND, "record_id": opaque, "content": obj.label, "label": obj.label, "month": obj.month,
        "strength": obj.band, "source_ids": [SOURCE_ID],
        "citations": [{"record_id": opaque, "source_id": SOURCE_ID, "content": f"{obj.label}, {obj.month}"}],
        "event_at": event_at}).model_dump()


def release(conn, sealed: dict, *, key: bytes, grant_id: str, owner_id: str, policy, now: int, boundary,
            opt_outs: frozenset = frozenset()) -> Optional[dict]:
    """The released record for one sealed member, decided again from the current rows, or None."""
    obj = release_object(conn, sealed, owner_id=owner_id, policy=policy, now=now, boundary=boundary,
                         opt_outs=opt_outs)
    return None if obj is None else record(obj, key=key, grant_id=grant_id, policy=policy)
