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

**Taint is a substring scan, and that is a floor, not a ceiling.** Structured
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
    from .blackhole import BlackholeStore

    return BlackholeStore(conn).list()


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
    if not rows:
        return EgressVerdict(False, frozenset(), (), provider=configured)

    haystack = normalize_entity_name(text_of(payload))
    if not haystack:
        return EgressVerdict(False, frozenset(), (), provider=configured)

    matched: list = []
    allowed: Optional[Set[str]] = None
    for row in rows:
        terms = [row["normalized_name"], *row.get("aliases", [])]
        hit = next((t for t in terms if t and t in haystack), None)
        if hit is None:
            continue
        matched.append(hit)
        tier = TIER_PROVIDERS.get(row["processing_tier"], TIER_PROVIDERS["local_only"])
        # Several protected entities in one payload means the strictest wins:
        # the intersection, never the union.
        allowed = set(tier) if allowed is None else (allowed & set(tier))

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
