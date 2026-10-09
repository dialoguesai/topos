"""Secure-processing policy: where content about a protected entity may be inferred.

A black hole is not only about who may *read* an entity — it is also about which
models may ever see text that mentions it. D1 fixes the admissible set: the local
adapters plus the Red Pill TEE for the default `secure` tier, local only for
`local_only`. BYOK and direct OpenAI are admitted by neither.

Two decisions worth stating plainly, because both are easy to get wrong in the
comfortable direction:

**Redirect before refusing.** When a task is tainted and its configured provider
is inadmissible, the policy rewrites the request onto a secure provider rather
than failing. Failing would be safe but would quietly stop enriching the owner's
own data — the protected entity's records would silently rot while every other
record improved. Refusal is reserved for the case where no secure provider can
serve the task at all.

**Taint is a text scan, and that is a floor, not a ceiling.** (Names as the share boundary reads them: a
short name as a whole token with its forms, a long one anywhere; BL-112.) Structured
taint (record ids joined through `entity_mentions`) is exact and belongs at the
call sites that have ids to hand; this module catches the rest — free text
assembled from who-knows-where. A scan cannot see a paraphrase or a nickname the
resolver never bound, so it is the last line, not the only one.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Set

from .blackhole import TIER_PROVIDERS, normalize_entity_name

logger = logging.getLogger("topos.features.lifecycle.blackhole_llm")

# Providers that never leave the device. Preferred landing spot for a redirect.
LOCAL_PROVIDERS = frozenset({"ollama", "huggingface"})


@dataclass(frozen=True)
class EgressVerdict:
    """What the policy decided for one task."""

    tainted: bool
    allowed_providers: frozenset
    matched_terms: tuple
    # Provider to use. None with tainted=True means nothing admissible was found.
    provider: Optional[str] = None
    redirected_from: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return self.tainted and self.provider is None

    @property
    def redirected(self) -> bool:
        return self.redirected_from is not None


def _iter_strings(value: Any) -> Iterable[str]:
    """Every string reachable in a task input, however nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _iter_strings(v)
    elif value is not None and not isinstance(value, (int, float, bool)):
        yield str(value)


def text_of(payload: Any) -> str:
    """Flatten a task input (or prompt context) into one scannable blob."""
    try:
        return " ".join(_iter_strings(payload))
    except Exception:  # noqa: BLE001 — a weird payload must not disable the gate
        try:
            return json.dumps(payload, default=str)
        except Exception:  # noqa: BLE001
            return str(payload)


def _blackhole_rows(conn: sqlite3.Connection):
    """The entries this gate reads. The gate decides where the OWNER'S OWN text is processed (enrichment, his chat
    turns, the local generation route), so an entry the upgrade carried and the owner has not acted on is not among
    them: an older "exclude" was a choice about sharing, and by itself it moves no model call to another provider
    and blocks none (`off_limits_view.for_own_processing`). No answer to another person is made through this gate:
    the share doors run their own pinned local model. Were this ever reached on behalf of a recipient, the rows
    would be every entry."""
    from .blackhole import BlackholeStore
    from .off_limits_view import for_own_processing

    return BlackholeStore(conn).list(view=for_own_processing())


def _boundary_names_hit(names, raw_text: str) -> bool:
    """Whether the share boundary's own matcher (`entity_boundary.text_hits`) finds any of these names in this raw
    text, in ONE pass over all of them (review R-N1-151 R2 N3: one pass per name cost 0.80 s on a 115 KB payload with
    24 names). Any text the boundary cannot read is a hit (its own rule: fail closed)."""
    if not names or not raw_text:
        return False
    from ...permissions_v2.entity_boundary import skeleton, split_terms, text_hits

    keys = {skeleton(name) for name in names} - {""}
    if not keys:
        return False
    short, long_terms = split_terms(keys)
    try:
        return bool(text_hits(raw_text, short, long_terms))
    except Exception:  # noqa: BLE001 -- a text the boundary cannot read is treated as protected
        return True


def _names_hit(terms, raw_text: str, haystack: str) -> bool:
    """One entry's names at the gate: as the share boundary reads them, and (WS0's ruling on N1) a name in the Arabic
    or Hebrew script also anywhere, as 1.5.0 read it (a clitic is written joined to the front of a name)."""
    from .blackhole import OffLimitsTerms, clitic_script

    clitic = {name for name in terms.names if clitic_script(name)}
    if clitic and OffLimitsTerms(names=clitic).found(haystack) is not None:
        return True
    return _boundary_names_hit(terms.names, raw_text)


def evaluate(
    conn: Optional[sqlite3.Connection],
    payload: Any,
    *,
    provider: str,
) -> EgressVerdict:
    """Decide whether `payload` may go to `provider`, and where it should go instead.

    With no database there is nothing to protect against — an engine with no
    store has no black holes — so the payload passes unchanged. A database that
    is present but unwell raises out of the store rather than reporting "nothing
    is protected"; that failure must not be swallowed here.
    """
    configured = (provider or "").strip().lower()
    if conn is None:
        return EgressVerdict(False, frozenset(), (), provider=configured)

    rows = _blackhole_rows(conn)
    from .record_protection import RecordProtectionStore

    record_floor = bool(RecordProtectionStore(conn).list())
    if not rows and not record_floor:
        return EgressVerdict(False, frozenset(), (), provider=configured)

    raw_text = text_of(payload)
    haystack = normalize_entity_name(raw_text)
    if not haystack and not record_floor:
        return EgressVerdict(False, frozenset(), (), provider=configured)

    # Payloads are not yet required to carry complete input lineage. An
    # id/name scan would miss a selected record paraphrased into a task input.
    # The beta therefore holds all engine inference local while record-level
    # protections exist; future certified lineage may narrow this impact.
    matched: list = ["owner_only_record_policy"] if record_floor else []
    allowed: Optional[Set[str]] = set(LOCAL_PROVIDERS) if record_floor else None
    from .blackhole import terms_of

    # A NAME as the share boundary itself reads one (WS0's rulings on BL-112, review R-N1-151 H2 and R2 N1-N3): a term
    # under 4 characters as a whole token with its forms, a longer one anywhere separator-free, a name in the Arabic or
    # Hebrew script anywhere; a handle, a username or an id only as itself (`OffLimitsTerms`). First ONE pass over every
    # entry's names together, as a fast "no" (N3); per entry only on a hit, to take the strictest tier.
    every = [(row, terms_of(row)) for row in rows]
    names = {name for _row, terms in every for name in terms.names}
    any_hit = (_boundary_names_hit(names, raw_text)
               or any(_names_hit(terms, "", haystack) for _row, terms in every)
               or any(terms.identifiers_found(haystack) for _row, terms in every))
    tiers = {row["processing_tier"] for row in rows}
    if any_hit and len(tiers) == 1:
        # Every entry has the one tier: which entry hit changes nothing, so no pass per entry.
        matched.append("off_limits_name")
        tier = TIER_PROVIDERS.get(next(iter(tiers)), TIER_PROVIDERS["local_only"])
        allowed = set(tier) if allowed is None else (allowed & set(tier))
        any_hit = False
    for row, terms in every if any_hit else ():
        hit = (next(iter(sorted(terms.names)), None) if terms.names and _names_hit(terms, raw_text, haystack)
               else terms.identifiers_found(haystack))
        if hit is None:
            continue
        matched.append(hit)
        tier = TIER_PROVIDERS.get(row["processing_tier"], TIER_PROVIDERS["local_only"])
        # Several protected entities in one payload means the strictest wins:
        # the intersection, never the union.
        allowed = set(tier) if allowed is None else (allowed & set(tier))
    # Not read here (WS0's ruling on review R-N1-151 R2 N2): the boundary's closure (an entry's name parts, linked
    # aliases and learned spellings, contacts and their handles). `EntityBoundary.mentions_protected` once per call cost
    # 0.107 s on a 115 KB payload with 24 entries, over the gate's 0.12 s budget with the names' pass (0.024 s). As in
    # 1.5.0, a payload naming the person only by a first name or a surname goes to the configured provider.

    if not matched:
        return EgressVerdict(False, frozenset(), (), provider=configured)

    allowed_frozen = frozenset(allowed or frozenset())
    if configured in allowed_frozen:
        return EgressVerdict(True, allowed_frozen, tuple(matched), provider=configured)

    # Redirect: prefer a local adapter, then anything else admissible.
    fallback = next(
        (p for p in sorted(allowed_frozen) if p in LOCAL_PROVIDERS),
        next(iter(sorted(allowed_frozen)), None),
    )
    if fallback:
        logger.info(
            "blackhole: redirecting inference from %s to %s (%d protected mention(s))",
            configured or "<unset>",
            fallback,
            len(matched),
        )
    return EgressVerdict(
        True,
        allowed_frozen,
        tuple(matched),
        provider=fallback,
        redirected_from=configured if fallback else None,
    )


def describe_block(verdict: EgressVerdict) -> str:
    """Operator-facing reason. Deliberately names no entity.

    The error can surface in logs, a job record, or an API response, none of
    which are guaranteed to be owner-only — so it says that protected content is
    involved without saying which.
    """
    return (
        "blackhole_secure_processing_required: this content mentions a protected entity "
        f"and may only be processed by {sorted(verdict.allowed_providers) or ['a secure provider']}; "
        "no such provider is available"
    )
