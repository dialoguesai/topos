"""Minimal canary-bearing corpus for engine-level privacy probes.

Builds an in-memory AdapterBundle (mirrors scripts/benchmarks/wiki_mvp_baselines._make_bundle)
with one planted PII canary in a conversation_messages row. The raw `content` column holds
the secret; the `content_disclosure` column holds the ingest-redacted form. A grantee read
must surface only the redacted form — never RAW_CANARY.

This is the fast (in-memory) build mode from plan §A.1. The seeded-SQLite full-ingest mode
(where the privacy layer actually computes disclosure columns) is a later addition; here we
pre-populate the disclosure column so the probe isolates the read-path enforcement, not ingest.
"""

from __future__ import annotations

from dataclasses import dataclass

from topos.query.manifest import ScopeResolutionManifest
from topos.storage.adapters.factory import AdapterBundle
from topos.storage.adapters.fakes import (
    InMemoryAuditLogStore,
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemoryQuerySessionStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)

# The secret that must never reach a grantee, and the redacted form that may.
RAW_CANARY = "reach me at zx-canary-7431@example-priv.net or +1-555-0199"
REDACTED_CANARY = "reach me at [REDACTED_EMAIL] or [REDACTED_PHONE]"

MESSAGES_SCOPE = "messages:read"
MESSAGES_TABLE = "conversation_messages"


@dataclass
class CanaryBundle:
    bundle: AdapterBundle
    raw_canary: str
    redacted_canary: str
    scope_id: str
    manifest: ScopeResolutionManifest


def _raw_manifest() -> ScopeResolutionManifest:
    return ScopeResolutionManifest(
        scope_id=MESSAGES_SCOPE,
        primary_dimensions=["messages"],
        canonical_tables=[MESSAGES_TABLE],
        access_mode_ceiling="raw",
    )


def build_canary_bundle() -> CanaryBundle:
    canonical = InMemoryCanonicalStore()
    canonical.upsert(
        MESSAGES_TABLE,
        {
            "record_id": "canary-msg-1",
            "content": RAW_CANARY,
            "content_disclosure": REDACTED_CANARY,
            "sender_id": "u1",
        },
    )
    bundle = AdapterBundle(
        canonical=canonical,
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )
    return CanaryBundle(
        bundle=bundle,
        raw_canary=RAW_CANARY,
        redacted_canary=REDACTED_CANARY,
        scope_id=MESSAGES_SCOPE,
        manifest=_raw_manifest(),
    )


# --- CER (Canary Extraction Rate) corpus: one planted secret per sensitivity class -------

@dataclass
class Canary:
    """A planted secret. `token` must never reach a grantee; the owner path must show it
    (that contrast makes the CER measurement non-vacuous)."""

    canary_id: str
    cls: str
    token: str
    should_leak_to_grantee: bool = False


@dataclass
class CerCorpus:
    bundle: AdapterBundle
    canaries: List["Canary"]
    scope_id: str
    manifest: ScopeResolutionManifest


# Class-specific unique tokens. Each appears ONLY in a record's raw `content`; the
# `content_disclosure` column holds a clean form with the token removed.
_CER_EMAIL = "zx-cer-email-5501@example-priv.net"
_CER_PHONE = "+1-555-0177"
_CER_THIRD_PARTY = "Vasquez Oleander"
_CER_NSFW = "cer-nsfw-marker-8842"
_CER_RAW_BODY = "cer-secret-body-3319"


def _msg(record_id: str, content: str, content_disclosure: str, **extra: Any) -> Dict[str, Any]:
    return {
        "record_id": record_id,
        "content": content,
        "content_disclosure": content_disclosure,
        "sender_id": "u1",
        **extra,
    }


# Shared retrievable phrase in every canary record, so a single query retrieves ALL of them
# and disclosure is genuinely exercised on each class (otherwise query-filtering could drop a
# record before disclosure runs, making a "no leak" result vacuous).
CER_QUERY_PHRASE = "project atlas"


def build_cer_corpus() -> CerCorpus:
    """Plant one canary per class in conversation_messages. Raw content carries the token;
    the disclosure column is clean; the NSFW record is tagged so grantee reads exclude it.
    Every record shares CER_QUERY_PHRASE so one query retrieves them all."""
    canonical = InMemoryCanonicalStore()
    canonical.upsert(
        MESSAGES_TABLE,
        _msg(
            "cer-email",
            content=f"re: {CER_QUERY_PHRASE} — contact me at {_CER_EMAIL} or {_CER_PHONE}",
            content_disclosure=f"re: {CER_QUERY_PHRASE} — contact me at [REDACTED_EMAIL] or [REDACTED_PHONE]",
        ),
    )
    canonical.upsert(
        MESSAGES_TABLE,
        _msg(
            "cer-third-party",
            content=f"re: {CER_QUERY_PHRASE} — had lunch with {_CER_THIRD_PARTY} yesterday",
            content_disclosure=f"re: {CER_QUERY_PHRASE} — had lunch with a contact yesterday",
        ),
    )
    canonical.upsert(
        MESSAGES_TABLE,
        _msg(
            "cer-nsfw",
            content=f"re: {CER_QUERY_PHRASE} — explicit content {_CER_NSFW}",
            content_disclosure=f"re: {CER_QUERY_PHRASE} — [removed]",
            content_nsfw=1,
        ),
    )
    canonical.upsert(
        MESSAGES_TABLE,
        _msg(
            "cer-raw-body",
            content=f"re: {CER_QUERY_PHRASE} — the launch password is {_CER_RAW_BODY}",
            content_disclosure=f"re: {CER_QUERY_PHRASE} — a message about logistics",
        ),
    )
    bundle = AdapterBundle(
        canonical=canonical,
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )
    canaries = [
        Canary("cer-email", "email_phone", _CER_EMAIL),
        Canary("cer-email-phone", "email_phone", _CER_PHONE),
        Canary("cer-third-party", "third_party_name", _CER_THIRD_PARTY),
        Canary("cer-nsfw", "nsfw", _CER_NSFW),
        Canary("cer-raw-body", "raw_body", _CER_RAW_BODY),
    ]
    return CerCorpus(bundle=bundle, canaries=canaries, scope_id=MESSAGES_SCOPE, manifest=_raw_manifest())
